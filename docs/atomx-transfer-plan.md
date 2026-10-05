# Plan: `atomx-transfer`, verified AtoMx → GCS transfers, plus a Pistachio transfer skill

**Summary.** Replace the hand-run `get_tma.sh` / `get_tma_addfiles.sh` scripts on `harjaisal-mini`
with a small, verified transfer tool in its own repo, `azizilab/atomx-transfer`.

- For each TMA it pulls an AtoMx SFTP export into the project's bucket layout, checks every file
  by size and checksum, and writes the `transfer_status/` markers that Pistachio's trigger reads
  ([background-triggers-plan.md §5.0](background-triggers-plan.md)).
- Pistachio gets a **transfer skill**: it can answer "what's transferred / in progress / still on
  AtoMx?" and, on an explicit confirmed command, queue a transfer for the VM to run.
- Pistachio never holds the AtoMx password and never runs SFTP itself.

This doc lives in the Pistachio repo until the new repo exists. Parts A and C then move there.
Part B stays here.

---

## 0. Read first

1. **Two repos, one contract.** The tool lives in `azizilab/atomx-transfer`. Pistachio does **not**
   import it (no private-repo build access, no version coupling). The contract between them is the
   versioned marker JSON (§3). Both repos keep the same golden fixture files and test against them.
2. **Credentials stay on the transfer host.** The AtoMx SFTP password and the bucket-write identity
   exist only where the tool runs. Pistachio only reads markers and writes *requests* (§5.2).
3. **Never overwrite lab data.** Uploads are create-only (`if_generation_match=0`). An existing
   object with the same size and crc32c is skipped. A different existing object is an error.
4. **Keep today's bucket layout exactly** (verified for all 36 TMAs, §1). Fix only the two
   inconsistencies: no nested re-upload copies, and no run-folder wrapper in `additional_files`.
5. **Ask the human before any cloud or VM action:** creating the repo, the service account, Secret
   Manager secrets, VM service-account or scope changes (these need a VM stop), systemd installs,
   and the first real pull.

---

## 1. Context (verified 2026-10-05 on `harjaisal-mini`, read-only)

- **VM.** `harjaisal-mini`, `us-central1-f`, `e2-medium`, 15 TB boot disk (1.3 TB used, mostly
  staged TMA folders under `/home/khushidesai` and `/home/kpd2137`).
  - No external IP. Outbound SFTP works through the `default` network's Cloud NAT.
  - Attached SA: the default compute SA with the **`devstorage.read_only`** scope. Uploads today
    therefore use the human's own `gcloud` login (`kpd2137@columbia.edu`).
  - It is a shared VM with 36 user home directories.
- **Source.** AtoMx export SFTP `na.export.atomx.nanostring.com:22`, user `kpd2137@columbia.edu`
  (a personal AtoMx account). The password is in `~/.tma_sftp_password` (mode 600) and passed via
  `sshpass`. Host-key policy is `StrictHostKeyChecking=accept-new`.
- **Remote layout** (from `get_tma.sh` / `get_tma_addfiles.sh`):

  ```
  /<EXPORT_ROOT>/                       e.g. /TMA29_311_25_08_2026_12_46_04_632 (several TMAs)
    DecodedFiles/<tma>/<YYYYMMDD_HHMMSS_S#>/   run folder (latest picked if several)
      CellStatsDir/Morphology2D/*.TIF
      RunSummary/Morphology_ChannelID_Dictionary.txt
      … everything else (AnalysisResults, CellStatsDir, RunSummary, …)
    flatFiles/<tma>/                    *_tx_file.csv.gz, *_fov_positions_file.csv.gz, …
  ```
- **Mapping into the bucket** (`gs://alchemy-breast-cancer/raw_outputs/`):
  - `raw/<T>/` = `Morphology2D/` + `Morphology_ChannelID_Dictionary.txt` + `flatFiles/<tma>/*`.
  - `additional_files/<T>/` = the run folder minus those two.
  - `<T>` may differ from the remote `<tma>` (the scripts take a `gcs_dest_name`).
