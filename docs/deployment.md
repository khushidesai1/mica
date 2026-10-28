# Deployment

For the staged move from the existing deployment to `mica-staging`, follow the
[resource migration runbook](mica-resource-migration.md). Its separate Terraform root creates
the replacement stack without renaming resources in the existing state. Foundation and preview
do not switch Slack or Gmail traffic; admission, storage verification and the coordinated
cutover must complete before old resources can be retired.

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
When a host or container runtime fails during startup, isolate that boundary with a small
targeted probe before rebuilding the application images. Reuse an already verified immutable
image when only an operator check changes; lint and validate that check against the actual
production launch behavior. A verifier-only correction does not require another image build.
A worker image rebuild for Slack presentation, read-only routing or request-authorization metadata
alone does not require a live GPU job when the pinned GPU runtime, scientific dependencies,
inference code, device setup and isolation are unchanged. Use focused behavior tests plus packaged
source/compatibility gates and record the previous runtime validation being reused. Run actual CUDA
and sandbox checks for changes to those GPU execution components or new hardware compatibility
claims; do not launch a GPU simply because application source was repackaged.
Controller-only placement or approval changes can reuse the exact verified worker images after
contract checks and focused failure/race tests. A provider stockout should exercise compatible
GPU fallback, rather than block that controller release. Record hardware not yet observed in
live inference separately; do not claim that a fallback test verifies its scientific execution.
Run live Slack smoke checks only in the owner's private DM, never in shared project channels.
Simulate malformed model output, outages, and retry timing locally with fakes; do not inject
failures into collaborators' conversations. Remove synthetic DM messages after verification.

Routing uses the primary `AGENT_MODEL` first. Its one bounded correction attempt uses
`AGENT_ROUTING_REPAIR_MODEL` (default `claude-opus-5`) with forced `route_request` selection;
choose a repair model that supports forced tool choice. The correction retains the same context,
schema, authorization checks and shared routing deadline. Invalid prose never starts a worker.

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
An AtoMx request hands email delivery to the transfer controller: its preparation receipt is
silent, required forms arrive when needed, and the verified transfer result arrives at completion.

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

## Folder and script delivery

Both conversation surfaces use the same attachment policy. New or modified reports,
text/configuration files, source scripts and ZIPs in `outputs/` are eligible at any depth;
`outputs/gcs/` is exclusively bucket staging. Unsupported files produce an explicit delivery
failure, rather than being silently skipped. Prepared ZIPs and source files are retained with
run provenance for resends.

For a folder containing scripts, the worker's `prepare_folder_attachment` tool creates a
complete ZIP from `code/` or an `outputs/` subfolder. It includes every regular file and empty
subdirectory, including unchanged helpers and binary results, and preserves relative paths.
The tool returns the package path, file count, sizes and a bounded path inventory. It refuses
links, special files, changing trees, over 10,000 entries, or a tree/package exceeding the
surface's budget (200 MB Slack; 17 MB email). It never returns a partial archive as success.
Existing credential checks for source scripts remain in force. Large folders use the approved
recursive GCS publisher; packaging does not grant bucket write access.

ZIPs are for script-containing folders. Ordinary reports and figures remain direct attachments.
When a folder is packaged, only its figure/PDF previews accompany the ZIP; contained scripts
and tables are not sent again individually. Email's combined attachment/MIME limit still
applies, and any omitted attachments are named in the final reply. Model text and tool results
say a package is prepared, not delivered; actual delivery is recorded after the transport succeeds.

This behavior runs in the conversation worker, so an API-only rollout is insufficient. Use the
coordinated Slack release pipeline and its complete-folder image gate, preserving existing jobs
and immutable worker references during rollout.
### Versioned Cellpose tools

See [Cellpose versions](cellpose.md) for isolated Cellpose 3 / Cellpose-SAM runtimes, coordinated
CPU/GPU releases and the live GPU gate. Preserve the GPU image pin during API-only releases and
update both workers together when worker source changes.

### Read-only project reference repositories

