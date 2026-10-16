# Plan: Background triggers, starting with automatic TMA resegmentation

**Summary.** Mica gets a small, admin-defined trigger system. The first trigger watches
`gs://alchemy-breast-cancer/raw_outputs/`. When a TMA's `raw/<TMA>/` and `additional_files/<TMA>/`
folders are both complete and have stopped changing, Mica starts one Batch job. That job runs
harjaisal's `sap` CosMx pipeline (ingest → Cellpose → Proseg) and writes the SAP store to
`gs://alchemy-breast-cancer/resegmentation/<TMA>/`. When it finishes, Mica posts a summary and a
per-core QC figure to the project's linked Slack channels, or emails the project's members if no
channel is linked. Failures go to the same place. Users cannot create triggers. They are defined
in code and change only through a reviewed deploy.

---

## 0. Read first

1. **No LLM in the trigger path.** Segmentation is deterministic, pinned code. Intake, checks, the
   job, validation and notifications make no Claude calls. Claude is involved only if a person
   later replies in the posted Slack thread (optional M6).
2. **Never overwrite or delete lab data.** The job's identity can create objects under
   `resegmentation/` but cannot overwrite or delete them (`roles/storage.objectCreator`, prefix
   condition). If `resegmentation/<TMA>/` already has any object, the trigger skips that TMA.
3. **Fail closed and fail loudly.** If the listing is incomplete, a required file is missing, the FOV
   counts disagree, or the output does not validate, then nothing is uploaded and the project gets a
   failure notice. A partial upload is reported as partial, never as success.
4. **Each milestone ends green:** `.venv/bin/pytest -q`, `.venv/bin/ruff check src tests tools`,
   `.venv/bin/mypy src/mica`. Write the milestone's failing tests first.
5. **Ask the human before any cloud action:** image builds, Terraform apply, creating the bucket
   notification, creating a test bucket, deploys, and any job that writes to `alchemy-breast-cancer`.
6. **No new dependencies in the API image** (`docker/Dockerfile.api`). Email uses the stdlib
   `smtplib`. Slack uses the `slack_sdk` already present. All scientific dependencies live only in
   the new segmentation image.
7. **All build inputs are located and pinned** (§7.5, §11): the `sap` source archive, the
   `proseg` wheel, the Cellpose weights and the project ID. Nothing blocks M2.

---

## 1. Context

### What exists (verified)

- **Slack request pipeline.**
  - `receive()` (`api/slack_analysis.py:23`) admits a Slack event.
  - `BatchBridge.accept` (`agents/analysis/batch.py:255`) writes a Firestore receipt and enqueues a
    named Cloud Task (`batch.py:216-253`, 409 = already queued).
  - `BatchBridge.process/advance` (`batch.py:298`, `:350`) poll every 30 s. They claim one of
    `max_concurrent_runs` slots via `begin_event("claude-slot:{n}")` (`batch.py:438-454`), submit
    a Batch job built by `job_spec` (`batch.py:41`), watch it, and release the slot.
  - The trigger system copies this pattern (named Cloud Tasks, Firestore connector state, slots,
    Batch polling) but uses its own keys, slots and job spec. It does not route through
    `BatchBridge`, because that class is built around a Slack payload, a user principal and a thread.
- **Task callbacks are authenticated by OIDC principal.** `process` (`api/slack_analysis.py:114-124`)
  and Gmail push (`connectors/gmail.py:199-205`) reject callers whose principal is not
  `settings.cloud_tasks_service_account`. Push subscriptions and Scheduler jobs send the control
  service account's OIDC token (`infra/main.tf:476-557`).
- **Project registry.** `ProjectEntry` (`agents/analysis/registry.py:35-42`) has `owner`, `members`
  and `published_channels` (keys `"{team}:{channel}"`). It is stored at
  `projects/_registry/registry.json` in the artifact bucket. The trigger's project is
  **`breast-alchemy` = `p_05b107ac9f31b40b`** (`data_uri gs://alchemy-breast-cancer/`, owner
  `kpd2137@columbia.edu`, created 2026-10-05).
  - As of creation it has no members and no `published_channels`; it is bound only to the
    owner's DM. Until a channel is published, §8.1 resolves notifications to email to the owner
    alone.
  - Never notify through a DM binding. A binding is not a publication.
- **IAM.**
  - The control SA has project `storage.objectViewer`, `batch.jobsEditor`, `cloudtasks.enqueuer`
    and `iam.serviceAccountUser` (`infra/main.tf:121-135`).
  - The worker SA has project `storage.objectViewer` (`main.tf:137-148`). The conditioned-prefix
    IAM pattern is in `infra/slack_agent.tf:11-21`.
  - `alchemy-breast-cancer` is in project `azizilab-aml` (same as staging), has uniform
    bucket-level access, is in the `US` multi-region, and currently has no notification configs.
- **Unrelated existing pieces.** The Gmail topic, push subscription, watch renewal and
  `connectors/gmail.py` belong to the legacy web pipeline. Do not touch or reuse them here.

### Bucket layout (verified 2026-10-05)

`raw_outputs/` has `raw/`, `additional_files/` and `anndatas/`. **`anndatas/` is excluded** by
the user's decision; it holds flat `tmaNN.h5ad` files for only 6 TMAs. `raw/` and
`additional_files/` have the same 36 TMA folder names, with mixed case: `TMA26`, `tma36`,
`TMA13rpt`, `AdipokinesRO1TMA101`, and so on.

`raw/<T>/` has the same layout for all 36 TMAs:

```
Morphology2D/<run>_C902_P99_N99_F#####.TIF     one per FOV
Morphology_ChannelID_Dictionary.txt            PanCK, G, Membrane, CD45, DNA
<T>-polygons.csv.gz
<T>_exprMat_file.csv.gz
<T>_fov_positions_file.csv.gz                  header + one row per FOV
<T>_metadata_file.csv.gz
<T>_tx_file.csv.gz
```

The FOV-positions row count equals the TIF count (TMA26: 430/430, TMA1: 120/120, tma36: 476/476).

The `additional_files/<T>/` wrapper varies. TMA26 and TMA1 have a `<run>_S#/` subfolder and
`Logs/`. tma36 has no subfolder and no `Logs/`. These per-FOV signals are stable across all
three and each equals N:

- `**/AnalysisResults/*/FOV#####/*complete_code_cell_target_call_coord.csv`
- `**/CellStatsDir/FOV#####/CellLabels_F#####.tif`
- `**/CellStatsDir/CellComposite/CellComposite_F#####.jpg`
- `**/RunSummary/latest.fovs.csv` (one file)

`resegmentation/<T>/` (the format to reproduce, from TMA26) is a SAP store:

```
images/initial_imaging.ome.zarr/      zarr v3 OME-Zarr: zarr.json + s0..sK pyramid
points/initial_transcripts.parquet
points/proseg_transcripts.parquet
shapes/cellpose_polygons.parquet
shapes/proseg_polygons.parquet
tables/proseg_table.zarr/             AnnData (zarr v3): obs centroid_x/y/z, var gene/…, X
```