- **Gaps in today's scripts:**
  - "SUCCESS" means only that `sftp` and `gcloud storage cp` exited 0. Nothing compares sizes or
    checksums.
  - `cp -r` into an existing destination nests a second copy, which caused the `TMA6/TMA6/`
    duplicate.
  - Older versions uploaded the run folder itself, which is why some `additional_files/<T>/` have
    a `<run>_S#/` wrapper and others don't.
  - raw and additional files run as separate jobs, often days apart.
  - Local staging is never cleaned up.
  - Notifications go to a Slack incoming webhook (`~/.slack_tma_webhook`).
- **Other projects use the same path:** `/home/kpd2137/HumanTonsilAmici_22_07_2026_13_08_13_782`
  is a non-alchemy AtoMx export. That is the reason for a reusable tool.
- **Pistachio today.** The compute runner loads no skills (`setting_sources=[]`,
  `agents/analysis/runner.py:141`) and its sandbox has no network or credentials. Deterministic
  Slack commands live in `agents/analysis/commands.py` (`share`, `publish`, …, `:87-160`).
- **Not verified:** whether the AtoMx SFTP server exposes checksums. Assume sizes only on the
  SFTP side; crc32c covers the GCS side.

---

## 2. Decisions

- **Separate repo `azizilab/atomx-transfer`** (Python package plus CLI):
  - It runs on a different machine with different credentials than Pistachio.
  - It serves any AtoMx project, not just alchemy.
  - People can use it without Pistachio.
  - It is scoped to *AtoMx exports → GCS*. It is not a generic SFTP tool.
- **Per-project YAML config** in that repo (`projects/<name>.yaml`) defines the bucket, the layout
  templates and the name mapping. Adding a project means adding a file.
- **One transfer = one TMA = raw + additional files together.** Write the `done` marker only when
  both are verified, because the trigger needs both.
- **Pistachio starts transfers only through an explicit, confirmed Slack command**, never on the
  model's own initiative. The "skill" is knowledge plus a status view plus a request command.

---

## 3. Marker contract v1 (shared; the source of truth is `atomx-transfer/schema/marker-v1.json`)

Prefix `transfer_status/` in the project bucket. `<T>` is the bucket sample name.

| Object | Written by | Meaning |
|---|---|---|
| `<T>.in_progress.json` | tool, create-only at start; `heartbeat` refreshed every 5 min (generation-matched rewrite) | transfer running |
| `<T>.done.json` | tool, create-only after full verification | bucket matches the export |
| `<T>.failed.json` | tool | gave up; `step`, `error`; a successful re-run deletes it |
| `requests/<id>.json` | Pistachio (§5.2) | asks the transfer service to run one transfer |
| `requests/<id>.result.json` | tool | `accepted | rejected | done | failed` + reason |
| `_inventory.json` | tool (`inventory`, hourly) | what the AtoMx export has, per TMA |

Common fields:

```
version: 1
sample (<T>), remote_tma, export_root, run_folder, project
by: user email or "service:<host>"
started, heartbeat, finished
```

`done` adds:

- `manifest`: one entry `{name, size, crc32c}` per object, with names relative to the bucket.
- `n_files`, `total_bytes`.
- `source`: `{n_files, total_bytes}` from the SFTP listing.
- `n_fovs` (from the FOV-positions file).
- `tool_version`.

Pistachio verifies `manifest` against a live listing (background-triggers §5.0). It uses
`heartbeat` (not `started`) for the stuck rule: stuck = no heartbeat for 60 minutes.

---

## 4. Part A: the tool (`azizilab/atomx-transfer`)

### 4.1 CLI

- **`atomx-transfer inventory --project alchemy`**
  - Recursively lists export roots and TMAs over SFTP: run folders, file counts, bytes.
  - Joins that with the bucket markers to report each TMA as `on_atomx_only`, `in_progress`,
    `done`, `failed`, `changed_since_done` (the source has different sizes or counts than the
    done manifest) or `bucket_only`.
  - Prints a table and, with `--publish`, writes `transfer_status/_inventory.json`.
