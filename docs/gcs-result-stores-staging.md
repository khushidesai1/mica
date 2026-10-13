# GCS result-store staging verification

## Release

Source `a458e3b7f965661be0a56e60695884e3a786637f` was deployed on October 13, 2026
at 17:39 UTC as `pistachio-staging-stores-a458e3b7f965`, serving 100% of staging traffic.
[PR #3](https://github.com/khushidesai1/pistachio/pull/3) contains the implementation.

- Ruff (`src tests tools`) and strict mypy passed; **727 tests passed, 6 skipped**.
- [GitHub CI](https://github.com/khushidesai1/pistachio/actions/runs/38439291332) passed.
- Cloud Build `34c06506-3463-430e-951e-c923e173ded3` passed all ten steps, including
  source/attachment checks, worker compatibility, Linux telemetry and checkpoint checks,
  and synthetic spatial estimation, ingest, and Cellpose checks.
- Zero-traffic preview and live health returned 200. Removed UI endpoints returned 404;
  unsigned ingress and unauthenticated internal endpoints, including GCS publishing,
  returned 401.
- Promotion occurred with no active events or jobs; the task queue was resumed. Only
  the coordinated API/worker image references changed. IAM, secrets, grants, and other
  service settings were preserved.

## Behavioral verification

Offline tests publish 601 files while simulating a file above the previous 512 MiB limit.
They verify all relative paths and eight-transfer concurrency, reject 5,001 files and
excessive per-file/total bytes before staging, and enforce cumulative limits across
separate mid-turn calls and worker replacement.

Other tests cover completed publications surviving a later error, end-of-turn deduplication,
immutable staging after a lost acknowledgment, revocation during a store upload, existing
object protection, traversal/symlink rejection, and tool registration only with a grant.
A resumed thread receives its current application-set grant despite an earlier “no grant”
statement. The log includes the event, abbreviated grant ID, and URI without file content.

The credential test uses the real impersonated/downscoped credential classes and
AuthorizedSession with a fake clock and transport: chunk requests beyond 300 seconds
refresh both credentials and complete. A separate deadline test aborts without committing.
Multi-gigabyte and long-duration behavior was simulated, not exercised with a large live upload.

## Live store publish

**Passed** for event `f6b0ad591ce891239128f83ff3b6104d`.
The canary posts a clearly labeled bot message and submits a corresponding signed test event
for the authorized user; it exercises the deployed router, actual model, worker, publisher,
and Slack delivery. It is not a manually typed Slack message.

Source: `gs://alchemy-breast-cancer/resegmentation/TMA1/tables/` (287 objects,
61,900,890 bytes). Destination:
`gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-764fabc0/tables/`.
The existing grant is `b6631f95…`, scoped to the project bucket. Source data is unchanged.
The smoke copy is left for Khushi to delete after verification.

- Worker provenance records `mcp__pistachio__publish_gcs` at 17:44:04 UTC with folder
  `outputs/gcs/resegmentation/_publish_smoke/20261013-764fabc0`.
- Recursive `gcloud storage ls -r` lists all 287 destination objects. Every object's
  size and CRC32C matches its source; no extra or missing files were found.
- Every object has `pistachio-event=f6b0ad591ce891239128f83ff3b6104d` and the expected
  `pistachio-grant` metadata. The event's publication ledger confirms all 287 URIs.
- The worker's saved prompt contains the current grant. Its saved reply includes the
  actual GCS destination, and the exchange completed successfully without delivery errors.
- The live request manifest contains all four new limits at their expected defaults.

Worker log at 17:43:18 UTC:

```text
write_access event=f6b0ad591ce891239128f83ff3b6104d grant=b6631f95 uri=gs://alchemy-breast-cancer/
```

See [GCS write operations](slack-write-access.md) for staging layout, limits, retry behavior,
and grant management. Keeping large intermediate outputs in workspace checkpoints remains
outside this change; publish each completed store before beginning another expensive step.
