# Approved GCS overwrites: staging verification

Source `52033ddec6676d4195cba07af8c07922ae7f90f2` was deployed on October 13,
2026 as `pistachio-staging-overwrite-52033ddec667`, serving 100% of staging traffic.
[PR #5](https://github.com/khushidesai1/pistachio/pull/5) contains the implementation.

Ruff and strict mypy passed; **787 tests passed, 6 skipped**.
[GitHub CI](https://github.com/khushidesai1/pistachio/actions/runs/38481726142)
passed. Cloud Build `710a3771-af7e-45ca-87bf-7051ecaf8d41` passed all ten gates,
including source checks, attachments, worker compatibility, and spatial runtime checks.
Preview and live health returned 200; unsigned Slack events/actions and unauthenticated
internal endpoints returned 401. Promotion occurred with no active events or workers,
and the task queue was resumed. Other settings and secret mappings were preserved.

## Existing grant and cloud permissions

The dedicated `pistachio_staging_output_writer` custom role contains exactly
`storage.objects.create`, `storage.objects.get`, and `storage.objects.delete`.
The sole added permission is required by Cloud Storage to replace an object. Both existing
bucket policies matched their saved originals; grant scopes, bindings, and worker permissions
were unchanged. No Terraform apply or secret changes were needed.

Approved replacements use a short-lived credential restricted to one exact object.
The controller pins its approved generation and records the replaced generation in an audit
receipt. Workers cannot edit approvals or obtain writer credentials. The same write grant
covers new files and confirmed replacements; there is no separate overwrite grant or delete tool.

## Live approval and replacement

Event `4d803fb2ddd29088bf249fc816939d2d` replaced the disposable file
`gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-overwrite-retry/report.txt`.

- Slack showed the exact target, one existing file, and **Approve and run** / **Cancel**.
  No worker or worker manifest existed before confirmation.
- The signed callback persisted approval of generation `1791932916937276`.
- The actual worker published exactly `after overwrite canary` plus a newline: **23 bytes**.
  The destination's new generation was `1791933478232360`.
- The controller receipt recorded the approved old generation. `untouched.txt` retained both
  its contents and generation, and no extra objects appeared.
- Slack delivered the successful reply and one GCS receipt with zero errors. The memory editor
  completed and the thread contained no automatic “Remembered for this project” notice.

The check used a labeled bot message and synthetic signed event/callback for the authorized
requester, rather than a manually typed message or human click. One callback overlapped a status
poll and left approval pending. The observer was corrected to check durable approval instead of
assuming HTTP 200 meant approval; a retry persisted approval. No grant, approval record, or
controller event was edited directly.

## Failures caught and regression coverage

The initial image build exposed an obsolete help assertion requiring “new files only”; the
check now requires overwrite guidance and rejects the old wording. No release gate was bypassed.

The first live publication, event `d4750a12ad22ddc45ebee4afaddb7606`, failed with HTTP 400 and
left both test files unchanged. Reading destination metadata had cached its old CRC32C on the
upload handle; the SDK reused that checksum for the replacement. Separate read and upload
handles prevent this, and each reconciliation reads the latest generation. A regression using
the real storage SDK failed on the old serialized checksum and passes after the fix. Worker
context now also states that the requester confirmed through Slack before the run started.

Other tests cover cancellation, expiry, wrong actors/messages, duplicate clicks, access changes,
revocation, snapshot pagination/limits/timeouts, invalid paths, changed/deleted versions,
late collisions, retries, and exact-object credential boundaries. New files need no confirmation.

## Cleanup

Both exact prefixes were deleted after successful verification and individually confirmed absent:

- `gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-overwrite/`
- `gs://alchemy-breast-cancer/resegmentation/_publish_smoke/20261013-overwrite-retry/`

Project inputs were unchanged. Protected approval and publication receipts remain as audit records.
See [GCS write operations](slack-write-access.md) for usage and configuration.