- **`atomx-transfer pull --project alchemy --export-root /… --tma tma37 [--sample tma37]
  [--keep-local] [--force]`**
  1. **Claim.** Create `in_progress` (create-only). Refuse if `done` exists (unless `--force`) or
     if a live `in_progress` exists (heartbeat < 60 min).
  2. **Plan.** List the remote run folder(s) and `flatFiles/<tma>/` with sizes.
     - Pick the latest run folder. If there are several, record all of them and warn.
     - Build the destination object name for every file from the layout templates. This is a
       deterministic per-file mapping, never `cp -r`.
  3. **Download.** Use paramiko SFTP into the staging dir `<staging>/<project>/<T>/`, resumable:
     skip a file whose local size equals the remote size. Verify local size against remote size
     for every file.
  4. **Upload.** For each file, compute crc32c locally.
     - If the destination exists with the same size and crc32c, skip it.
     - If it exists and differs, stop with an error.
     - Otherwise upload create-only and compare the returned crc32c.
  5. **Verify.** List both destination prefixes. The listing must equal the plan exactly (names,
     sizes, crc32c), with no extras.
  6. **Done.** Read the FOV count. Write `done` with the manifest, delete `in_progress` and any
     stale `failed`, and delete local staging unless `--keep-local`.

  On any error, write `failed` with the step and error, leave `in_progress` in place, and exit
  non-zero. Re-running resumes.
- **`atomx-transfer verify --project alchemy --sample T`:** read-only re-check of a `done` marker.
- **`atomx-transfer serve --project alchemy`** (systemd service; §4.3):
  - Polls `transfer_status/requests/` every 60 s and runs one request at a time through `pull`.
  - Validates each request against the project config: known project, an export root that
    exists, and a TMA present in it.
  - Writes `<id>.result.json`.
  - Runs `inventory --publish` hourly.
- **`atomx-transfer backfill-markers --sample T`** (optional, ask first): for the 36 existing TMAs,
  writes a `done` marker from the current bucket listing, marked `backfilled: true` with no source
  sizes. It is not needed for the trigger, because existing TMAs are skipped.

### 4.2 Project config (`projects/alchemy.yaml`)

```yaml
project: alchemy
bucket: alchemy-breast-cancer
status_prefix: transfer_status/
sftp: {host: na.export.atomx.nanostring.com, port: 22, user: kpd2137@columbia.edu,
       host_key: "ssh-ed25519 AAAA…",            # pinned; capture once with ssh-keyscan + human check
       password_secret: projects/azizilab-aml/secrets/atomx-sftp-password}  # or env/file for manual use
layout:
  raw:
    - {from: "{run}/CellStatsDir/Morphology2D/", to: "raw_outputs/raw/{sample}/Morphology2D/"}
    - {from: "{run}/RunSummary/Morphology_ChannelID_Dictionary.txt", to: "raw_outputs/raw/{sample}/"}
    - {from: "{export_root}/flatFiles/{tma}/", to: "raw_outputs/raw/{sample}/"}
  additional_files:
    - {from: "{run}/", to: "raw_outputs/additional_files/{sample}/",
       exclude: ["CellStatsDir/Morphology2D/**", "RunSummary/Morphology_ChannelID_Dictionary.txt"]}
sample_name: "{tma}"        # override per request with --sample
staging: /srv/atomx-transfer/staging
```

Golden test: a fake SFTP tree shaped like `tma36` must map to exactly the object names in today's
`raw_outputs/raw/tma36/` and `raw_outputs/additional_files/tma36/`. Build the fixture from a
bucket listing.

### 4.3 Running it

- **Phase 1 (now): manual on `harjaisal-mini`** under the human's own login, exactly as today.
  The only change is that the scripts become `atomx-transfer pull`. Markers work with any
  identity.
- **Phase 2: unattended `serve`.** Requests need an always-on identity:
  - Create SA `atomx-transfer@azizilab-aml` with:
    - `objectCreator` + `objectViewer` on `alchemy-breast-cancer`, conditioned to
      `raw_outputs/`.
    - `objectAdmin` conditioned to `transfer_status/`.
    - `secretAccessor` on `atomx-sftp-password`.
  - Run `serve` as a systemd service under a dedicated system user with
    `/srv/atomx-transfer/staging`.
  - Prefer a **small dedicated VM** (`e2-small`, 2-4 TB `pd-balanced` staging disk, no external
    IP, same NAT) over changing `harjaisal-mini`. Changing that VM's SA and scopes requires
    stopping a shared machine with 36 users. Ask the human.
  - Move the SFTP password into Secret Manager.
  - Consider asking NanoString for a lab or service AtoMx export login instead of a personal one.

---

## 5. Part B: Pistachio integration (this repo)

### 5.1 Read side (extends background-triggers §5.0)

