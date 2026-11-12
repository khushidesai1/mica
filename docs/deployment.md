# Deployment

The active service is `mica-staging` in `azizilab-aml/us-central1`, with mailbox
`mica@azizilab.com` and artifacts bucket `mica-staging-artifacts-azizilab-aml`.
Deploy subsequent releases to this service and preserve its current configuration and worker
image pins. The former `pistachio-staging` service is frozen for rollback; its queue, schedulers,
and push consumers must remain paused. Existing buckets and historical images remain available.
The [resource migration runbook](mica-resource-migration.md) describes the separate state owners
and the reverse reconciliation required before rollback after new writes. Source merging or
a healthy new release does not authorize deleting legacy resources.

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

GCS operation confirmations and controller-executed moves/deletions are API-only. Use
`cloudbuild/build-slack-api.yaml`, preserving the deployed CPU, GPU and shared-worker image
pins and all runtime access settings. The existing scoped writer role supplies get/create/delete;
no Terraform, IAM or secret change is part of this release. Validate exact-operation cards,
immutable source inventories, occupied destinations, changed generations, revocation and
interrupted copy/delete recovery. Run a tiny move and delete in a recorded disposable prefix
using the actual controller/writer path, verify source removal and unchanged siblings, then
remove all smoke data. Unit tests alone do not establish live IAM conformance. Never use real
scientific data as the rollout smoke test.

Project-backchannel changes require coordinated API and Claude CPU/GPU worker application
sources. The manifest enables live coordination only for newly routed requests; existing
workers cannot acquire hooks retroactively. `AGENT_BACKCHANNEL=false` disables new conversation
tools and worker broker access. The offline `check_backchannel_release.py` gate checks the board,
peer exchange and worker-hook contract without model calls or scientific execution.

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

Conversation routing receives a fresh compute catalog derived from the deployed tier settings
on every turn. It lists host RAM separately from GPU VRAM, CPU count, usable workspace, runtime
ceilings and resize support. This describes enabled configurations, not a live capacity guarantee.
Explicit memory requirements are checked before worker approval or allocation, and every GPU
fallback must meet both host-RAM and VRAM requirements. An unsupported memory request receives a
direct explanation; the conversation cannot retry it with a lower requirement in the same turn.
With `AGENT_GPU_FLEXIBLE_SIZING=true`, GPU requests require a structured sizing assessment before
approval: host RAM, per-device VRAM, CPUs, exact GPU count, evidence and parallelism justification
for multiple GPUs. The controller selects a fitting configuration by estimated cost; an explicit
GPU preference is honored when it fits. A 128 GiB host-RAM request can use a custom N1/T4 or
`g2-standard-32` L4 worker. Every fallback preserves the same resource minima and GPU count.