All 36 TMAs already have this folder. **`resegmentation/TMA18/images/initial_imaging.ome.zarr`
has no root `zarr.json` and only 214 chunks (TMA26 has 1,816), so it looks partial.** That is a
data issue to raise with the human, not something this plan fixes. It shows why §7's upload
verification matters.

### The `sap` pipeline (verified at `harjaisal/spatial_analysis_pipeline@de96763`)

- `sap.ingest.cosmx(input, store)` reads only `raw/<T>/` and writes `images/` and
  `points/initial_transcripts.parquet`.
  - **It silently fills any missing FOV tile with zeros** (`get_lazy_stitched_image`, where
    `path is None` falls back to `np.zeros`). A half-uploaded TMA would "succeed" with holes in
    it, so the completeness gate is required.
- `sap.segmentation.cellpose(store, nuclei_channels, cyto_channels, diameter=None,
  model_type="cyto3", gpu=True, batch_size=8, chunk_overlap=80, temp_dir=".temp")`
  - It needs a running `dask.distributed` client (`get_client()`) and writes
    `shapes/cellpose_polygons.parquet`.
  - The repo's signature has **no polygons-output argument**, unlike the user's snippet.
- `sap.segmentation.proseg(store, temp_dir=".temp", um_per_px=1.0)` imports
  `from proseg import run_proseg`. That package is not on PyPI. It is harjaisal's
  `proseg-py 3.1.1` binding (source `github.com/harjaisal/proseg-py`), shipped as a wheel at
  `gs://sap-harjaisal-khushi/proseg_py-3.1.1-cp38-abi3-manylinux_2_31_x86_64.whl`.
  - 77,948,778 bytes, sha256 `2df2b4f06fbc01da2276e26d1871843bc2b729447f78c17ba082ed6cd91de7f2`,
    md5 `Ujuu13cJCd32hnTjIrb6VQ==`. It needs glibc ≥ 2.31 on x86_64.
  - `envs/resegmentation.yaml` does not install it.
- Environment: `envs/resegmentation.yaml` (Python 3.12, `cellpose==3.1.1.3`, `ome-zarr==0.13.0`,
  `sopa==2.2.2`, `-e .`).
- `models.Cellpose(model_type="cyto3")` downloads weights at first use. The weights harjaisal used
  are cached at `gs://sap-harjaisal-khushi/.cellpose/models/{cyto3,size_cyto3.npy}`. Bake those
  into the image.

### Core mapping (verified at `azizilab/alchemy_breast_cosmx@cd78d897`, private)

`src/alchemy/merge_map.py` is the lab's downstream core-to-patient mapping. The QC figure must
reuse its logic (§7.4), not invent new logic:

- `label_cores` (`:402-472`):
  - Searches rotations of ±10° in 81 steps, maximizing the squared histogram mass of x and y
    centroid projections.
  - On the rotated coordinates, fits a regular lattice with 1D KMeans: 7 bands in x (rows A–G)
    and 10 in y (columns). Centers come from a linear fit.
  - Assigns each cell to the nearest lattice slot (`core_id = y_idx * 7 + x_idx`).
  - Raises for TMA 1 ("tissue grid is cut off").
- `grid_based_mapping` (`:475-547`):
  - Maps a slot to a label such as `A1`, choosing among 4 flips by scoring map occupancy.
  - Scoring: marker slots should have cells, blank and empty slots should not, and patient slots
    should have at least `max(100, 5% of the median)` cells.
- `parse_tma_core_metadata` (`:239-264`): workbook rows 5–11 are A–G, columns 1–10; parses study
  ID, T/N and slot type (patient, marker, blank or empty).
- `tma_number` (`:154-162`): `tma\s*#?\s*(\d+)`, case-insensitive. **Quirk:** `91` → 9 and
  `101` → 10, so `AdipokinesRO1TMA91` is map TMA 9. `tma_map_file_key` (`:165-177`) picks the
  workbook range.
- It uses the Proseg table's `obs[centroid_x, centroid_y]` (pixels) as `obsm["spatial"]`
  (`:735`).
- The plotting code for the "geometry-mapped physical cores and patient IDs" figure is **not** in
  this repo. §7.4 specifies the plot.
- `configs/reseg_pipeline_*skip_tma1_tma15_tma26*.yaml` exclude TMA1, tma15 and TMA26 from
  downstream analysis. **The current TMA26 output is a known failed segmentation** (the user's
  example figure). Use it as a negative control.

### Cellpose parameters actually used (inferred, verified 2026-10-05)

No run log records them. The only call in either repo is the commented example in `sap`
(`05c6533`, 2026-06-12): `["DNA"]`, `["Membrane","CD45","PanCK"]`, `diameter=80`, `gpu=False`.
The existing outputs fit a fixed diameter near 80 px. Median equivalent diameter of
`shapes/cellpose_polygons.parquet` is 69.3 px for TMA1 (60,772 cells), 66.9 px for tma36
(233,459) and 60.9 px for TMA25 (364,011), which is 7.3–8.3 µm at 0.12028 µm/px. That is evidence,
not proof, so the canary (§12) is the gate.

### Decided with the user

- Admin-defined triggers only, kept to a few. No user-facing commands.
- Completeness means `raw/<T>` and `additional_files/<T>` are complete and not mid-transfer.
  Ignore `anndatas/`.
- Transfers write status markers in `transfer_status/` (`<T>.in_progress.json` →
  `<T>.done.json`). A done marker is authoritative. Inference is only the fallback for unmarked
  hand uploads.
- Run the CosMx parameters from the provided script (§7.2) and write the output in the existing
  `resegmentation/<T>/` format.
- Notify the project's linked Slack channels, or the project's owner and member emails if none
  are linked. Send the QC figure on success. Failures alert the same destinations.
- No cost cap. Triggers fire rarely.
- Notification email comes from a dedicated Mica address (§8.3).

### Deliberate simplifications

- One trigger kind (GCS folder completion → Batch job). An email-sourced trigger is future work.
  Shape the code so a new `source`/`action` pair can be added, but build only this one.
- No automatic retries of failed runs (no retry loops on a 3-hour job). Retries go through the CLI.
- Re-uploads into an already-segmented TMA are logged and skipped. To re-run, a human removes the
  output and runs `retry`.

---

## 2. Architecture

```
GCS OBJECT_FINALIZE (prefix raw_outputs/)
  → Pub/Sub topic mica-<env>-triggers
  → push → POST /api/v1/internal/triggers/gcs                 (cheap; no listing)
        map object → (trigger, TMA); ignore anndatas/ & others
        enqueue named Cloud Task trig-<trigger>-<hash(TMA)>-<10-min window>, delay = quiet period
  → POST /api/v1/internal/triggers/check/<trigger>/<key>/<tick>
        list raw/<T> and additional_files/<T>; completeness rule (§5); quiet period
        skip if resegmentation/<T>/ non-empty or a run record exists
        create run record; claim trigger slot; submit Batch job
        poll every 5 min (tick+1) until job ends
        verify result.json and destination objects; notify (§8); release slot
Hourly Cloud Scheduler → POST /api/v1/internal/triggers/sweep   (backstop for missed events)
Admin CLI: python -m mica.triggers.cli status|check|run|retry|figure
```

