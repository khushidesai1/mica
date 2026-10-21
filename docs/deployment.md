# Deployment

## Build immutable images

Resolve the Python base image tag to a digest, pass it through `PYTHON_IMAGE`, and publish both
images to Artifact Registry. Record the resulting digests in Terraform variables.

```bash
docker build -f docker/Dockerfile.api \
  --build-arg PYTHON_IMAGE=python:3.12.12-slim@sha256:REPLACE \
  -t REGION-docker.pkg.dev/PROJECT/REPO/api:VERSION .
docker build -f docker/Dockerfile.science \
  --build-arg PYTHON_IMAGE=python:3.12.12-slim@sha256:REPLACE \
  -t REGION-docker.pkg.dev/PROJECT/REPO/science:VERSION .
docker push REGION-docker.pkg.dev/PROJECT/REPO/api:VERSION
docker push REGION-docker.pkg.dev/PROJECT/REPO/science:VERSION
```

Use your normal Cloud Build pipeline instead of the illustrative commands if it already records
SBOMs and provenance.

For application code updates, `cloudbuild/deploy-code-update.yaml` builds and explicitly pushes
both images, runs the isolated-executor Matplotlib smoke test inside the new science image, resolves
both tags to immutable digests, and deploys the API while updating `MICA_BATCH_IMAGE` to the
new science digest. A successful image build alone is not a deployment: Cloud Run otherwise keeps
the prior worker digest for every newly submitted Batch job.

The coordinated pipeline exports `ExecutionBundle` from the API image and checks it inside the
worker before publishing either image. The check includes all decision scopes, requires an exact
schema match and verifies lossless bundle deserialization. API health checks alone do not establish
worker compatibility. Include `scripts/check_worker_compatibility.py` in the build context.

For an API-only release that changed execution models, synchronize the worker before running new
tasks. `cloudbuild/refresh-worker.yaml` builds a candidate from the existing science digest and copies
the application package from the exact deployed API digest. This preserves scientific dependencies
and tests the API/worker contract before pushing. It does not deploy automatically: run the bounded
protocol-2 Batch conformance check, then update `MICA_BATCH_IMAGE` to the candidate digest.
Keep worker sandbox settings unchanged. Existing tasks pin their runtime and need a deliberate,
audited recovery; changing the service variable only affects newly initialized sessions.

## Headless API rollout

The web application, Google browser OAuth flow, and interactive API documentation pages have been
removed. `/`, `/auth/*`, `/assets/pista_logo.svg`, `/docs`, and `/redoc` return 404.
Use `/api/v1/healthz` for health checks. The JSON API, `/openapi.json`, signed Slack ingress,
service-account OIDC callbacks, Gmail, and GitHub connections remain in the same service.

`cloudbuild/build-slack-api.yaml` now builds the complete headless API from source using
`docker/Dockerfile.api`. Supply `_API_IMAGE` and the existing compatible shared task worker as
`_WORKER_IMAGE`; `_PYTHON_IMAGE` is a pinned Python base image. The build verifies the absence of
the UI and checks the API/worker execution contract before publishing. The old `_BASE_API_IMAGE`
overlay and web-file preservation scripts are retired. If the execution contract differs, use
the coordinated API/worker pipeline above; do not bypass the compatibility check. Build the
Claude worker separately with `cloudbuild/build-slack-agent.yaml` when its source changes.

Preserve deployed Slack tokens, signing secrets, channel access settings, project registry, memory,
queues, worker images, and bucket connections. Browser OAuth variables are no longer consumed or
mounted; remove them from local tfvars/environment files when convenient. The old OAuth Secret
Manager resource remains tracked to avoid deleting existing credentials as part of UI removal.
Keep the separate signed-link secret for signed upload operations.
No Terraform apply or cloud deployment is implied by these source changes.

## Release validation

Run Ruff, mypy and the fast automated suite, then use small, bounded live smoke checks for
changed behavior. Prefer fakes for large file counts, large object sizes and failure injection.
Do not run lengthy model conversations or full-scale data transfers for routine releases;
reserve those for explicit scale validation or a specific unresolved production issue.
Keep build, authentication and worker compatibility gates, and clean up synthetic outputs.
Run live Slack smoke checks only in the owner's private DM, never in shared project channels.
Simulate malformed model output, outages, and retry timing locally with fakes; do not inject
failures into collaborators' conversations. Remove synthetic DM messages after verification.

## Provision

```bash
cd infra
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan
terraform apply
```

