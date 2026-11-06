---
name: segmentation-report
description: Generate and visually review a standard PDF QC report after whole-sample or multi-FOV segmentation, including Cellpose, Cellpose-SAM and Proseg; compare prior masks and cohort summaries when available.
---

Produce this report after every whole-sample or multi-FOV segmentation. Use actual result
statistics, never copied or hard-coded statistics from a previous batch. Copy
`scripts/report.py` into `code/` and run it with `/opt/sap/bin/python` in Bash:

```bash
/opt/sap/bin/python code/report.py --request '<JSON>'
```

Required JSON fields:

- `sample`; `method` with `name`, `version`, and `parameters`; `um_per_px` from verified metadata.
- `cells`: `{ "path": "checkpoints/cells.parquet", "units": "pixel" }` for GeoParquet WKB
  polygons (`geometry` column by default), or `{ "path": "checkpoints/masks.tif" }` for a
  nonnegative integer label TIFF/Zarr (zero is background). Polygon `units` must be `pixel`
  or `um`; they are not inferred from a filename. Optional `geometry` names the WKB column.
- `image`: `{ "path": "checkpoints/image.ome.zarr", "channels": ["DNA", "Membrane"],
  "nuclear_channel": "DNA", "membrane_channel": "Membrane" }`. TIFF is also supported.
  Set `axes` (e.g. `CYX`) only when metadata is ambiguous. `membrane_channel` can be omitted
  if no membrane stain exists; disclose that in `review`. Use exact channel names from metadata.
- `output_dir`: a **new** folder under `outputs/` (for example `outputs/segmentation-qc-v1`).

All coordinates must use the same image frame: origin at the upper left, x right and y down.
The image is a single 2D plane. Label shape must match it. Polygon coordinates in µm are
divided by `um_per_px`; translations and flipped y frames must be resolved before calling.
Inspect source metadata and a representative overlay; stop if scale or alignment is uncertain.
Do not infer scale from the algorithm's default, or relabel pixel areas as µm².

Optional inputs:

- `prior`: polygon input using the same schema as `cells`.
- `transcripts`: `{ "path": "checkpoints/transcripts.parquet", "x": "x_global_px",
  "y": "y_global_px", "cell_id": "cell_id" }`, or CSV/CSV.gz. Coordinates are pixels.
  Set `unassigned` to the source's unassigned identifiers if they differ from zero, -1,
  empty/null, or `UNASSIGNED`. Reading streams batches and keeps an unbiased seeded reservoir
  of at most 100,000 rows. Assignment fractions and transcripts per cell are estimates of the
  supplied table; do not describe them as exact full-dataset measurements if it is a sample.
- `fovs`: `{ "path": "positions.csv.gz", "width": 2048, "height": 2048 }`. Accepts
  `FOV,x_global_px,y_global_px` or `id,x0,y0,width,height`, in the verified image frame.
  Without positions, the script uses grid tiles. Ranking uses assignment fraction when
  available and cell density otherwise; it is a review aid, not a biological quality verdict.
- `cohort`: at most 100 previous `segmentation_summary.json` paths or polygon descriptors.
  Polygon-only cohorts supply count and area metrics. Use matching spatial units and methods;
  do not compare unrelated samples without explaining their differences.
- `thresholds`: overrides for `cells_per_prior` (0.9), `assigned_pct` (90),
  `fov_assigned_pct` (80), `small_area_um2` (10), `small_cell_fraction` (0.1), `sparse_cells` (2).
- `review`: visual findings and limitations as a string.

The script writes `report.pdf`, schema-v1 `segmentation_summary.json`, `flags.json`,
`provenance.json`, six page PNGs and six full-resolution crop PNGs under `figures/`.
It reads a low-resolution image overview and full-resolution crop windows; label statistics
stream in 512-pixel tiles. It does not materialize a whole-slide image or all transcripts.
Large TIFFs should be tiled or pyramidal; a strip-based TIFF may require expensive strip reads.

View **all six crop PNGs**, checking alignment, merging/splitting, stain coverage and missing
tiles. Rerun into a new output folder with concrete `review` notes. On tiny samples, repeated
crops are explicitly labelled. All-zero flags come from the overview and need visual follow-up.
Keep automatic flags visible even when an unusual biological result explains them.

Publish the final summary next to the segmentation results (e.g. the authorized output's `qc/`
folder), including the full figures, flags and provenance, so later runs can use it as a cohort
input. After viewing the crops and confirming publication, copy only the final `report.pdf`
and `segmentation_summary.json` to `outputs/report-delivery/`. Move the complete QC folders
(including draft reports and figure PNGs) into `checkpoints/` before finishing the turn. Mica
attaches every remaining file under `outputs/`; the PDF already contains the review figures,
so do not send dozens of intermediate PNGs as separate Slack attachments. Preserve the full
published QC folder and do not remove an unpublished result. If using `send_outputs`, register
only `outputs/report-delivery/` after organizing these files, never a draft or full QC folder.

Deliver the final PDF and concise findings;
include missing inputs, flagged FOVs and any open scientific choices. Do not claim success when
the report, required checks, publication, or delivery failed.