An upload of one TMA produces about 9,000 finalize events. The push handler must not write
Firestore per event, because a single document allows about 1 sustained write/s. Deterministic
Cloud Task names dedupe them to one check per TMA per 10-minute window. A per-instance in-memory
set of recently enqueued names avoids most of the 409 calls.

---

## 3. Code layout

New package `src/mica/triggers/`. It may import `agents/analysis/{registry,storage,identity,
slack_format,config}`. It must not import web modules (`mica.services.*`,
`mica.agents.{memory,context,harness,session}`).

| File | Purpose |
|---|---|
| `definitions.py` | Typed trigger definitions (pydantic) and the `TRIGGERS` constant |
| `gcs_listing.py` | Listing helpers; returns `ObjectInfo(name, size, updated, md5)` lists |
| `completeness.py` | Pure functions: listings → `Verdict(complete, reasons, n_fovs, newest_update, fingerprint)` |
| `transfers.py` | Marker read/write, manifest build and verify (crc32c); shared by the CLI now and the transfer agent later |
| `lifecycle.py` | Intake, check, run record, slots, Batch submission and polling, result verification |
| `jobs.py` | `segmentation_job_spec(...)` (Batch spec, §7.1) |
| `notify.py` | Destination resolution, Slack post and upload, SMTP email |
| `cli.py` | Admin CLI (ADC credentials; read-only by default) |
| `api.py` | FastAPI router (`/api/v1/internal/triggers/*`), included in `api/app.py` |
| `segment_tma.py` | **Runs inside the segmentation image only.** No API-only imports. |
| `qc_figure.py` | **Segmentation image only.** Core detection, TMA map parsing, plot, per-core metrics |

New image `docker/Dockerfile.segmentation` and `cloudbuild/build-segmentation.yaml`.

### 3.1 Trigger definition

```python
class GcsFolderTrigger(BaseModel):
    id: Literal["tma-resegmentation"]
    enabled: bool                         # also gated by AGENT_TRIGGERS_ENABLED
    bucket: str                           # "alchemy-breast-cancer"
    watch_prefix: str                     # "raw_outputs/"
    required_folders: tuple[str, ...]     # ("raw_outputs/raw/", "raw_outputs/additional_files/")
    ignored_prefixes: tuple[str, ...]     # ("raw_outputs/anndatas/",)
    dest_prefix: str                      # "resegmentation/"
    quiet_minutes: int = 30
    gap_quiet_minutes: int = 360          # when instrument gaps exist (§5.2)
    stuck_after_hours: int = 24
    project_id: str                       # "p_05b107ac9f31b40b" (breast-alchemy)
    params: SegmentationParams            # §7.2
    tma_maps_prefix: str                  # "metadata/" (tma_maps_*.xlsx)
```

The folder key `<T>` is the first path segment after a required folder. It must match
`^[A-Za-z0-9._-]{1,80}$`, and the identical name must exist under every required folder.
Matching is case-sensitive: `tma15` is not `TMA15`.

---

## 4. Intake and debounce (`api.py`, `lifecycle.py`)

- `POST /triggers/gcs`: Pub/Sub push with `payload_format = NONE`. Read the attributes
  `bucketId`, `objectId`, `eventType`.
  - Reject callers that are not `cloud_tasks_service_account` (same check as
    `slack_analysis.py:120`).
  - Return 200 for anything that is not a finalize under a watched folder of an enabled trigger.
  - Ignore names ending in `.gstmp` and anything under `ignored_prefixes`.
  - Enqueue `check` with the task name `trig-{id}-{sha256(T)[:16]}-{floor(now/600)}` and
    `scheduleTime = now + quiet_minutes`. Treat 409 as success.
  - Never list the bucket here.
- `POST /triggers/check/{id}/{key}/{tick}`: Cloud Tasks callback, same principal check.
  - Take the mutex `begin_event("trigger-check:{id}:{key}", lease_seconds=240)`. Load the TMA
    name from `trigger-pending:{id}:{key}`, which intake writes once per window, not per event.
  - Run §6.
- `POST /triggers/sweep`: Cloud Scheduler, hourly.
  - Do a delimiter listing of each required folder.
  - For each TMA that is present in all required folders, has no run record, and has an empty
    destination, enqueue a check.
  - With the current bucket (all 36 done), the sweep enqueues nothing.

---

## 5. Completeness

Two signals, in priority order:

1. **Transfer status markers (§5.0)**, written by whatever did the transfer. These are
   authoritative. Mica's own transfer agent (future work) will always write them, and people
   can write them today with the CLI.
2. **Inference (§5.1-§5.2)**, the fallback for hand uploads that have no marker.

### 5.0 Transfer status markers (`transfers.py`, preferred)

> The producer of these markers is the `atomx-transfer` tool. Its plan and the authoritative
> **marker contract v1** (heartbeat, requests, `_inventory.json`, manifest fields) are in
> [atomx-transfer-plan.md](atomx-transfer-plan.md) §3. Where the two documents differ, that
> contract wins. In particular, "stuck" means no `heartbeat` for 60 minutes, falling back to
> 24 hours for markers without a heartbeat.

**Location.** `gs://alchemy-breast-cancer/transfer_status/<T>.in_progress.json` and
`gs://alchemy-breast-cancer/transfer_status/<T>.done.json`.

- The folder sits outside `raw_outputs/` so it never mixes with instrument files or with `sap`'s
  globbing.
- The folder name `<T>` is the same case-sensitive key as everywhere else.
- `failed` is a third state, `<T>.failed.json`, written when a transfer gives up.

**Protocol.** GCS has no atomic rename, so "renaming" `in_progress` to `done` is two steps. The
order matters:

1. **Begin.** Create `<T>.in_progress.json` with `if_generation_match=0`. Abort if `<T>.done.json`
   already exists, unless `--force`. Contents:

   ```json
   {"version": 1, "tma": "TMA37", "state": "in_progress", "started": "<iso>",
    "by": "<email or agent run id>", "source": "<host:/path or URI>",
    "folders": ["raw_outputs/raw/TMA37/", "raw_outputs/additional_files/TMA37/"]}
   ```
2. **Copy** the files.
3. **Verify** every source file against the bucket: name, size and crc32c (or md5) must match.
4. **Write `<T>.done.json`** (create-only) with the same fields plus `finished` and a
   **manifest**: `[{"name", "size", "crc32c"}]` for every object under both folders, and
   `n_files` and `total_bytes`.
5. **Delete `<T>.in_progress.json`.**

On failure, write `<T>.failed.json` with `error` and leave `in_progress` in place.

**How the trigger uses them** (this runs before §5.2):

- `in_progress` exists and `done` does not → **blocked**, whatever inference says. No quiet
  period can override an explicit in-progress marker.