Set `planner_version = 2` in staging. New tasks store this value permanently; nonterminal legacy
tasks remain on planner v1. The default worker is `gpt-6-luna` at low reasoning and falls back to
`gpt-5.6-sol` low if the configured worker model is unavailable. Promote the flag only after the
stage latency and approval-continuity smoke tests pass.

The 180-second master-planner and 150-second worker limits are failure guardrails, not latency
targets. Stage timing remains visible in task metadata, and completed stages survive a timeout so
only the failed stage is retried. Their bounded worst-case path remains below the 900-second Cloud
Tasks dispatch deadline.

Use separate GCP projects or at minimum separate Terraform state and resource names for staging and
production. First target the Secret Manager resources, add the OpenAI, Slack, GitHub, and
link-signing secret versions with `gcloud secrets versions add`, then run the full apply (Cloud Run
references `latest`). Never put secret values in committed tfvars.

Configure the Slack event and interactivity URLs, Gmail mailbox OAuth/watch, and GitHub App
after the first apply. Create a read-only Cloud Build trigger for this repository using
`cloudbuild/tool-builder.yaml`, place its trigger ID in `tool_build_trigger`, and grant its build
service account only source-read, Artifact Registry write, scanner, and Cloud Build log access.
An exact tool approval starts that trigger; only a successful scan/smoke-test completion marks the
tool available and requeues waiting tasks. Run staging smoke tests for Slack, Gmail, the API, unknown-tool approval,
GPU/high-cost approval, duplicate delivery, Spot retry, and missing-completion reconciliation before
promoting the same image digests to production.

Batch workers use the configured VPC and subnet with no external IP address. The subnet must have
Private Google Access enabled so workers can pull Artifact Registry images and exchange bundles with
Cloud Storage and Pub/Sub. A five-minute reconciliation job marks terminal Batch failures and
recovers successful results if a worker completion event is missed.

The Terraform queue caps concurrent dispatches at three and Cloud Run scales to zero. Batch hard
limits and the $20 gate are enforced again in application policy, independent of Terraform.

## Migrating a staging project folder

Project identities remain stable when moving an artifact prefix. The optional GCS object
`projects/_registry/storage-paths.json` maps project IDs to physical folder names, for example
`{"pista-cosmx-6k-crc": "crc-cosmx"}`. Every analysis storage operation resolves this index;
checkpoint listings retain their logical keys. Private-user runs and internal metadata keep
their existing paths. Mapping entries must be unique, without chains or reserved internal names.

Deploy mapping-aware API and worker images first. Pause task dispatch, confirm workers have
stopped, and drain in-flight API requests before changing data. Copy the exact source prefix to
an unoccupied destination with source-generation and create-only preconditions. Verify object
counts, sizes and CRC32C checksums, then confirm source generations have not changed. Publish
the mapping with a generation precondition; verify reads through the old logical keys before
deleting only those recorded source generations. Record the inventory and cutover evidence.
Restore dispatch after verification. A failed verification must preserve the source objects.

After deleting source objects, rolling back to an API or worker without mapping support requires
copying the data back and removing the mapping under the same drain-and-verify procedure. Merely
switching to an older revision would make the renamed project's saved results inaccessible.

## Gmail activation

Deploy the API and Claude worker together; update `MICA_SLACK_AGENT_IMAGE` to the new immutable
Claude digest and preserve `MICA_BATCH_IMAGE` for the shared science worker. Run the existing
API/shared-worker compatibility gate. Email defaults off when any required OAuth or label setting
is missing. The Gmail refresh token stays in the control plane; workers submit bounded artifact
references through the authenticated, attempt-fenced `/api/v1/internal/slack-analysis/email-result/`
callback. Do not mount Gmail credentials in workers.

The mailbox owner must complete these activation steps:

1. Create a desktop OAuth client and consent configuration for the consumer Gmail mailbox.
   Run `.venv/bin/python scripts/gmail_authorize.py /secure/path/client_secret.json` locally.
   It requests Gmail readonly, send and modify scopes and prints a sensitive refresh token.
   Keep the client JSON and token out of Git and logs. External OAuth apps left in Testing can
   issue short-lived refresh tokens; configure the consent app appropriately for ongoing use.
2. Create Secret Manager secrets for the OAuth client secret and refresh token. Set
   `gmail_oauth_client_secret_name`, `gmail_refresh_token_secret_name`,
   `gmail_oauth_client_id`, and optionally `gmail_oauth_secret_version` in private tfvars.
