# <Project name>

One or two sentences on what this project studies.

Data location: gs://<bucket>/<prefix>/   (read-only)

## Datasets
| File | Samples | Assay / platform | Cells or rows | Notes |
|---|---|---|---|---|
| raw/example_all.h5ad | A–D | Xenium | 2.4M | All samples combined, unfiltered |
| processed/example_B_qc.h5ad | B | Xenium | 612k | QC-filtered. Use this for sample B. |

## Column meanings
- Sample ID: `obs["sample_id"]`
- Cell type: `obs["annotated_cell_type"]`
- Spatial coordinates: `obsm["spatial"]` (microns)

## Defaults
- Prefer processed/ files over raw/ when both exist.
- Figure style: <colors, fonts, sizes, if the team has conventions>

## Caveats
- <for example: "Sample D has a staining artifact in the upper-left region; exclude it unless asked.">

## Glossary
- <lab shorthand the agent should understand, for example "PP zone = periportal">