- `done` exists → verify its manifest against a fresh listing of both folders: the same names,
  sizes and crc32c, and no extra objects (ignore `*.gstmp`).
  - **Match** → complete. Start immediately, with no quiet period.
  - **Mismatch** → blocked. Send one notice naming the differing files ("done marker lists 9,112
    files, bucket has 9,090").
  - Then still run the §5.2 FOV checks, **but only as warnings**. A done marker means the
    transfer finished, so FOVs missing everywhere were never produced (the `TMA13rpt` case). No
    6-hour wait and no blocking on gaps.
- `failed` exists (and no newer `done`) → blocked. Send one failure notice to the project
  destinations.
- An `in_progress` older than `stuck_after_hours` (24) with no `done` → one "transfer looks
  stuck" notice.
- No marker at all → fall back to inference (§5.2), unchanged.

**Intake.** Marker writes are the cleanest possible event: one object per transfer. Intake maps
`transfer_status/<T>.done.json` (and `.failed.json`) finalize events straight to an immediate
`check` (task name `trig-{id}-{hash(T)}-marker-{generation}`, no delay). Events under
`raw_outputs/` keep the debounced path from §4.

**CLI for transfers done by hand today** (the future transfer agent uses the same library
functions):

- `cli transfer begin --tma T --source <path>` writes `in_progress`.
- `cli transfer finish --tma T --source <local dir with raw/ and additional_files/>`:
  - Lists the local files and computes crc32c with `google-crc32c`, which the API image already
    has through `google-cloud-storage`.
  - Compares them against the bucket. If everything matches, writes `done` (with the manifest)
    and deletes `in_progress`. Otherwise it prints the differences and writes nothing.
- `cli transfer status [--tma T]` shows markers alongside the inference verdict.
- Writing markers requires create and delete on `transfer_status/` only (§9).

**Existing TMAs.** The 36 current TMAs have no markers and don't need any: they are already
segmented and skipped. Don't backfill.

### 5.1 Inference fallback: why it needs care

Without a marker, the instrument export comes with no manifest or "done" file. Completeness is
then inferred from three independent signals:

1. The FOV-positions file says how many FOVs exist.
2. Every per-FOV file family should cover those FOVs.
3. Nothing has changed for a while.

The FOV-positions file is the single source of truth for which FOVs exist. Gaps are only
acceptable when they look like *instrument* gaps (missing in every place they could appear), never
like *transfer* gaps (present upstream, missing here).

#### Validated against every existing TMA (2026-10-05)

A script applied the strict version of this rule (every per-FOV family must exactly equal the
FOV set) to all 36 TMAs. It passed 33/36. No folder had changed since 2026-09-02, and there were
no zero-byte files. The three failures were not transfer problems:

| TMA | Strict-rule failure | What it actually is |
|---|---|---|
| `TMA13rpt` | 25 of 414 FOVs (306–389, scattered) have no `Morphology2D` TIF in `raw/` **and** no `CellLabels`/`CellComposite` in `additional_files/` | Instrument gap: transcripts exist (`AnalysisResults` 414/414) but morphology images were never produced. The existing output was made anyway; `sap` fills missing tiles with zeros. |
| `TMA14rpt` | `CellLabels`/`CellComposite` missing for FOVs 350, 358, 361 (465/468); raw TIFs 468/468 | The instrument's own segmentation produced nothing for 3 FOVs. `sap` does not use these files. |
| `TMA6` | Two `RunSummary/latest.fovs.csv` | The export was uploaded twice: the folder also contains a nested `TMA6/TMA6/…` copy. Duplicate data, not missing data. |

The strict rule would block TMAs like these forever and say nothing. Use the tiered rule below,
and add these three as M0 regression fixtures.

### 5.2 Inference rule (fallback when no marker exists)

Given the listings of `raw/<T>/**` and `additional_files/<T>/**` (both must exist with the
identical folder name):

1. **Required files in raw (block if absent).** Exactly one object for each of
   `_tx_file.csv.gz`, `_fov_positions_file.csv.gz`, `-polygons.csv.gz`, `_exprMat_file.csv.gz`
   and `_metadata_file.csv.gz` at the folder root, plus `Morphology_ChannelID_Dictionary.txt`.
   - The dictionary must contain every channel in the trigger params.
   - **F** = the `FOV` column set of the FOV-positions file (download it; it is a few KB).
2. **Transcripts per FOV (block if incomplete).** The `**/AnalysisResults/*/FOV#####/` csv set
   must equal F (36/36 historically). At least one `**/RunSummary/latest.fovs.csv` must exist.
   - More than one copy (a nested re-upload, as in `TMA6`) is a **warning**. Match FOV sets as
     sets, so duplicates count once.
3. **Images per FOV.**
   - Let `tif` = the FOV set of `raw/<T>/Morphology2D/*_F#####.TIF`, and `af_img` = the union of
     the FOV sets of `**/CellStatsDir/CellComposite/CellComposite_F#####.jpg` and the top-level
     `**/CellStatsDir/FOV#####/CellLabels_F#####.tif`.
   - **Transfer gap (block and wait):** any FOV in `af_img − tif`. The instrument produced an
     image for it, so the TIF is still in transit.
   - **Instrument gap (allowed, warn):** FOVs in `F − tif − af_img`, missing everywhere. These are
     allowed only after the **long** quiet period below. The notice and `result.json` list them,
     because `sap` renders them as blank tiles.
   - **Instrument-segmentation gap (warn only):** FOVs in `tif − af_img`. The raw image exists but
     AtoMx produced no labels (the `TMA14rpt` case). `sap` does not need them.
   - Raw TIFs for FOVs not in F: warning (stray files).
4. **Not mid-transfer.**
   - No zero-byte TIF, csv, gz, jpg or parquet under either folder.
   - The newest `updated` across both folders must be at least `quiet_minutes` old (30) when
     there are no gaps, or at least `gap_quiet_minutes` old (360) when instrument gaps exist. A
     long quiet period is what separates "the instrument never made it" from "a sync stalled".
   - If too recent, reschedule the check for `newest + quiet`.
   - Why this is enough for individual files: GCS makes an object visible only after its upload
     finishes, so a listed file is never half-written. `gcloud storage` parallel-composite
     temporary parts are written under the bucket-root `gcloud/tmp/` prefix, outside
     `raw_outputs/`. Ignore `*.gstmp` names defensively.
5. **Fingerprint.** Record the sorted (name, size, generation) as a sha256. The job re-lists after
   downloading (§7.3) and fails with "source changed during run" if it differs.

`Verdict` carries `status: incomplete | complete | complete_with_gaps`, `blocking_reasons`,
`warnings` (each human-readable, such as "25 FOVs have no morphology image anywhere: 306–389" or
"additional_files uploaded twice (nested TMA6/TMA6/)"), `n_fovs`, `newest_update` and
`fingerprint`.

- `cli status` shows all of these.
- Success notices include the warnings.
- A TMA that stays `incomplete` for more than 24 h after its last change gets **one**
  "looks stuck" notice listing the blocking reasons, so a broken transfer is never silent.
  Record `stuck_notified` in `trigger-pending` so it is sent only once.

Expected `cli status` on the current bucket (M1 acceptance):

- 33 TMAs complete.
- `TMA13rpt` complete_with_gaps (25 instrument-gap FOVs).
- `TMA14rpt` complete with a warning (3 FOVs without AtoMx labels).
- `TMA6` complete with a warning (duplicate nested upload).
- All 36 `skipped_existing`.

---

## 6. Run lifecycle (`lifecycle.py`)

Firestore connector state `trigger-run:{id}:{key}`:

```
{tma, trigger, status: queued|submitting|running|verifying|complete|failed|skipped_existing,
 run_id (uuid4 hex), fingerprint, n_fovs, slot, slot_token, job_name, created, started,
 finished, step, error, result (metrics), notified: {slack:[...], email:[...]}, tick}
```

- **Check order:**
  1. An existing record with any status other than `queued` stops the check (`running` goes on
     polling).
  2. Any object under `dest_prefix/<T>/` → `skipped_existing` (log only, no notification).
  3. Completeness verdict: markers first (§5.0), otherwise inference (§5.2). If incomplete,
     stop with no record; record the reason (and which signal decided it) in `trigger-pending`
     for `cli status`.
  4. Quiet period.
  5. Create the record, then claim the slot `begin_event("trigger-slot:{n}", lease_seconds=36000)`
     for n < `AGENT_TRIGGER_MAX_CONCURRENT` (default 1). The trigger pool is separate from the
     Slack `claude-slot:*` pool so chat workers are never starved. If no slot is free, re-check
     in 10 minutes.
  6. Write `tasks/triggers/{run_id}/request.json` to the artifact bucket (tma, source URIs,
     dest URI, params, fingerprint). Submit the job `trig-{run_id}` and treat `AlreadyExists` as
     success. Persist `status=submitting` before submitting, as in `batch.py:443-445`.
- **Polling** every 5 minutes via `tick + 1` (Cloud Tasks scheduled):
  - `SUCCEEDED` → `verifying`.
  - `FAILED`, or the job is missing → `failed`.
  - Running longer than 10 h → delete the job, then `failed`, with "timed out".
- **Verification on success:**
  - Read `tasks/triggers/{run_id}/result.json`. Require `status == "ok"`.
  - List `dest_prefix/<T>/**` and compare it to `result.json.manifest` (every name present,
    sizes equal).
  - On a mismatch, mark `failed`. The error says "partial output at …; remove it before retrying".
- **Notify** (§8) for `complete` and `failed`. Record each delivered destination so a retried
  task never posts twice. Then release the slot.
- **Failure without result.json** (OOM, preemption): use `step` from
  `tasks/triggers/{run_id}/progress.json`, which the worker writes at each step. Include a Cloud
  Logging link filtered on the label `mica-trigger-run={run_id}`.

---

## 7. The segmentation job

### 7.1 Batch spec (`jobs.py`)

- Image `AGENT_SEGMENTATION_IMAGE`, required to contain `@sha256:`. Command
  `python -m mica.triggers.segment_tma {run_id}`.
- Service account: the new `mica-<env>-segmentation` (§9), not the Claude worker SA.
- `n2-highmem-64` (64 vCPU / 512 GB), provisioning **STANDARD** (not Spot; a preempted 3-hour run
  wastes more than Spot saves).
  - 100 GB `pd-balanced` boot disk plus a 2,000 GB `pd-ssd` attached at `/scratch`, mounted into
    the container.
  - `max_run_duration = "36000s"`, `max_retry_count = 0`.
- Network: same `batch_network`/`batch_subnetwork`, no external IP (`batch.py:56-61`, `:126-132`).
  The job has no internet need at runtime because the model weights are baked in.
- Labels `mica-agent=trigger`, `mica-trigger-run={run_id}`. Cloud Logging enabled.
- No secrets in the environment or metadata. The job needs none.

Size the machine to the user's script: `LocalCluster(n_workers=32, threads_per_worker=2,
memory_limit="12GB")` = 64 threads and 384 GB. Record peak memory and step durations from the
first canary (§10) and adjust the machine type in the definition if needed.

### 7.2 Parameters (`SegmentationParams`)

| Param | Value | Source |
|---|---|---|
| dask cluster | 32 workers × 2 threads, 12 GB each | user script |
| `nuclei_channels` | `["DNA"]` | repo example call in `segmentation/cellpose.py` (commented) |
| `cyto_channels` | `["Membrane", "CD45", "PanCK"]` | same |
| `diameter` | `80` | same; consistent with existing outputs (§1). Gated by the canary (§12) |
| `gpu` | `False` | user script |
| `model_type` | `"cyto3"` (repo default) | repo |
| `um_per_px` (Proseg) | `0.12028` | user script |
| `sap` commit | `de96763933ab5952bc0ba304cb10edadfb1614a0` | pinned |

Call the functions by keyword. Assert the pinned `sap` signatures at startup with
`inspect.signature`, and fail early with a clear error if they differ.

### 7.3 Worker steps (`segment_tma.py`)

Write `progress.json` (`step`, `started`) before each step and log durations.

1. **Load** `request.json`. `cd /scratch` so `sap`'s relative `.temp` directories land on the
   SSD.
2. **Download** `raw/<T>/` to `/scratch/input/<T>/` (parallel GCS download). Recompute the §5
   raw rule and the fingerprint on the downloaded copy. A mismatch fails with "source changed
   during run". `additional_files/` is gated on but not downloaded, because `sap` does not read
   it.
3. **Ingest** `sap.ingest.cosmx("/scratch/input/<T>", "/scratch/store/<T>")`.
4. **Cellpose** with the §7.2 parameters, inside the user's `LocalCluster`/`Client`.
5. **Proseg** `sap.segmentation.proseg("/scratch/store/<T>", um_per_px=0.12028)`.
6. **Validate the layout.** These must exist and be non-empty:
   - `images/initial_imaging.ome.zarr/zarr.json`, with at least `s0` and an `omero.channels`
     label set that includes the param channels.
   - The 4 parquet files in §1.
   - `tables/proseg_table.zarr/zarr.json` with `obs/centroid_x`.
   - Both polygon tables with more than 0 rows, and the table with `n_obs > 0`.
   - Also compare the set of non-chunk structural paths (every `zarr.json` plus the parquets) to
     the reference TMA26 skeleton, allowing for a different pyramid depth.
7. **QC figure and metrics** (§7.4) from the local store.
8. **Upload** the store to `gs://<bucket>/<dest_prefix><T>/`.
   - First re-check that the destination is empty; abort if it is not.
   - Upload with create-only semantics: `if_generation_match=0` on every object, so a
     concurrent writer can never be overwritten.
   - Write `manifest` (name, size, md5) into the result.