Set these nonsecret settings on the control-plane API only (never worker environment variables):

```text
AGENT_REFERENCE_GITHUB_ORG=azizilab
AGENT_REFERENCE_GITHUB_APP_ID=5405671
AGENT_REFERENCE_GITHUB_INSTALLATION_ID=173616981
AGENT_REFERENCE_GITHUB_KEY_SECRET=projects/azizilab-aml/secrets/mica-github-secret/versions/<verified-version>
```

The existing Mica AziziLab GitHub App must be installed on the selected AziziLab repositories with
Contents read permission. Store the app-generated RSA PEM private key as a verified numeric Secret Manager version;
a client secret or webhook secret is not a private key. The RSA private key stays in Secret Manager, readable by the API
identity. No new secret is required. Do not reuse `AGENT_MEMORY_TOKEN_SECRET` or a worker credential.
If a newly requested repository is unavailable, an organization administrator adds it to the app's
installation selection; project owners cannot expand that ceiling through chat or project metadata.
The API requests short-lived installation tokens narrowed to one repository and read-only contents.
See [GitHub's installation-token documentation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

The weekly `/api/v1/internal/tools/catalog` refresh uses the same GitHub App settings and key,
minting a fresh read-only installation token on every run. It lists the installation's approved
repositories through `/installation/repositories`; it does not use the legacy
`MICA_GITHUB_APP_TOKEN`/`PISTACHIO_GITHUB_APP_TOKEN` or the memory repository credential.
Changing that old static token is unnecessary. The job catalogs repository metadata and commit
IDs only; it does not install or run tools. GitHub reads finish before catalog updates begin, and
existing proposed, approved, building, available or failed tool records are preserved. Failures
are labeled as the scheduled GitHub tool-catalog refresh in the existing failure channel.
After a code release, run the existing `*-tool-catalog` Cloud Scheduler job once and verify an
HTTP 200 and its catalog count. This exercises the actual API identity and configured GitHub App
without changing credentials, IAM or the weekly schedule.

The catalog also makes the approved AziziLab repositories discoverable across every project;
per-project registration is optional. Requests select only the references they need. Read-only
source/parameter/documentation questions use the direct Claude API path; execution requests pass
snapshots to workers.
This controller-only selection change uses the existing snapshot format and can retain verified
worker image pins after compatibility checks. Verify offline fetch,
authorization, snapshot, read-only and failed-fetch tests before release. For the live owner check,
ask about a catalog repository from a project with no default references, without registering it,
or add a permitted default in your own private Slack conversation and confirm the selection. Ask
about a known source parameter; the answer should cite `inputs/repos/<repo>/...` and its pinned
commit. Remove the selection and confirm a subsequent compute request has no restored reference
folder. Do not send synthetic messages to another user's conversation.

The bounded information-read route is an API change and can retain verified worker images after
contract checks. Validate direct repository/text/history replies, source byte/round limits, current
access checks, catalog removal before delivery, and README replies longer than one Slack chunk.
A partial listing or read failure must not automatically launch a worker. A live owner request to
explain a catalog repository should produce source-grounded documentation without a new Batch job.
No synthetic Slack/email message or repository write is required for release validation.

### Flexible compute and durable stages

Deploy the API and both Claude workers together for the new allowance controls, durable stage
storage and Slack activity states. Preserve the existing science worker after its execution
compatibility gate. Set `AGENT_GPU_MAX_SECONDS=604800` and
`AGENT_GPU_DEFAULT_SECONDS=28800` on the controller; keep hardware/image authorization and
model-budget settings intact. Approval durations are request-bound; old requests are not extended.
No Terraform, IAM or secret changes are required. See [Cellpose](cellpose.md#long-runs-and-saved-progress).

Required regression evidence includes requester-bound allowance selection, unchanged cumulative
limits across continuation, and result restoration after terminal cleanup. Exercise those paths
in the worker image with synthetic local storage. The final GPU smoke remains a small synthetic
crop/isolation check; do not rerun a whole-slide workload for deployment.
