---
name: segmentation-report
description: Generate and visually review a standard PDF QC report after whole-sample or multi-FOV segmentation, including Cellpose, Cellpose-SAM and Proseg; compare prior masks and cohort summaries when available.
---

Follow the requested report format. For ambient segmentation, deliver one concise PDF unless
the task explicitly requests a detailed review. Report delivery and project-bucket publication
are separate choices: a write grant does not authorize adding an unwanted QC bundle.
Use actual result statistics, never copied or hard-coded statistics from a previous batch. Copy
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
  Optional `context_channel` adds a third verified stain, such as PanCK, to show tissue context.
  Never label fluorescence as H&E/brightfield or fabricate a stain that was not acquired.
  NGFF/OME channel names and order are checked against image metadata when available.
- `output_dir`: a **new** folder under `outputs/` (for example `outputs/segmentation-qc-v1`).

For a concise report, add `format: "concise"`, `run_status` (`completed`, `failed`, or
`incomplete`), `publication_status` (`verified`, `failed`, or `not attempted`), an `outcome`
of at most 220 characters, and concrete `review` findings/next steps of at most 650 characters.
The statuses must come from observed run and publication checks. This writes **only one
single-page `report.pdf`**, with actual metrics, automatic-flag counts and the review. It does
not write JSON summaries, flags, provenance, page images or crop folders. Existing input,
scale, coordinate and statistical checks still run. Review representative aligned stain/mask
crops privately before reporting success; concise output does not waive scientific checks.
If processing failed before valid report inputs exist, create a short failure PDF with the
observed failure and next step; leave unavailable metrics unavailable. Never fabricate inputs
to make this generator succeed. Render and inspect the final PDF for legibility.

Keep exactly this final PDF in `outputs/report-delivery/` for Slack/email. Do not attach JSON,
scripts, archives, or review PNGs. Do not stage the PDF or any QC bundle under `outputs/gcs/`
unless report storage was explicitly requested. Temporary visual checks belong in the private
workspace. A successful data publication and a generated PDF do not prove delivery.

The following detailed-report workflow applies only when that format is requested. Set
`format: "detailed"` (the script's compatibility default) to produce its review bundle.

All coordinates must use the same image frame: origin at the upper left, x right and y down.
The image is a single 2D plane. Label shape must match it. Polygon coordinates in µm are
divided by `um_per_px`; translations and flipped y frames must be resolved before calling.
Inspect source metadata and a representative overlay; stop if scale or alignment is uncertain.
Do not infer scale from the algorithm's default, or relabel pixel areas as µm².

Optional inputs:

- `prior`: the same sample's input masks, using the same schema as `cells`.
  Supply it whenever available. Set `prior_label` (e.g. `AtoMx prior`) and `result_label`
  (e.g. `Proseg`) to identify the actual comparison. A prior is not interchangeable with an
  older segmentation pipeline: disclose when no older pipeline result is available.
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
  `min_fov_transcripts` (100) is the minimum sampled-transcript denominator for FOV assignment
  ranking/low-assignment flags. Lower-support FOVs remain visible as a sampling limitation.
- `review`: visual findings and limitations as a string.

The script writes an eight-page `report.pdf`, schema-v1 `segmentation_summary.json` with
`report_version: 2`, `flags.json`, `provenance.json`, eight page PNGs and six matched-region
comparison PNGs under `figures/`. Each comparison shows identical stain pixels/contrast,
the input-prior outlines and the final-mask outlines, with channel/color legends, calibrated
scale bars and region/FOV identifiers. Six cell-containing regions are selected from supported
higher/lower assignment FOVs and seeded FOVs; crops are anchored to cells instead of empty
FOV centers. They are selected review examples, not an unbiased accuracy measurement.
It reads a low-resolution image overview and full-resolution crop windows; label statistics
stream in 512-pixel tiles. It does not materialize a whole-slide image or all transcripts.
Large TIFFs should be tiled or pyramidal; a strip-based TIFF may require expensive strip reads.

View **all eight pages and all six paired crop PNGs**, checking that the stains and both mask
sets are actually visible and spatially aligned, along with merging/splitting, stain coverage
and missing tiles. Empty background or invisible outlines do not satisfy visual review:
correct crop selection/alignment and regenerate before delivery. Do not merely note that five
of six crops are background and send the PDF anyway. Examine suspicious stain-rich regions
with missing masks separately; selecting cell-containing crops can otherwise hide omissions.
Rerun into a new output folder with concrete `review` notes describing observed regions and
remaining uncertainty. On tiny samples, repeated crops are explicitly labelled. All-zero
flags come from the overview and need visual follow-up.
Keep automatic flags visible even when an unusual biological result explains them.
Prior/final cell counts and area distributions are same-sample descriptive comparisons;
count ratios do not prove cell identity retention or good boundaries. Cohort count/area
differences may reflect tissue amount/composition. Do not present a lone new-sample point as
a cohort transcript comparison, rank a few sampled transcripts as best/worst segmentation,
or claim that high transcript assignment alone validates the masks.

Only if explicitly requested, publish the detailed bundle next to the segmentation results
(e.g. the authorized output's `qc/` folder) for later cohort use. After viewing the crops,
copy only the final `report.pdf` to `outputs/report-delivery/`. Move the complete QC folders
(including draft reports and figure PNGs) into `checkpoints/` before finishing the turn. Mica
attaches every remaining file under `outputs/`; the PDF already contains the review figures,
so do not send intermediate PNGs or JSON as separate Slack attachments. If using `send_outputs`, register
only `outputs/report-delivery/` after organizing these files, never a draft or full QC folder.

Deliver the final PDF and concise findings;
include missing inputs, flagged FOVs and any open scientific choices. Do not claim success when
the report, required checks, publication, or delivery failed.