9. Upload `qc.png` and write `result.json`
   (`status, metrics, manifest, sap_commit, params, durations`). On any exception, write
   `result.json` with `status: "error", step, error` (the first line plus the exception type)
   inside a `finally` block, then exit non-zero.

### 7.4 QC figure (`qc_figure.py`)

Goal: the user's example figure, with every core's cells drawn and labeled, so a failed
segmentation is obvious at a glance.

- **Port, don't depend.** Copy `tma_number`, `tma_map_file_key`, `parse_study_id`,
  `parse_core_tumor_label`, `parse_tma_slot_type`, `parse_core_label`,
  `parse_tma_core_metadata`, `label_cores` and `grid_based_mapping` from
  `alchemy_breast_cosmx@cd78d897` (`src/alchemy/merge_map.py`, §1) into `qc_figure.py`.
  - Credit and pin the source commit in a header comment. Change only the I/O: plain DataFrames
    and arrays instead of AnnData, and `openpyxl`/`pandas.read_excel` on a local copy of the
    workbook.
  - Do not install the `alchemy` package. It pins Python <3.12 and pulls in scanpy, igraph and
    leiden, which conflict with the segmentation env (Python 3.12).
  - Add a golden test: on fixed synthetic centroids, the ported functions must give the same
    slots and labels as the originals. Generate the expected values once from the original repo
    and commit them as JSON.
