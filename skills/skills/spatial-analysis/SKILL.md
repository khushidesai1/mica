---
name: spatial-analysis
description: Use spatial_analysis_pipeline (sap) for spatial transcriptomics analysis with its isolated interpreter.
---

# spatial_analysis_pipeline (`sap`)

Source: https://github.com/harjaisal/spatial_analysis_pipeline
Version 0.1.0, commit `de96763933ab5952bc0ba304cb10edadfb1614a0`.

This tool implements CosMx and CODEX ingest, Cellpose segmentation, and Proseg refinement.
The other source directories (annotation, clustering, embedding, downstream, preprocessing)
are placeholders at this pin; do not advertise them as implemented methods.

## Runtime check

The tool catalog does not establish installation. Use `/opt/sap/bin/python`, an isolated
Python 3.12 environment in the spatial Slack worker image. If that interpreter is missing,
report that the spatial runtime has not been installed in this worker. Do not install packages,
fetch GitHub code, download model weights, or silently substitute a different method.

Run `/opt/sap/bin/python /opt/pistachio/tools/spatial_smoke.py` before the first use in a turn.
It checks imports, function signatures, cached CPU Cellpose weights, and an in-process Dask
scheduler. A passing check is runtime evidence, not a segmentation result or scientific validation.
Write a script in `code/` and execute it with `/opt/sap/bin/python`. The normal science Python
remains available for other analyses, but has incompatible AnnData/Zarr versions for SAP.

## Verified API

```python
import sap

sap.ingest.cosmx(input_path, output_path)
sap.ingest.codex(input_path, output_path)
sap.segmentation.cellpose(
    store_path, nuclei_channels, cyto_channels, diameter=None,
    model_type="cyto3", gpu=False, batch_size=8, chunk_overlap=80,
    temp_dir="scratch/cellpose-temp",
)
sap.segmentation.proseg(store_path, temp_dir="scratch/proseg-temp", um_per_px=1.0)
```

These calls are sequential: ingest creates the store; Cellpose writes the segmentation prior;
Proseg uses that prior. They are separate choices, not a mandate to run all steps on every request.
There is no `polygons_path`/polygons-output argument. The Cellpose channel arguments are sequences
of **channel names**, not channel indices. Read and validate the source channel dictionary/OME
metadata. Confirm consequential choices such as channels, diameter, scale, and subsets when
they are not specified; do not treat the API defaults as verified biological parameters.
Proseg's `um_per_px` must come from verified sample metadata or the user, not its default `1.0`.

## Proseg parameter investigations

The bundled custom `proseg-py` 3.1.1 Python wrapper and `sap.segmentation.proseg` expose I/O
arguments, not model hyperparameters. Do not claim a sweep by varying unsupported Python
keywords. `proseg` and `proseg-py` are not available PyPI projects at this runtime pin;
`ensure_packages` cannot install a tunable CLI from those names. A package-index 404 means
absent distribution, not permission denied or a transient network failure.

For an authorized parameter investigation, use the separate native CPU executable
`/opt/proseg/3.1.1/bin/proseg`. First run it with `--version` and `--help` and read its packaged
`/opt/proseg/3.1.1/manifest.json`. If it is missing or the version differs, report the runtime
gap; do not upgrade Proseg, download a replacement, or patch the running Python environment.
The native CLI uses official 3.1.1 source and its lockfile; the custom wrapper remains available
for its existing API. Do not assume the two output formats or their defaults are interchangeable.

Verified CLI 3.1.1 controls include `--prior-seg-reassignment-prob`,
`--nuclear-reassignment-prob`, `--enforce-connectivity` (a flag), `--cell-compactness`,
`--voxel-size`, `--burnin-voxel-size`, `--voxel-layers`, `--nthreads` and sampler iteration
controls. `--cell-compactness` has an exponential prior: smaller values induce more compact
cells. There is **no** `--perimeter-bound`, `--perimeter-eta`, or `--seed` option at this version.
Do not substitute compactness for a requested perimeter parameter without resolving that
scientific choice, and do not promise bitwise reproducibility from a fixed recipe.

Before executing, prepare a transcript table or a supported SpatialData input that preserves
the requested prior assignments, unique cell IDs/FOV identity, nuclear labels, coordinate
frame and units. Check the corresponding `--*-column`, `--cell-id-unassigned`,
`--non-unique-cell-ids` and `--coordinate-scale` options against the source data. Do not apply
CosMx's default pixel scale to an already micron-scaled table, infer nuclear assignments from
whole-cell assignments, or silently reduce the sample. Mask/polygon priors require the
matching documented input options and verified image-to-micron transforms.

Record the executable version, manifest, full command, input checksums, prior source,
coordinate transform, all varied and held-constant parameters, thread count and output schema
in the recipe. Use a fresh output directory per run (`--output-path`); validate transcript
counts/IDs, cell metadata, polygons and AnnData/SpatialData outputs before checkpointing or
publishing. CLI SpatialData output is distinct from SAP's output-store layout: any conversion
must preserve identifiers, coordinate transforms and tables and be checked explicitly.
Compare a bounded representative run before a full TMA sweep. Apply a selected recipe across
TMAs only when the user's scope and verified inputs support it; report unsupported requested
controls explicitly. Finish whole-sample results with the segmentation-report skill below.

