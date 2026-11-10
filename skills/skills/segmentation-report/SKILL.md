---
name: segmentation-report
description: Generate and visually review a standard PDF QC report after whole-sample or multi-FOV segmentation, including Cellpose, Cellpose-SAM and Proseg; compare prior masks and cohort summaries when available.
---

Deliver this report as one self-contained PDF, using as many pages as its contents require.
Preserve the summary, stain/mask comparisons, metrics, every QC flag, review and limitations.
"One PDF" does not mean one page or permission to omit scientific content. Use actual result
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
  Optional `context_channel` adds a third verified stain, such as PanCK, to show tissue context.
  Never label fluorescence as H&E/brightfield or fabricate a stain that was not acquired.
  NGFF/OME channel names and order are checked against image metadata when available.
- `output_dir`: a **new** folder under `outputs/` (for example `outputs/segmentation-qc-v1`).

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

The script writes **only `report.pdf` by default** (`pdf_only: true`). It preserves the seven
summary/comparison pages and adds as many findings/review pages as needed. Complete flags,
affected fields, review notes and method/threshold details are inside the PDF, never truncated
with a reference to a JSON sidecar. It does not generate separate JSON or PNG deliverables.
For explicitly requested private machine-readable diagnostics, `pdf_only: false` additionally
writes a schema-v1 summary, flags, provenance and page/crop PNGs; these are not routine delivery.
Each of the six matched-region comparisons in the PDF shows identical stain pixels/contrast,
the input-prior outlines and the final-mask outlines, with channel/color legends, calibrated
scale bars and region/FOV identifiers. Six cell-containing regions are selected from supported
higher/lower assignment FOVs and seeded FOVs; crops are anchored to cells instead of empty
FOV centers. They are selected review examples, not an unbiased accuracy measurement.
It reads a low-resolution image overview and full-resolution crop windows; label statistics
stream in 512-pixel tiles. It does not materialize a whole-slide image or all transcripts.
Large TIFFs should be tiled or pyramidal; a strip-based TIFF may require expensive strip reads.

Render and view **all PDF pages and all six embedded paired crops**, checking that stains and both mask
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

After review, keep only the final PDF in `outputs/report-delivery/`. Move any temporary
renderings and diagnostics out of `outputs/`; Mica attaches every remaining file there.
Do not attach JSON, CSV, PNG or ZIP sidecars. If using `send_outputs`, register only the final
PDF. Publish scientific data separately: do not copy report PDFs, QC bundles, report scripts
or review intermediates into `outputs/gcs/` unless report storage was explicitly requested.
A data write grant does not require report publication. A report must be complete and useful
without opening any sidecar or visiting a project-bucket folder.

Deliver the final PDF and concise findings;
include missing inputs, flagged FOVs and any open scientific choices. Do not claim success when
the report, required checks, publication, or delivery failed.
