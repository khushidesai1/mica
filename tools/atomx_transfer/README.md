# atomx-transfer

Standalone implementation of the pinned upstream repository's M1–M3 plan. This package is
installed only in the transfer image. It is kept here as a reviewed overlay until published
upstream; the image records both the original commit and the overlay hash.

```bash
python -m pip install ./tools/atomx_transfer
atomx-transfer inventory --config project.yaml --export-root /MyExport --publish
atomx-transfer pull --config project.yaml --export-root /MyExport --sample tma37 --as TMA37
atomx-transfer verify --config project.yaml --sample TMA37
atomx-transfer backfill --config project.yaml --dry-run
atomx-transfer backfill --config project.yaml --sample TMA37 --yes
```

`inventory` and `pull` prompt for a login (the terminal password is hidden); `pull` prints counts and
asks for confirmation. Agent jobs instead read an exact Secret Manager version from their GCS
request and use `atomx-transfer job --request gs://…/tasks/transfers/<run_id>/pull-request.json`.
No password is accepted in that request, a command argument, or an environment variable.
`verify` and `backfill` use GCS credentials only and never open an SFTP connection.

The layout matches the original scripts: morphology images, the channel dictionary, and flat
files go to `raw_outputs/raw/<sample>/`; the remaining decoded run files go to
`raw_outputs/additional_files/<sample>/`. The latest timestamped run is selected, directly under
the slide folder or one level below it. Aliases change only the destination sample name. Arbitrary
layout mappings are not supported in this release.

Inventory without `--export-root` searches up to three levels below `/` for folders containing
`DecodedFiles` (at most 400 listings). A requested sample matches slide folders by
spelling-insensitive name (case, separators, leading zeros, bare numbers, appended AtoMx
timestamps), or a lone slide folder inside an export named for the sample. Results include a
`searched` summary of the export and slide folders seen, and matching folders that cannot be
planned are reported as `incomplete` with a fixed `problem` string.
Symlinks and special files are rejected. SSH host keys must be pinned before use.

Transfers use generation-zero uploads and compare all files by size and crc32c. Identical files
are read from SFTP for their checksum and skipped in GCS; conflicting files are never replaced.
Source listing changes and extra destination objects prevent a done marker. The inventory is a
metadata snapshot: equal remote sizes alone cannot detect same-size source edits. Use a transfer
or checksum verification appropriate to the source to establish content equality.

Job progress distinguishes `listing`, `downloading`, `checking_downloads`, `uploading`, and
`verifying` before `complete`. `download_bytes` includes the partial local file, while
`download_files` counts files whose local size matches the plan. `upload_bytes` includes blocks
consumed by the destination stream (or checked against existing objects); only `files_done`
and `bytes_done` certify size/CRC32C-verified objects. Progress is published at most once a
minute plus phase changes. The metadata and completion checks remain required even when a
byte counter reaches 100%.

## Completion markers

`done.txt` in the destination sample folders determines whether a new pull is blocked.
`transfer_status/<sample>.done.json` retains detailed completion evidence. After verifying all
data, a transfer creates `done.txt` in both `raw_outputs/raw/<sample>/` and
`raw_outputs/additional_files/<sample>/`. Each text marker describes completion of the entire
sample, with per-folder and sample counts. JSON is written first; a missing text marker can be
repaired with `backfill`. Text markers are create-only and never overwritten. Existing text that
differs from the completion record is rejected, including during repair.

After moving both defective sample folders, a new approved pull may reuse the original name.
An old JSON record without destination text markers is historical evidence, not a transfer lock.
The new pull rechecks source size/CRC32C and the exact destination manifest before replacing the
JSON with a generation precondition. Its `previous_completion` retains the prior generation and
full record (including earlier history). The new text markers identify the new run. Moved text
markers can be associated with that history by `run_id`; moving folders does not rewrite history.
Either remaining text marker blocks a pull, and active leases still take precedence.

Text markers start with `# AtoMx transfer marker v1`, followed by `key: value` lines. A blank line
ends the metadata; the remaining prose is not machine-readable. Keys include `sample`,
`remote_sample`, `scope`, `status`, `verified`, `export_root`, `run_folder`, `scope_files`,
`scope_bytes`, `sample_files`, `sample_bytes`, `finished`, `tool_version`, `run_id`, `requested_by`,
and `record` (the JSON evidence path). `verified: crc32c` records source comparison at
transfer completion. It is not a claim that the source or destination has remained unchanged.

The basename `done.txt` is reserved anywhere in either destination tree. Source files with this
name are rejected, and marker objects are excluded from destination checksum verification.
`verify` compares current GCS objects with the saved checksum manifest, without contacting SFTP.

## Legacy backfill

`backfill` discovers sample folders with delimiter listings, without recursively listing all
samples' contents. It defaults to a dry run; only `--yes` writes. `--sample` limits the operation.
The JSON summary lists `marked`, `repaired`, and `skipped` samples; dry-run lists describe planned
actions. `--dry-run` and `--yes` are mutually exclusive.

For legacy samples without a completion record, backfill records a bucket listing snapshot in
JSON (`provenance: backfill`, per-scope counts, no manifest or source fingerprint), then writes
text only in the folders present at that time. Text reports `verified: listing-only (backfilled)`,
`source_checked: no`, `backfilled_at`, unknown source fields, and `additional_files: absent` or
`raw: absent` when applicable. These counts do not prove that copying finished or matches AtoMx.
Run this only after the old shell copy jobs have stopped; they do not honor the engine's lease.

Repairs use the existing JSON's original counts and verification level. A later-arriving folder
does not silently extend a backfilled snapshot. Before restoring missing text, repair checks the
current manifest (or listing-only counts) and skips changed destinations as `destination_changed`.
Re-running skips fully marked samples without
changing object generations. Backfill respects the per-sample lease and skips unfinished or
failed transfers without a done record, and text-only markers with no authoritative JSON.
For a completed record, a stale lease can be reclaimed after one hour to repair missing text.

Inventory labels backfilled samples `done_backfilled`, text without a matching-name JSON record
`done_text_only`, and source samples with only a historical record `completion_record_only`.
Bucket-only discovery follows the actual sample folders, including renamed folders.
Slack reports destination marker presence without claiming current content verification.
`verify` exits nonzero for backfilled records unless `--allow-backfilled` is supplied; that
option acknowledges the listing-only record and performs no checksum verification. A real pull
refuses either existing destination text marker. To upgrade, an administrator must first review
the data and remove both text markers, then run a verified pull. Keep the JSON for history.
Review any incomplete transfer or late-arriving folder before removing its markers.

The marker schema is in `schema/marker-v1.json`; synthetic offline tests are in
Mica's `tests/test_atomx_engine.py` and `tests/test_analysis_transfers.py`. No test fixture is a
verified real lab sample.
An independently verified host key, scratch-bucket canary, and live IAM/NAT checks are still
required before lab rollout. See `docs/slack-agent-setup.md#transfers` in the Mica repository.