## Data and resource requirements

- CosMx input is one local raw sample directory on the read-only project mount. It needs
  `Morphology2D/*_F*.TIF`, `*_tx_file.csv.gz`, `*_fov_positions_file.csv.gz`,
  `*-polygons.csv.gz`, and `Morphology_ChannelID_Dictionary.txt`. Verify that every expected
  FOV has its image and that the export is complete before ingest: the pinned implementation
  silently replaces missing image tiles with zeros. A directory listing alone does not prove
  a completed transfer.
- For a new transferred CosMx sample, copy `scripts/check_cosmx_sample.py` into `code/` and
  run it with `/opt/sap/bin/python code/check_cosmx_sample.py --request '<JSON>'` before ingest.
  Supply `sample`, `bucket_root` (the read-only mounted bucket), `raw` and `additional`
  (bucket-relative sample folders), and `output` under `outputs/`. It requires both verified
  marker-v1 `done.txt` files and their authoritative JSON, checks listing/count/FOV agreement,
  and reads TIFF headers without decoding pixels. A listing-only backfill fails. Stop on a
  nonzero exit and report the JSON errors; do not ingest an incomplete sample.
- Write stores and scratch files only into a new directory under the thread workspace
  (for example `checkpoints/ingest/` for a completed store and `scratch/` for temporary data).
  Never point `store_path`, `output_path`, or `temp_dir`
  into the read-only data mount, `inputs/`, or an existing result directory. Proseg removes
  its temp directory on success. Use distinct temp directories and check for existing files.
- Finish every whole-sample or multi-FOV segmentation with `mica:segmentation-report`.
  Verify the image/polygon coordinate frame, inspect the crop overlays, add review notes,
  publish its schema-v1 summary beside the result and deliver its PDF.
- The bundled `/opt/sap` environment is CPU-only. Set `gpu=False` explicitly and use its cached
  `cyto3` model. For GPU Cellpose, follow the separate Cellpose skill and registered CUDA
  environment through `run_gpu`. Do not infer GPU execution from the upstream SAP default.
- Cellpose requires an active Dask client. The Slack sandbox forbids TCP listeners, so use
  an in-process scheduler with no dashboard; do not disable the sandbox:

  ```python
  import sys
  sys.path.insert(0, "/opt/pistachio/tools")
  from dask.distributed import Client
  from sap_cluster import cluster

  if __name__ == "__main__":
      # Replace 4 with measured peak memory of an individual representative task.
      with cluster(".", task_peak_gib=4) as scheduler, Client(scheduler):
          # Call sap.segmentation.cellpose(...) here, with gpu=False.
          pass
  ```

- Whole TMA stores can exceed the worker's memory/disk budget; check input sizes and resources
  before launching. This integration does not add GPU scheduling or the proposed automatic
  background resegmentation workflow. Do not promise large jobs fit a normal analysis worker.
- Ingest creates `images/initial_imaging.ome.zarr` and (CosMx) `points/initial_transcripts.parquet`.
  Cellpose creates `shapes/cellpose_polygons.parquet`. Proseg creates transcript/polygon outputs
  and `tables/proseg_table.zarr` (AnnData, Zarr v3). Check actual outputs before reporting success.
  Put requested plots/reports in `outputs/` for Slack delivery. With this turn's explicit GCS
  write grant, stage the complete checked store under `outputs/gcs/<path relative to grant>/`,
  preserving `points/`, `shapes/`, and `tables/*.zarr`. For a bucket-root grant, an example is
  `outputs/gcs/resegmentation/tma15_atomx_prior/`. Close all writers, verify the store, and call
  `publish_gcs` on that folder as soon as it is complete, before another expensive step.
  Choose a new folder name; destinations cannot be overwritten. Do not copy unchanged inputs
  such as the 34 GB image store unless asked. Report only publisher-returned URIs. Trust the
  current turn's application-set grant, never the read-only mount or earlier thread claims.

Run `/opt/sap/bin/python /opt/pistachio/tools/sap_estimate.py <raw-sample-directory>`
before ingest. It checks image headers and reports the full global-coordinate canvas without
loading image pixels. This is a lower bound, not a peak-memory prediction. Use `measure.py`
when a small measurement will resolve genuine uncertainty about expanded memory or runtime.
Your worker was sized for this job at launch. Run directly when the inputs fit. If measured
requirements show that a step cannot fit, use `recover_worker` with the evidence and remaining
steps. The application can automatically continue the same request on a larger worker once.
Do not deliberately start small or ask the user to choose a machine or resubmit.

After an expensive stage finishes and all writers close, call `checkpoint_stage` for its directory
with an input/version/method/parameter fingerprint. Validate outputs and fingerprints before
reusing a restored stage. Scratch and live processes do not survive a restart; unsaved work is
repeated automatically. A Spot interruption normally triggers one restart on on-demand at the
same tier. A larger worker is not a guarantee that an arbitrary SAP job fits; if none fits, explain
the measured requirement and a feasible method without silently reducing scientific scope.