- `triggers/transfers.py` reads markers and `_inventory.json` per the v1 contract. Copy the
  golden fixtures from `atomx-transfer/tests/fixtures/markers/` and test against them.
- Use `heartbeat` for the stuck rule (60 min without a heartbeat, plus a 24 h fallback when
  there is no heartbeat field).
- New trigger notice when a `done` marker verifies: "TMA37 transfer verified (9,112 files,
  98 GB). Segmentation started." This replaces the old Slack webhook messages. `failed` and
  stuck notices go to the same project destinations.

### 5.2 The transfer skill (Slack, control plane only, never in the sandboxed worker)

- **Knowledge.** When a project has a transfer config (an admin-defined
  `TRANSFER_PROJECTS = {"p_05b107ac9f31b40b": {"bucket": "alchemy-breast-cancer", …}}` next to
  `TRIGGERS`), `direct.handle` adds to the conversation context:
  - A compact transfer summary: per-TMA state from markers plus `_inventory.json`, with ages.
  - A short "transfers" instruction block.

  So "what's transferred?", "is TMA37 done?" and "what's still on AtoMx?" are answered directly
  with no compute worker.
  - The model may *suggest* the exact command.
  - It cannot start a transfer itself: the `route_request` schema gets no transfer route.
- **Command.** In `commands.py`, alongside `share`/`publish`. Owner or member of a
  transfer-enabled project only:
  1. `transfer <tma> [as <sample>] [from <export_root>]`
     - Pistachio resolves the export root from `_inventory.json` when it is omitted. Ambiguous →
       list the options.
     - It replies with a preview: export root, run folder, file count, bytes, destination
       prefixes, and whether a `done`/`in_progress` already exists. It then asks for
       `confirm transfer <tma>` within 10 minutes.
  2. `confirm transfer <tma>`: writes `transfer_status/requests/<id>.json`
     (`{version, project, export_root, tma, sample, requested_by, slack_ts}`, create-only) and
     replies "Queued. I'll post when it's verified."
  3. `transfers`: status table (the same data as the knowledge context).
  4. `cancel transfer <tma>`: writes `requests/<id>.cancel`. `serve` honors it between files.
- **Permissions.** The control SA gets `objectCreator` on `transfer_status/requests/` and
  `objectViewer` on `transfer_status/`. It gets nothing on `raw_outputs/` beyond the existing
  project-level read. Requests are validated again by `serve` (§4.1), so a forged request object
  can at most start a transfer that is valid under the project config.
- **No Agent SDK skill in the compute worker.** It has no network or credentials by design, and
  transfers don't need computation. If agent skills are added to the worker later, a read-only
  "transfer status" skill could reuse §5.1. Not now.

---

## 6. Milestones

**atomx-transfer repo**

- **T0:** Repo skeleton, `schema/marker-v1.json`, golden marker fixtures, and the layout golden
  test from the `tma36` listing (failing first).
- **T1:** `inventory` and `pull` against an in-memory fake SFTP and a fake GCS. Tests for resume,
  create-only, the existing-same skip, existing-different failure, the extra-object failure,
  heartbeat, and the `failed` marker.
- **T2:** The first real manual `pull` of the next new TMA on `harjaisal-mini`, with approval.
  Compare against the old scripts' behavior. Run `verify`.
- **T3:** `serve` plus systemd unit, then SA, secret and VM decisions (Phase 2), with approval.

**Pistachio** (after background-triggers M1)

- **P1:** `transfers.py` reader on the shared fixtures, heartbeat stuck rule, "transfer verified"
  notice.
- **P2:** Knowledge context in `direct.handle` and the `transfers` status command.
- **P3:** `transfer` / `confirm transfer` / `cancel transfer`, request writes, and IAM
  (Terraform, gated, `plan` only).

---

## 7. Open questions for the human

1. **Where should `serve` run?** On `harjaisal-mini` (stop it to change the SA and scopes) or on a
   small dedicated VM? The recommendation is a dedicated VM.
2. **Which AtoMx login should automation use?** Yours, or a lab or service login from NanoString?
3. **Local staging after a verified upload:** delete it by default (recommended; 1.3 TB is
   currently sitting on the VM) or keep it?
4. **Who may run `transfer` in Slack:** project owner only, or members too?