- **Inputs:** the Proseg table `obs[centroid_x, centroid_y]` (pixels) and the map workbook from
  `metadata/tma_maps_<a>_<b>.xlsx`, sheet `"TMA #<n>"`. Per-core cell count = cells per mapped
  label.
- **Plot**, matching the user's example:
  - Rasterized scatter, `s=0.1`, alpha 0.3, colored per core. Axes in pixels. Equal aspect,
    with the y axis inverted.
  - Each label is placed at its core's centroid: `"<A1>\n<study_id>\n<T|N>"`, or
    `"<B1>\nblank"` / `"<A1>\nmarker"`.
  - Title: `"<T>: geometry-mapped cores and patient IDs — N cells (Proseg), sap@de96763"`.
  - Add a small text box listing patient cores with fewer than `max(100, 5% of the median)`
    cells (the same threshold as `grid_based_mapping`). These are the cores a person should
    inspect.
- **Fallback:**
  - TMA 1 (`label_cores` raises), no matching map, or an exception during mapping: plot with
    lattice slot numbers only and say so in the title.
  - Never fail the run because of the figure. Record `figure_mode: mapped|unmapped` and the
    reason in `result.json`.
- **Metrics in `result.json`:** total cells (Cellpose and Proseg), median Cellpose
  equivalent-diameter (px), occupied slots out of 70, chosen flip, low-cell patient cores, the
  median cells per patient core, and step durations.

### 7.5 Image (`docker/Dockerfile.segmentation`)

- Base `mambaorg/micromamba` pinned by digest. Create an env from `envs/resegmentation.yaml` at
  the pinned `sap` commit.
- Add `gcsfs`, `universal_pathlib`, `dask-geopandas`, `polars`, `rasterio`, `geopandas`,
  `anndata`, `openpyxl`, `matplotlib`, `scipy`, `scikit-image`, `tifffile` and
  `google-cloud-storage`. Pin every version and keep a lock or constraints file.
- Install the `proseg_py` wheel (§1). A Cloud Build step copies it from
  `gs://sap-harjaisal-khushi/` into the build context, and the Dockerfile verifies sha256
  `2df2b4f0…de7f2` before installing; a mismatch fails the build. The Cloud Build SA needs
  `objectViewer` on that object (§9). Alternatively, the human copies the wheel once into the
  artifact bucket under `build-inputs/`.
- Install `sap` from the source archive at
  `gs://<artifact-bucket>/build-inputs/spatial_analysis_pipeline-de96763.zip`.
  - This is GitHub's master zip, 18,035 bytes, sha256
    `09bc5d8f84ba8ef79242e87ef339a88edc0abc056f37a1aabfb973b0e072fbc3`. Its zip comment is the
    commit `de96763933ab5952bc0ba304cb10edadfb1614a0`.
  - Its contents were verified identical to a clone of that commit on 2026-10-05.
  - Cloud Build copies it into the build context, the Dockerfile checks the sha256, unzips it and
    runs `pip install --no-deps ./spatial_analysis_pipeline-master` (not editable). No GitHub
    access is needed at build time.
  - Do not commit this archive or `sap` source to the Mica repo.
  - **Uploaded 2026-10-05** with the human's approval, `--no-clobber`. Verified: 18,035 bytes,
    md5 `d+E/7RvBD9DlkkG0OpxoUw==`, sha256 matches. It lives at
    `gs://pistachio-staging-artifacts-5304c19c/build-inputs/spatial_analysis_pipeline-de96763.zip`.
  - `build-inputs/` survives the artifact bucket's 30-day delete rule only because that rule's
    `matches_prefix` list (`infra/main.tf:58-66`) omits it. If `slack_agent_configured` were
    false, `matches_prefix = null` would make the rule delete *everything* older than 30 days.
    Add a test or `check` asserting the lifecycle rule always has a prefix list without
    `build-inputs/`.
- Copy only `src/mica/triggers/` plus `src/mica/__init__.py`. Do not `pip install`
  Mica's full dependency set.
- Copy `cyto3` and `size_cyto3.npy` from `gs://sap-harjaisal-khushi/.cellpose/models/` into
  `/opt/cellpose-models` (`CELLPOSE_LOCAL_MODELS_PATH`). At build time, load
  `models.Cellpose(model_type="cyto3", gpu=False)` to prove no download is needed (run the build
  step without network after the copy if possible). Record both files' md5 in the image labels.
- Smoke check (also a Cloud Build step): `import sap, proseg, cellpose`; assert the signatures;
  run `segment_tma --self-test`, which runs ingest → cellpose → proseg on a tiny synthetic
  2-FOV CosMx fixture in `tests/fixtures/cosmx_tiny/` built by `tests/make_fixture.py`.
- To move to a newer `sap` commit, the human supplies a new archive. Update its sha256, the
  commit pin in §7.2, and the signature assertions together, then re-run the §12 canary.

---

## 8. Notifications (`notify.py`)

### 8.1 Destinations

Recipients always come from the project, never from a hard-coded list. Load the registry entry
for `p_05b107ac9f31b40b` at notify time, not when the trigger fires, so later `share`/`unshare`
and `publish`/`unpublish` changes apply to the next notice.

- **Email recipients** = `{project.owner} ∪ project.members`. These are already normalized emails
  (the registry's user IDs). Deduplicate, sort, and validate with `email.utils.parseaddr` (drop
  anything without an `@`).
  - On 2026-10-05 this resolves to `kpd2137@columbia.edu` only.
  - Send one message with every recipient in `To:`. Members of the same project may see each
    other's addresses.
- If `project.published_channels` is non-empty, post to every channel id in it (the part after
  `team:`).
- If it is empty, email the recipients above.
- If every Slack post fails (bot removed, channel archived), fall back to email.
- If email also fails, mark `notify_failed` in the record (shown in `cli status`) and log an error.
- A missing project or empty registry is a configuration error: log it, show it in `cli status`,
  and send nothing.

### 8.2 Content

**Success.** A top-level Slack message, then the figure uploaded into its thread with
`files_upload_v2`. Email has the same text with `qc.png` attached.

```
✅ Resegmentation finished for *TMA26* (430 FOVs, 2h 41m)
• 412,331 cells (Proseg) across 66/70 cores; median 5,980 per core
• Low-cell cores (<100): B1, C1, D1 (map: blank)
• Output: gs://alchemy-breast-cancer/resegmentation/TMA26/
• Params: Cellpose cyto3 d=80 (DNA | Membrane+CD45+PanCK), Proseg 0.12028 µm/px · sap@de96763
Check the figure: every core should be filled with cells.
```