3. Set `gmail_account = "azizi.lab.341+mica@gmail.com"`, create a Gmail filter labeling requests
   to that alias, set `gmail_label = "Mica"` (or its label ID), and list other bot addresses in
   `gmail_aliases`. Confirm the base mailbox and label before enabling intake.
4. Apply Terraform yourself to mount the existing secrets and environment settings. Routine
   application releases do not create secrets, change IAM, or apply Terraform.
5. Call the authenticated `/api/v1/internal/gmail/renew-watch` endpoint. Verify its history ID,
   then send `projects` and a small artifact-producing analysis from your own authorized address.
   Confirm threading and one final reply, without an acknowledgement or progress emails.
   Check reply-all filtering only
   with addresses you have authorized for testing.

The first activation records a time cutoff and history baseline; mail older than activation is
not executed. Watch renewal never advances the processed cursor. Expired history triggers a
resumable scan of labeled mail since activation, protected by durable message receipts. Labeling
an older, pre-activation message does not execute it; forward a fresh request instead.

Email sends final answers, failures, and cancellations. Required clarification questions,
approval requests, and private credential links still arrive by email so work cannot silently
wait for user input. Acknowledgements, queue/startup notices, automatic retry updates, and transfer
progress stay on Slack; worker progress remains local for email-origin requests. The same policy
applies to older task conversations, so their final emails do not include earlier progress text.

The GCS outbox at `projects/_gmail/outbox/<event>/final.json` records send intent
before contacting Gmail. Gmail can rewrite the RFC822 Message-ID, so new sends also carry a random
`X-Mica-Delivery-ID` recorded in the outbox. A lost response is reconciled against that header on
sent messages in the original thread. Legacy operations use their saved Message-ID search.
An ambiguous send with no unique matching sent message is held for operator review; it is never
blindly resent. Inspect Gmail and the recorded delivery identifier before resolving a stuck
operation. This prioritizes avoiding duplicates and does not promise exactly-once delivery.
Existing `ack.json` receipts remain for historical reconciliation; new requests do not create them.

Email intake removes conventional signature blocks and trailing quoted replies before routing
the new request. For example, `projects` followed by a signature still runs the projects command.
Inline replies below quoted text remain intact. Sender verification always checks the original
signed message bytes before extracting request text.


Natural-language account and control requests are documented in
[`natural-language-requests.md`](natural-language-requests.md). This API release includes
sender-only account replies, guarded project creation, signed administrative confirmation,
and stable email follow-up session metadata. It does not enable or replace AtoMx transfer
images, credentials, or infrastructure. Run the routing snapshot and complete application checks
before the existing API/worker compatibility release gates.


## Project memory archive rollout

Use `cloudbuild/build-slack-release.yaml` to build the API, Claude worker, and refreshed shared
worker from the same source commit. Its offline gates verify archive materialization, paged
indexes, immutable archive proposals, prompt bounds, and worker execution compatibility.
Update the API image and immutable worker image environment pins in one revision; preserve
all other runtime settings and secret references. Preview that revision before promoting traffic.

For the first archive rollout, set `AGENT_MEMORY_ARCHIVE_AFTER` to a Unix timestamp later than
promotion plus the old revision's request timeout and a safety margin. Verify that deadline
has passed before checking archive behavior. This lets old API requests finish scheduling their
old-format snapshots while normal analysis continues. Existing worker snapshots remain immutable;
late proposals use the new controller's conflict-preserving writer. Do not pause the shared queue.
Run `tools/memory_smoke.py --github-cli` with the deployed memory repository settings and record
and confirm cleanup of the exact `projects/_smoke/` folder. Never create a real project or send
user-facing messages for this store check. After archives exist, retain archive-compatible API
and workers when rolling back. See [project memory](project-memory.md) for limits and controls.

## AtoMx personal connections

The coordinated release now packages the AtoMx tool on the existing worker and adds private
connection/approval links for Slack and email. Enable the runtime settings only after the
normal release checks pass. See [AtoMx transfers](atomx-transfers.md) for the existing-key,
trusted-host, writer-prefix and worker-identity requirements. The initial rollout skips a live
SFTP canary by explicit owner request; the owner will start the real-data transfer.

## Failure channel

Set `AGENT_FAILURE_CHANNEL` to the private operator channel ID after inviting Mica. This is an API-only
change using `cloudbuild/build-slack-api.yaml`; preserve both worker image pins. The existing five-minute
reconciliation endpoint delivers summaries; the daily maintenance endpoint expires old incident state.
Verify those schedules remain enabled. See [failure-log.md](failure-log.md) for coverage, delivery
recovery and retention. Empty channel configuration still records incidents in Firestore.
