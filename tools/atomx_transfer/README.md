# atomx-transfer

Standalone implementation of the pinned upstream repository's M1–M3 plan. This package is
installed only in the transfer image. It is kept here as a reviewed overlay until published
upstream; the image records both the original commit and the overlay hash.

```bash
python -m pip install ./tools/atomx_transfer
atomx-transfer inventory --config project.yaml --export-root /MyExport --publish
atomx-transfer pull --config project.yaml --export-root /MyExport --sample tma37 --as TMA37
atomx-transfer verify --config project.yaml --sample TMA37
```

Manual commands prompt for a login (the terminal password is hidden); `pull` prints counts and
asks for confirmation. Agent jobs instead read an exact Secret Manager version from their GCS
request and use `atomx-transfer job --request gs://…/tasks/transfers/<run_id>/pull-request.json`.
No password is accepted in that request, a command argument, or an environment variable.

The layout matches the original scripts: morphology images, the channel dictionary, and flat
files go to `raw_outputs/raw/<sample>/`; the remaining decoded run files go to
`raw_outputs/additional_files/<sample>/`. The latest timestamped run is selected. Aliases change
only the destination sample name. Arbitrary layout mappings are not supported in this release.
Symlinks and special files are rejected. SSH host keys must be pinned before use.

Transfers use generation-zero uploads and compare all files by size and crc32c. Identical files
are read from SFTP for their checksum and skipped in GCS; conflicting files are never replaced.
Source listing changes and extra destination objects prevent a done marker. The inventory is a
metadata snapshot: equal remote sizes alone cannot detect same-size source edits. Use a transfer
or checksum verification appropriate to the source to establish content equality.

The proposed marker schema is in `schema/marker-v1.json`; offline fixtures and tests are in
Mica's `tests/fixtures/atomx_markers/`, `tests/test_atomx_engine.py`, and
`tests/test_analysis_transfers.py`. The fixtures are synthetic, not a verified real lab sample.
An independently verified host key, scratch-bucket canary, and live IAM/NAT checks are still
required before lab rollout. See `docs/slack-agent-setup.md#transfers` in the Mica repository.