**Failure.** Same destinations, with the figure if one was produced.

```
⚠️ Resegmentation failed for *TMA26* at step *cellpose* after 1h 12m
• Error: MemoryError: …
• Output: nothing written            (or: PARTIAL output at …; remove it before retrying)
• Logs: <Cloud Logging link>
Retry after fixing: python -m mica.triggers.cli retry --trigger tma-resegmentation --tma TMA26
```

Use `to_mrkdwn`/`chunks` from `slack_format.py` for Slack text. The email subject is
`[Mica] TMA26 resegmentation finished` or `… FAILED`.

### 8.3 Email sender

Use a dedicated Gmail account owned by the lab (for example `mica.<lab>@gmail.com`) with
2-step verification and an **app password**. Send with `smtplib.SMTP_SSL("smtp.gmail.com", 465)`.

Settings (`agents/analysis/config.py`):

- `AGENT_NOTIFY_EMAIL_ADDRESS`
- `AGENT_NOTIFY_EMAIL_APP_PASSWORD` (`SecretStr`, from Secret Manager via a Cloud Run
  `secret_key_ref`)

Cloud Run allows egress on 465. If the address is unset, email delivery is disabled and
`cli status` says so.

This is not the legacy `connectors/gmail.py` path. That path uses ADC, which cannot send as a
Gmail user without domain-wide delegation. If email-sourced triggers are added later, that inbox
will need Gmail API OAuth instead; leave that for the email-trigger plan.

---

## 9. Infrastructure (`infra/triggers.tf`, all gated by `var.triggers_enabled`, default false)

- `google_pubsub_topic.triggers`. Give the GCS service agent
  (`data.google_storage_project_service_account`) `roles/pubsub.publisher` on it.
- `google_storage_notification` on `var.trigger_bucket` (`alchemy-breast-cancer`):
  `object_name_prefix = "raw_outputs/"`, `event_types = ["OBJECT_FINALIZE"]`,
  `payload_format = "NONE"`. The bucket itself stays unmanaged by Terraform.
- A second `google_storage_notification` on the same bucket and topic for
  `object_name_prefix = "transfer_status/"` (finalize only). One notification config supports
  one prefix.
