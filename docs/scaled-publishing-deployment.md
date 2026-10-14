# Scaled publishing and failed-turn history deployment

## Release

The 14 October 2026 release replaces per-run file and byte caps with batched publication,
resumable GCS server-side rewrites and sharded manifests. Overwrite approval names the requested
folders and records an inspection time. Each replacement still checks the destination generation;
objects created or changed after approval are refused. Slack reports totals per folder.

Successful copies receive durable, generation-specific receipts before staged sources are deleted.
Published files are excluded from workspace checkpoints. Failed exports remain available for
recovery under the existing artifact lifecycle. Bucket soft-delete retention remains seven days,
so deleting an object does not immediately remove its retained storage cost.

Terminal failures now retain their missing conversation exchange once, including original intent
and the failure reason. Routed failures can record concise pending work in project memory.
Cancellations and commands are excluded. Memory edits stay silent in Slack.

Large and xlarge requests retain independent submission leases, without an application cap on
their active count. Standard-worker limits, ordering within a conversation, budgets and GCP quotas
remain unchanged.

## Build and rollout

- Coordinated source `0c8fccd07b96` passed all ten gates in Cloud Build
  `ec8212d8-9b1a-4203-812e-d83ddfe19527` and was promoted after the existing real jobs finished.
  No existing job was cancelled or restarted by the rollout.
- Live checks exposed suppressed rewrite progress logs and transient GCS TLS errors that discarded
  otherwise successful batch responses. Source `8a602055afbf` enables bounded progress logging and
  returns retryable status only for the affected path, retaining successful receipts.
- The compatible API correction passed all four gates in Cloud Build
  `0990a42e-acd9-413e-aa11-24c95a6b14ca`, including the unchanged science worker's execution contract.
  [Exact-source CI](https://github.com/khushidesai1/pistachio/actions/runs/38510016108) passed.
- A later IAM token-service 503 was wrapped as a non-retryable credential refresh error by
  the SDK. Source `76b15bb9bbb1` also classifies structured transient IAM responses, while
  preserving permanent permission failures. Cloud Build `1f259279-f234-4854-bc8f-75d268d388e2`
  passed all four gates and [CI](https://github.com/khushidesai1/pistachio/actions/runs/38512292736) passed.
- Revision `pistachio-staging-refresh-76b15bb9bbb1` serves 100% of staging traffic. Preview and
  live checks passed: health 200, removed UI/docs 404, unsigned Slack/worker requests 401.
  Environment, secret mappings and worker images were preserved, and task dispatch resumed.

Both worker images remain from the coordinated release. The separate, explicitly approved IAM
prerequisite grants the writer only source-object reads under event `write-exports/` prefixes.
Destination grants remain unchanged. No Terraform apply, secret change or quota change was made.

## Validation

Ruff and mypy passed; pytest reported **820 passed, 6 skipped in 45 seconds**. Tests cover 25,000-file publishing,
a simulated 10 GiB object, real SDK request serialization, resumable rewrites, generation races,
receipt reconciliation, source cleanup, per-path transient retries (including token-service
failures), permanent credential errors, and failed-turn history/memory.

Live checks use synthetic signed Slack events with bot-posted verification labels. They exercise
the deployed router, approval callback, worker and memory editor; they are not human-originated
Slack messages.

The 12,000-file overwrite check (`17939d42d8593809620da1120bb34cb7`) was stopped early at the
user's request to keep deployment validation fast. At cancellation, **11,173 replacements**
had new generations and the expected size and CRC32C: 5,958 in `tma17/` and 5,215 in `tma18/`.
The 524-byte approval named both folders and appeared in about 13 seconds; persistence after
the existence probes took 0.24 seconds. The extra object created after approval was rejected,
and its generation and content remained unchanged. Full 12,000-file completion and final Slack
folder totals were **not verified**. Routine releases should use the bounded checks described
in [Deployment](deployment.md#release-validation).

Failed event `6a7bc5001520b7119237a399dcc150ad` retained its original request, failure reason and
empty outputs. Follow-up `819853cff84d4003004f89a255bd61f9` correctly narrowed the previous request
to TMA17 while preserving its method and destination. The controller rejected the deliberately
out-of-grant destination before launching a worker. The memory editor committed pending work
(`23633bb`); cleanup commit `bf0f2fd` removed the marked synthetic section while preserving concurrent
updates to real project notes. The rejected destination was verified empty with `gcloud storage ls`.

The original 10 GiB check (`1554bafda2abd0ed1a389d9cea0341cf`) passed. The progress-logging API repeated
it as event `4a4c517402f6722e1c6a26d994e5393d`: all **10,737,418,240 bytes** reached destination
generation `1791951984179789`, with CRC32C `RXJ2NQ==`. Five rewrite log entries advanced from
2,650,800,128 bytes to `bytes=10737418240 total=10737418240 pending=False`. File bytes remained
in GCS. The verified source was automatically removed and the worker checkpoint was only
**41,530 bytes**. Both disposable 10 GiB destination folders were deleted and verified absent.

After cancellation, the scale worker was confirmed absent. All 12,001 remaining small fixture
objects under `gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261014-limits/`
were deleted. That root and all three test events' staged `write-exports/` prefixes were
verified empty with `gcloud storage ls`. The final memory review found neither the test marker
nor its dated output path. Small run records, receipts and logs remain for audit; existing
bucket soft-delete retention still applies to deleted data.

Rollback to `pistachio-staging-retry-8a602055afbf` preserves the batched worker protocol and
rewrite logging but removes the transient credential-refresh classification. Do not route these
new workers to revisions using the earlier single-file publication protocol.