The trusted catalog supports T4 (16 GiB per GPU), L4 (24 GiB), A100 (40 GiB), A100 80 GB and
H100 (80 GiB). Enable families through `AGENT_GPU_TYPES=t4,l4,a100,a100-80gb,h100` only with a
compatible pinned GPU worker; leave `AGENT_GPU_DEFAULT_TYPE=t4`. The default is one GPU and
larger families require an assessed need. Multiple-device VRAM is not automatically pooled.
See Google's [GPU configurations](https://docs.cloud.google.com/compute/docs/gpus) and
[accelerator machine types](https://docs.cloud.google.com/compute/docs/accelerator-optimized-machines).
H100 configurations with 1–4 GPUs use Spot; an explicit on-demand request cannot silently become
Spot or increase its GPU count. The approval card binds the sizing evidence, exact primary and
alternative machines, each provisioning mode, image, time allowance and worst estimated cost.
Placement can try one approved alternative before credentials are issued and only after the first
reservation is gone. It never changes hardware under a running computation.

Family enablement is not a quota or stock guarantee. Enable only families with usable regional
quota; for example an A100 80 GB quota of zero need not block H100 Spot. Provider capacity/explicit
quota errors can trigger a fitting approved alternative; permissions and transport errors cannot.
Planning estimates include conservative compute/disk allowances and are not billing caps.
The model cannot edit this catalog, cloud permissions, quotas or deployment ceilings.
Existing requests without flexible profiles keep their original machine definitions and estimates;
image/configuration changes still require fresh confirmation under the existing approval rules.

CPU requests specify an advisory `expected_hours` alongside their memory and tier requirements.
`AGENT_ON_DEMAND_AFTER_HOURS` defaults to 2; CPU requests at or above that estimate use on-demand
unless Spot was explicitly requested. Retired workload categories do not size workers. Remove
the obsolete `AGENT_LAUNCH_HEAVY_TIER` setting when updating operator configuration.

GPU execution uses `run_gpu` with a named environment from `toolkit_catalog.py`. Bash remains
CPU-only. Image environments and validated saved package runtimes share this registry with
the conversation's compute catalog; saved runtimes must be restored with `ensure_packages`
before use. Scripts run offline and unprivileged, with only workspace writes, read-only inputs
and project data, and all allocated NVIDIA devices. Output folders save durably before success.
`MCP_TOOL_TIMEOUT` covers the run allowance and package-setup timeout; it does not extend the
approved worker deadline. Cellpose's adapter is bundled at
`/opt/pistachio/plugin/skills/cellpose/scripts/segment.py`, using the existing pinned environments
and model weights.

GPU worker guidance requires checking imports and a small CUDA operation through `run_gpu`
before loading large inputs or training. Missing NVIDIA devices in Bash are expected under its CPU sandbox.
The environment catalog includes the GPU image's pinned Torch/CUDA package versions;
`ensure_packages` returns the registered environment id and CUDA metadata for immediate use.
That metadata does not establish device or driver compatibility. When a method such as ResolVI
needs packages beyond a registered CUDA environment, use an isolated package runtime with
supported version constraints and repeat the small preflight. If no usable runtime is available,
report the failure; a requested GPU task must not silently become a full-data CPU probe or run.

For a new GPU architecture, use `scripts/check_gpu_hardware.py` for bounded CUDA arithmetic in
the actual general-tool sandbox, including device and credential isolation. It performs no model
inference or scientific workflow and uses no project mounts. Reuse prior T4/L4 inference evidence
when scientific dependencies are unchanged, and record unobserved hardware separately.
When the GPU tool or adapter changes, `scripts/check_cellpose_gpu.py` verifies one synthetic crop,
actual CUDA provenance and a durable-save round trip. The separate offline CPU gate executes the
skill script directly. These checks use temporary local data and never scientific project data.

Run live Slack smoke checks only in the owner's private DM, never in shared project channels.
Simulate malformed model output, outages, and retry timing locally with fakes; do not inject
failures into collaborators' conversations. Remove synthetic DM messages after verification.

All requests use the conversation loop with `AGENT_MODEL`. Enable `AGENT_FLEXIBLE_HARNESS=true`
on a coordinated API and Slack CPU/GPU worker release. The policy is pinned when each request
is received; already accepted requests retain their policy. Disabling the flag affects new
requests, without discarding active checkpoints. Deploy matching worker source before enabling it.

With this policy, routing persists its provider transcript and references in private control-plane
storage between queue deliveries. Six calls and the 90-second default delivery slice yield to
another delivery; they do not end the task. Transient provider failures retry with backoff and no
three-attempt ceiling. Active context is compacted while the original transcript is archived.
Invalid tool input returns to the model for correction. Refusals and permanent provider errors
end with an explanation. Starting a worker still requires a validated terminal tool call.

Slack check-ins offer **Keep working**, **Cancel task**, and **Report a problem** after two minutes
of routing, six routing calls, three transient routing failures, or 24 hours of unresolved routing.
The last threshold applies only to routing, never to an active analysis. Silence continues work;
acknowledging a reason suppresses repeat notifications for that reason. Reports are idempotently
saved to the existing failure queue, then delivered to `AGENT_FAILURE_CHANNEL`, with request and
project identifiers, phase, bounded counters, provider status and worker-attempt evidence. Reports
do not cancel or restart work. Only the original requester, in the original card, with current
access can act. Cancellation uses the existing cooperative stop and preserves saved outputs.
Email requests continue with the same routing policy, but do not receive Slack action cards.

Worker turn/model thresholds become continuation points: the saved SDK session resumes with full
tools and the controller posts a check-in. Model-cost accounting remains cumulative. GPU approval
cards explicitly describe the model notification threshold for new flexible requests. Existing
explicit compute time/hardware approvals, access controls, isolation, allocation ceilings and
bounded I/O remain enforced. Individual provider, HTTP and tool timeouts still protect connections;
they are not a total routing deadline. Cloud Tasks process deliveries allow 900 seconds and use a
960-second poll lease; configured routing slices cannot exceed 600 seconds. The deployment's
Cloud Run request timeout must be at least 900 seconds. No queue/IAM/Terraform changes are needed.

Requests accepted before this rollout retain the previous shared 120-second/six-call routing
policy and bounded retries. Cancel or explicitly resubmit a failed old request to use the new
policy; deployment never silently replays scientific work.
Remove the retired `AGENT_CONVERSATION_PRINCIPALS` environment variable during an API release;
there is no per-user routing flag. `AGENT_ROUTING_REPAIR_MODEL` remains the model setting for
failure-card assessments, with its existing `claude-opus-5` default.

The conversation API includes available tool schemas before Claude chooses a tool. The larger
`start_worker` schema uses ordinary tool calling because compiling it as a strict provider
grammar can reject even a simple chat request. Its input schema, local typed validation,
resource constraints, and allocation approvals remain enforced. Other conversation tools keep
provider strict mode. A small provider request can verify schema compatibility without starting
workers or running a scientific workload.

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


## Native Proseg runtime overlay

The coordinated application overlay also carries native Proseg CLI 3.1.1 at
`/opt/proseg/3.1.1/bin/proseg`. Its separate Rust builder verifies the crates.io archive checksum
in `deploy/proseg-cli.json` and builds with the archive's `Cargo.lock`; no compiler or changed
Python science dependencies enter the final image. The manifest, lockfile and MIT license
remain beside the executable. `build-proseg-cli` warms the native build cache before the
API/CPU/GPU overlays; retain that dependency when adding image-cache pulls to the pipeline.

`scripts/check_proseg_release.py` runs in each Claude worker image, offline and as UID 65532.
It enters the existing bubblewrap compute sandbox, checks isolation, exact version and flags,
then runs 4,000 synthetic transcripts through nondefault prior/connectivity/compactness controls
and validates cell metadata, transcript metadata, counts and SpatialData output. The outer
container permits nested bubblewrap and is capped at 2 CPUs/2 GiB; each subprocess has a
120-second limit. Temporary data is local and automatically removed. This gate proves native
runtime availability, not biological quality, convergence, a selected TMA recipe, or equivalence
to the custom SAP Python wrapper. Legacy perimeter controls and a random-seed flag are absent
in 3.1.1; they must not be fabricated or silently mapped to other controls. Real runs are neither
restarted nor reconfigured by deployment.

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
Claude failure assessments default on when the existing Anthropic credential is available;
`AGENT_FAILURE_INVESTIGATION_ENABLED=false` disables further assessment calls. This API-only
addition reuses the configured structured-response model, starts no compute jobs and requires no
new secrets, IAM or infrastructure. Alerts post before assessment and retain their original evidence
if Claude is unavailable. See the failure log for the bounded inputs, caching and call limits.

Impact-based priority, post-turn outcome review and the request watchdog are API-only as well;
preserve CPU, GPU and shared-worker image pins. Set `AGENT_OUTCOME_REVIEW_ENABLED=true` to
review newly completed replies asynchronously. No historical request backfill, worker allocation
or scientific inference is part of this release. Existing incidents acquire the new labels when
next due. Independently deploy the reviewed additive outage-alert configuration in
`infra/failure-logging`; keep its operator email in private tfvars and verify the uptime and
Scheduler policies without disrupting service. API deployment alone does not establish that
those external policies were applied or that the operator received a notification.

## Folder and script delivery

Both conversation surfaces deliver only files explicitly selected with `send_files`, plus
script snapshots when the user has enabled `scripts on`. Select only figures needed for review
and requested files. Creating or changing a file anywhere in `outputs/` does not select it.
Internal datasets, diagnostics and unused figures remain separate from attachments and do not
produce user-facing attachment or retention warnings. Provenance and workspace retention keep
their existing policies; `outputs/gcs/` remains exclusively bucket staging. Invalid explicit
selections return per-file tool errors. Prepared ZIPs and source files retain run provenance.
Selected attachments survive worker continuation separately from internal pending outputs;
legacy pending outputs do not implicitly become attachments.

Slack uploads selected files in batches of at most five and 200 MB combined, bounding SDK memory
and completion URL size. Each confirmed batch is recorded before the next; a later failure stops
delivery without retrying already shared files or treating unconfirmed files as delivered.

The worker's `send_files` tool accepts individual output files or complete folders. A folder
under `code/` or an `outputs/` subfolder becomes one ZIP. It includes every regular file and empty
subdirectory, including unchanged helpers and binary results, and preserves relative paths.
The tool returns the package path, file count, size and any per-path errors. It refuses
links, special files, changing trees, over 10,000 entries, or a tree/package exceeding the
surface's budget (200 MB Slack; 17 MB email). It never returns a partial archive as success.
Existing credential checks for source scripts remain in force. Large folders use the approved
recursive GCS publisher; packaging does not grant bucket write access.

Explicit folder requests are ZIPs; individual reports and figures remain direct attachments.
When a folder is packaged, only explicitly selected figure/PDF previews accompany the ZIP;
contained scripts and tables are not sent again individually. Email's combined attachment/MIME limit still
applies, and any omitted attachments are named in the final reply. Model text and tool results
say a package is prepared, not delivered; actual delivery is recorded after the transport succeeds.

With `scripts on`, the worker snapshots changed scripts beneath `code/` after Bash and GPU
execution tools, regardless of shell syntax or exit status. One ZIP contains their last snapshots;
these copies do not establish successful execution. Unchanged scripts from earlier turns are not
included automatically. Snapshots retain the ten-script and 1 MB-per-script limits, credential
checks and trusted checkpoint validation. The ZIP uses the normal archival, pending-output and
delivery path, including resends after transport failure.

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
The exact public-reference allowlist in `reference_repos.PUBLIC_REPOS` also makes
`khushidesai1/nympheas-temp` available across projects. It uses anonymous GitHub reads, checks
public visibility and repository identity on each fetch, and verifies commit-pinned raw files
against Git blob hashes under the same snapshot limits. It receives no GitHub App or memory
credential. Other personal repositories remain excluded; a private or moved repository fails
closed. This provides source access, not automatic package installation or execution.
Selection-only controller changes can retain verified worker image pins after compatibility
checks. Repository-validation changes must reach both API and worker images so materialization
recognizes the same approved references. Verify offline fetch,
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


### Shared approval lifecycle

Approval registry changes are API-only. Keep current worker pins, GPU allowances, mailbox secrets
and access settings. The existing Cloud Tasks identity invokes the authenticated
`/api/v1/internal/slack-analysis/approvals/<kind>/<id>` expiry endpoint; no new queue, IAM binding
or secret is needed. Fresh confirmations schedule deadline-specific tasks, and an old timer
rechecks the current saved deadline before doing anything. Existing active compute/write polls
also check expiry. Older dormant command/access cards without a timer are checked when opened.

Run offline fake-clock tests for all kinds, generation conflicts, requester/owner binding,
expired-card renewal, delayed timers, duplicate decisions and queue ordering. A renewal retains
saved work and asks for fresh confirmation; it cannot resume an old run after its conversation
has moved on. Preserve original approval keys/action IDs and legacy overwrite aliases during
rollback. Use the owner's scratch output path for the optional approve/reject live acceptance;
no real scientific work is needed to verify this controller change.


### Generic transfer receipt compatibility

The transfer handoff update is API-only and preserves live worker images and runtime settings.
Legacy handoffs and private-delivery receipt IDs remain readable during the transition. Generic
logical email stages retain the previous durable outbox key for new intents, so an older
controller also sees an ambiguous send and cannot repeat it. Delivery reads either saved
namespace; if both exist, operator reconciliation is required. Do not rename or delete saved
outbox objects during rollout. Existing queued transfer
tasks remain valid because the endpoint and tick checks are unchanged. This release needs
unit/contract checks; it does not need a live transfer or another scientific/GPU run.


## Thread memory rollout

Build the API and Claude worker with `cloudbuild/build-slack-release.yaml`, including the
thread-memory modules and the updated prompt/boundary checks. Start with
`AGENT_THREAD_MEMORY=dual`. Preserve both worker pins and all unrelated settings. Older
in-flight workers keep their legacy memory handoff; import their history after they finish.
Never cancel user computation for the release.

Preview and apply the thread importer as described in [project memory](project-memory.md),
then verify the complete coverage report in
`projects/_registry/thread-memory-coverage.json`. Switch to `github` for read-path verification
while both stores continue receiving writes. Verify cross-thread lookup and a tiny worker
script/plot in an isolated test project, and record exact smoke paths for cleanup.

After the soak has no pending-memory alerts, recheck migration coverage and active workers
before switching to `github_only`. This enables expiry; it is a separate operator cutover,
not implied by merging the release. Keep incomplete import, soak or cutover work in the active
rollout record. Read-back hash/generation checks and cleanup gates must remain enabled.

Use the existing quiet-release boundary for the final switch. Run the importer with
`--verify-cutover --apply` only when its fresh coverage, checkpoint-generation, active/recovery,
and pending-save checks pass. Bind `AGENT_THREAD_MEMORY_CUTOVER_AT` to the resulting report's
exact `checked_at` timestamp in the same revision that enables `github_only`; preserve all image
pins. Resume dispatch after verifying that revision. Oversized legacy workspaces are retained
with explicit reasons under the bounded archive policy; they do not excuse missing conversation
coverage. No bucket lifecycle policy or source deletion is part of the mode switch.

### Optional cloud-access grants

Cloud access ships disabled (`AGENT_CLOUD_ACCESS` empty) through the API-only pipeline. Preserve
worker pins and existing settings. Activating it requires a separate operator-reviewed
`infra/mica-staging` apply for the access service account, role-limited IAM binding, control
impersonation and bundle configuration. Routine application releases never apply this IAM.
See [cloud-access activation and live checks](cloud-access.md#operator-activation), including
how to deny a Slack test, approve an email test, and remove the temporary test roles.

The API-only pipeline takes `_API_BASE` (a verified API runtime digest), `_SOURCE_COMMIT`,
`_API_IMAGE`, and `_WORKER_IMAGE` (the unchanged shared worker digest). It uses the same
application overlay as coordinated releases so attachment, conversation and transfer support
remain installed. Reuse the verified runtime base from the release inputs rather than adding
unbounded layers to successive application images. Headless API, source/attachment, transfer
and API/worker contract checks must all pass before promotion.

Memory-editor context and retry fixes are API-only releases; preserve all worker image pins.
Incident recovery and notification-policy changes are also API-only. The default
`AGENT_FAILURE_ALERT_MODE=actionable` keeps recovered retries in diagnostic storage and posts
unresolved problems; `all` restores the full stream. Preserve the independent uptime/Scheduler
alerts. Reconcile historical cards only from verified receipts/log evidence, preserving counts
and manual-resolution fields. Do not synthesize a failure or send a test Slack message for this
release. See [failure notification policy](failure-log.md) for grace periods and recovery checks.
Pending conversational saves without accepted changes upgrade their cached input on the next
dispatch. For recovery, dispatch affected memory tasks individually and verify both the durable
thread receipt and `memory_editor_done`; a successful HTTP response alone is insufficient.
Do not delete pending caches, regenerate accepted proposals, or mark incidents resolved merely
because the new API is healthy. A `context_limit` requires a reviewed writer-budget or memory
layout adjustment; increasing router context does not change the writer budget.