- Marker writers: humans already have bucket access. The future transfer agent's SA gets
  `objectCreator` + `objectViewer` on `raw_outputs/` and `objectAdmin` on `transfer_status/`
  (in that agent's own plan). The control SA only reads markers.
- `google_pubsub_subscription.triggers_push` → `${api.uri}/api/v1/internal/triggers/gcs`, with an
  OIDC token from the control SA (pattern `main.tf:488-498`), `ack_deadline_seconds = 20`, and an
  exponential retry policy.
- `google_cloud_scheduler_job.triggers_sweep`, hourly → `/api/v1/internal/triggers/sweep`
  (pattern `main.tf:526-538`).
- `google_service_account.segmentation`, with:
  - `roles/batch.agentReporter` and `roles/logging.logWriter`, plus `artifactregistry.reader` on
    the repo.
  - `storage.objectViewer` on `var.trigger_bucket` (conditioned to `raw_outputs/` and
    `metadata/`).
  - `storage.objectCreator` on `var.trigger_bucket` (conditioned to `resegmentation/`). Create
    only: it can neither overwrite nor delete.
  - `storage.objectAdmin` on the artifact bucket (conditioned to `tasks/triggers/`).
    `tasks/` objects expire after 30 days. That is fine for `request.json` and `progress.json`,
    but `result.json` and `qc.png` are the run's provenance. So the segmentation SA also gets
    object create on `projects/<project_id>/triggers/`. The worker writes a second copy there
    (`projects/p_05b107ac9f31b40b/triggers/<T>/<run_id>/{result.json,qc.png}`), which never
    expires.
- Cloud Build SA:
  - `roles/storage.objectViewer` on the artifact bucket, conditioned to `build-inputs/` (the
    `sap` archive).
  - The same role on `gs://sap-harjaisal-khushi` for the wheel and `.cellpose/models/`, unless
    the human copies those into `build-inputs/` too. That bucket is in `US-CENTRAL1`; confirm
    its project before writing the binding.
- Secret `mica-<env>-notify-email-app-password` (the human sets the value). Cloud Run env:
  `AGENT_TRIGGERS_ENABLED`, `AGENT_SEGMENTATION_IMAGE`, `AGENT_SEGMENTATION_SERVICE_ACCOUNT`,
  `AGENT_NOTIFY_EMAIL_ADDRESS`, and the secret reference.
- `check "triggers_ready"`: if enabled, the image is pinned by digest and the email address or a
  project with channels is configured.

The control SA already has project `storage.objectViewer` (list/read for completeness),
`batch.jobsEditor`, `cloudtasks.enqueuer` and `iam.serviceAccountUser`. Confirm that
`serviceAccountUser` covers the new SA; if it is project-level, it does.

---

## 10. Milestones

### M0: Failing tests first (`tests/test_triggers_*.py`)

Use fake listings built from the verified shapes in §1: TMA26-style with a wrapper and
tma36-style without one.

- Complete TMA → complete.
- Transfer gap: a TIF missing while that FOV's `CellComposite` exists → incomplete (wait), with a
  reason naming the FOVs.
- A missing `AnalysisResults` FOV → incomplete.
- Fixtures reproducing the three real cases in §5.1:
  - `TMA13rpt` → complete_with_gaps, but only once 6 h have passed with no changes.
  - `TMA14rpt` → complete with a warning.
  - `TMA6` nested duplicate → complete with a warning.
- Incomplete for more than 24 h → exactly one "looks stuck" notice.
- Markers:
  - `in_progress` only → blocked, even when inference says complete and quiet.
  - `done` with a matching manifest → complete immediately; FOV gaps become warnings.
  - `done` whose manifest differs from the listing (missing file, size or crc32c mismatch, extra
    file) → blocked, with one notice.
  - `failed` → blocked, with one failure notice.
  - A stale `in_progress` → one stuck notice.
  - No marker → inference path.
  - The `transfer_status/<T>.done.json` event → immediate check (no debounce).
  - `cli transfer finish` writes nothing when any local file differs from the bucket.
- `anndatas/` events are ignored.
- Mismatched case between folders → not matched.
- A recent `updated` → reschedule at `newest + quiet`.
- A zero-byte file → incomplete.
- Intake: 1,000 events for one TMA in one window → one task name. Unrelated prefixes → no task.
  A wrong OIDC principal → 403.
- Lifecycle: an existing destination → `skipped_existing` and no job. An existing record → no
  second job. Slot exhaustion → re-check scheduled. `AlreadyExists` on submit → treated as
  success. A failed job → one failure notice. A retried notify task → no duplicate post.
- Verification: a manifest mismatch → `failed` with "partial output".
- Notify:
  - Channels present → Slack only.
  - No channels → owner and member emails, read from the registry at send time. A `share` after
    the trigger fires but before notify reaches the new member; an `unshare` removes them.
    Recipients are deduplicated and invalid addresses dropped.
  - All Slack posts fail → email fallback.
  - Missing project → nothing sent and an error recorded.
- The job spec has no secrets in env or metadata, uses STANDARD provisioning and the segmentation
  SA, and requires a digest-pinned image.
- `qc_figure`:
  - Map cell parsing: `Study ID # 1227-78 (A8) T`, `Marker (lung)`, `(blank)`.
  - TMA numbers: `TMA13rpt` → 13, `AdipokinesRO1TMA91` → 9, `AdipokinesRO1TMA101` → 10,
    `tma36` → 36.
  - TMA 1 → unmapped fallback.
  - Golden equivalence with the original `alchemy` functions (§7.4).

### M1: Definitions, listing, completeness, CLI `status`/`check` (read-only)

Implement §3.1, §5 and the read-only CLI. Run `cli status` against the real bucket using the
human's ADC credentials. It must match the §5.2 expected verdicts for all 36 TMAs, with no
writes. Paste the output into the PR.

### M2: Segmentation image and worker (after the §11 blockers are cleared)

- `Dockerfile.segmentation`, `segment_tma.py`, `qc_figure.py`, the synthetic fixture and
  `--self-test`.
- `cli figure --tma <T> --from gs://alchemy-breast-cancer/resegmentation/<T>/` downloads only the
  Proseg table `obs` and the map workbook, and writes `qc.png` locally. This validates the figure
  without a 3-hour run. Show the human two of them:
  - **TMA25** (good segmentation): every patient core filled and labeled; marker and blank slots
    in the right places.
  - **TMA26** (known failure, the user's example): it must look visibly wrong, and its low-cell
    list must be long.

### M3: Control plane

`api.py` routes, `lifecycle.py`, `jobs.py`, and the Firestore records. Include the router in
`api/app.py`. Add `cli run --tma T [--dest gs://…]`, which uses the same lifecycle with a
destination override for canaries, and `cli retry`.

### M4: Notifications

`notify.py`, the email settings, and the §8 message formats. Test with the in-memory repository
and fake Slack/SMTP clients.

### M5: Infrastructure and docs

- `infra/triggers.tf` and variables, with `terraform plan` output only.
- `cloudbuild/build-segmentation.yaml`.
- A new `docs/background-triggers.md` operator guide: how to add a trigger (a code change plus a
  deploy), the CLI, the TMA18 note, and how to re-run a TMA.
- Update `README.md` and `deploy/slack.env.example`.

### M6 (optional, ask first): Follow-up questions in the posted thread

Seed `projects/_threads/{thread_key}/checkpoint.json` and `claude-thread:{key}` for the posted
message, so replies in that Slack thread go through the normal agent with this project's brief
and the run's `result.json` as context. Skip this unless the human asks.

---

## 11. Status of open items

1. **`proseg` wheel: resolved.** It is located and pinned (§1, §7.5).
2. **Private repo access for Cloud Build: resolved by archive.** The human supplied GitHub's zip
   of `de96763`, verified identical to the clone (§7.5).
   - Uploaded and verified on 2026-10-05 (§7.5).
   - No deploy key is needed. `alchemy_breast_cosmx` code is ported into this repo at a pinned
     commit, so builds never fetch it.
3. **Mica project: resolved.** It is `breast-alchemy` (`p_05b107ac9f31b40b`). Before go-live
   (§12 step 5), the owner should `share` it with the people who should get emails and publish it
   to the notice channel. Otherwise notices go only to the owner's email. `cli status` shows the
   resolved destinations.
4. **Cellpose parameters: inferred.** Nobody recorded them. Use the repo's example, which matches
   the existing outputs (§1). The canary in §12 is the acceptance gate. Ask harjaisal to confirm
   if they can; that isn't required to proceed.
5. **QC figure code: resolved.** Port the core mapping from `alchemy_breast_cosmx` (§7.4). The
   plotting code wasn't found, so §7.4 specifies it.
6. **Known-bad existing outputs: FYI only.** TMA26 (failed), TMA1 and tma15 (excluded
   downstream), and TMA18 (partial image). The trigger never re-runs them automatically because
   their folders exist. A re-run needs a human to delete the folder, then `cli retry`.

---

## 12. Rollout (ask before every step)

1. Build the segmentation image and record its digest.
2. **Canary run** with no trigger and no lab-bucket writes: run
   `cli run --tma TMA3 --dest gs://<artifact-bucket>/tasks/triggers/canary/`. TMA3 is the smallest
   TMA with a good existing output (244 FOVs); TMA1 is smaller but is a known-bad,
   cut-off grid. **Acceptance gate**, compared against `resegmentation/TMA3/`:
   - Identical structural skeleton.
   - Cellpose cell count within ±5%.
   - Median Cellpose equivalent diameter within ±5%.
   - Proseg cell count within ±5%.
   - The mapped `qc.png` looks right to the human.

   If the cell counts or diameter miss, the parameters differ from what was used before: stop and
   ask the human (try `diameter=None` next). Record runtime and peak memory and adjust the
   machine type if needed.
3. Deploy the API with `AGENT_TRIGGERS_ENABLED=false`, then set the email app-password secret.
   Send a test notification with `cli notify-test` to the project destinations.
4. **End-to-end test on a scratch bucket.**
   - With approval, create `pistachio-staging-trigger-canary`, copy TMA3's `raw/` and
     `additional_files/` into it under a new name, and point a second, test-only trigger
     definition at it (destination in the same scratch bucket; notify only the human's email).
   - Confirm the whole chain: debounce → one job → success notice with the figure.
   - Also upload a deliberately incomplete copy and confirm no job starts.
   - Delete the scratch bucket afterwards, with approval.
5. `terraform apply` with `triggers_enabled = true` (notification on `alchemy-breast-cancer`).
   Confirm `cli status` and the first sweep are quiet, since all 36 are already done.

---

## 13. Verification

- Unit: all M0 tests. Ruff and strict mypy pass.
- `cli status` against the real bucket matches the §5.2 expected verdicts (33 complete, TMA13rpt
  complete_with_gaps, TMA14rpt and TMA6 complete with warnings), 36/36 `skipped_existing`; `anndatas`
  absent from the output.
- Self-test in the image passes. The TMA3 canary meets the §12 step 2 gate (skeleton, ±5% cell
  counts and median diameter versus `resegmentation/TMA3/`).
- `cli figure` on TMA25 looks right and on TMA26 looks visibly failed (§10 M2).
- E2E (scratch bucket): one job per upload despite thousands of events; an incomplete upload
  starts nothing; a success notice has the figure; a forced failure (bad channel name in the
  test params) sends a failure notice to the same destination; destination objects are never
  overwritten (attempt a second run → `skipped_existing`).
- No secrets in Batch job env or metadata (`gcloud batch jobs describe`).

## 14. Out of scope

User-created triggers; email-sourced triggers; automatic retries; GPU Cellpose; running
downstream analysis after segmentation; repairing existing outputs (TMA18).
