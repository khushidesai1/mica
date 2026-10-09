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

## Data and resource requirements

- CosMx input is one local raw sample directory on the read-only project mount. It needs
  `Morphology2D/*_F*.TIF`, `*_tx_file.csv.gz`, `*_fov_positions_file.csv.gz`,
  `*-polygons.csv.gz`, and `Morphology_ChannelID_Dictionary.txt`. Verify that every expected
  FOV has its image and that the export is complete before ingest: the pinned implementation
  silently replaces missing image tiles with zeros. A directory listing alone does not prove
  a completed transfer.
- Write stores and scratch files only into a new directory under the thread workspace
  (for example `checkpoints/ingest/` for a completed store and `scratch/` for temporary data).
  Never point `store_path`, `output_path`, or `temp_dir`
  into the read-only data mount, `inputs/`, or an existing result directory. Proseg removes
  its temp directory on success. Use distinct temp directories and check for existing files.
- The supplied image is CPU-only. Set `gpu=False` explicitly and use the cached `cyto3` model.
  Other models are unavailable. Do not infer that a GPU exists from the upstream default.
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
  Put requested plots/reports in `outputs/` for Slack delivery. Large stores require the normal
  explicit GCS write grant and upload mechanism; mounted project data remains read-only.

Run `/opt/sap/bin/python /opt/pistachio/tools/sap_estimate.py <raw-sample-directory>`
before ingest. It checks image headers and reports the full global-coordinate canvas without
loading image pixels. This is a lower bound, not a peak-memory prediction. Use `measure.py`
on representative bounded probes and estimate total and per-task RAM before requesting compute.
When the compute tools are available, `estimate_worker` and `request_worker` can continue the
same request on a larger Spot tier. Quotes include provisioned disk and startup margin; requests
up to the configured $50 infrastructure limit proceed automatically. Above it, Slack asks for
approval. Model spending has a separate request-wide limit.

After a stage finishes and all writers close, call `checkpoint_stage` for its directory with
an input/method fingerprint. Validate outputs and the fingerprint before reusing a restored
stage. Scratch does not survive resizing. Saved stages survive a Spot interruption, but automatic
preemption restart is not enabled in this first release. Full TMA runtime/memory estimates have
not yet been calibrated; a larger worker is not a guarantee that an arbitrary SAP job fits.
