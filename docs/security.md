# Security and operations

The versioned Cellpose tool uses a dedicated networkless, PID-isolated process with its selected
input read-only and a new output folder writable. Only an approved GPU worker supplies the
allocated NVIDIA devices. GPU confirmation binds the requester, project, T4/L4 model, image, duration, retry
terms and estimated cost; these are checked again at submission. See [Cellpose tooling](cellpose.md).

V1 is for private, non-PHI research data. Do not use it for PHI until a separate compliance and
threat-model review is complete.

## Controls

- Secrets live in Secret Manager and never in `RunContextWrapper`, prompts, execution bundles, or
  generated-code environments.
- SDK tracing is disabled by default. Application logs should contain IDs, state transitions,
  duration, and byte counts—not prompts, datasets, code, or results.
- Slack signatures have a five-minute replay window. Gmail, Cloud Tasks, Batch completions, and
  compaction operations have durable idempotency keys.
- Source repositories are data. Their instruction files cannot override the harness, and only exact
  approved commits enter builds. GitHub App permissions must be metadata and contents-read.
- GPU, >64 GiB RAM, >6 hours, or compute estimates above $20 including contingency require approval.

## Incident recovery

1. Disable channel ingress or set Cloud Run max instances to zero if active compromise is suspected.
2. Revoke Slack/Gmail/GitHub credentials and rotate Secret Manager versions.
3. Cancel queued Cloud Tasks and Batch jobs; retain task IDs and immutable GCS generations.
4. Export affected Firestore task, approval, memory, and event records. Do not edit audit artifacts.
5. Rebuild images from locked commits, scan, smoke-test, and deploy by digest.
6. Reconcile `QUEUED`/`RUNNING` tasks against Batch and completion-topic history before reopening.

Artifact bucket lifecycle removes unpinned objects after 30 days. Pinning copies an object to the
versioned pinned bucket. Budget alerts are warnings; configure an organization-level billing policy
if a hard spend stop is required.


## Operator failure summaries

The [failure log](failure-log.md) stores fixed operational cause codes and bounded correlation metadata
in Firestore, and renders summaries into a private operator Slack channel. Project names, IDs and the
latest verified requester's email are intentional operator-visible metadata. Restrict channel membership
accordingly. No message bodies, raw exceptions, model output, filenames, SFTP credentials or private
connection/approval links are copied into incidents. Worker access and credentials are unchanged.

Optional infrastructure incident ingestion accepts authenticated Cloud Run log exports only for its
own configured project and service. The Pub/Sub topic carries original log entries temporarily;
Firestore/Slack receive only fixed route templates, status, service/revision, hashed occurrence IDs
and counts. Request bodies, arbitrary URLs/queries and exception payloads are not copied into the
incident index. The additive log-sink infrastructure requires a separate reviewed operator apply.
See [failure log](failure-log.md) for scope, retention and outage limitations.
