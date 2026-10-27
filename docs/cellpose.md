# Cellpose versions and Cellpose-SAM

Mica exposes `cellpose_versions` to list installed versions and models, and `cellpose_segment`
to run the selected version on one 2D image crop. These prebuilt tools work independently of
the setting for installing arbitrary Python packages.

| Version | Models | CPU interpreter | GPU interpreter |
| --- | --- | --- | --- |
| 3.1.1.3 | `cyto3` | `/opt/sap/bin/python` | `/opt/cellpose/3.1.1.3/bin/python` |
| 4.2.1.1 (Cellpose-SAM) | `cpsam`, `cpsam_v2` | `/opt/cellpose/4.2.1.1/bin/python` | Same path in the GPU image |

The environments coexist. Selecting SAM does not replace the Cellpose 3 dependency or API used
by SAP. To switch versions, select a different version and model on the next tool call. There
is no global upgrade or downgrade that alters older runs. Unsupported versions are rejected;
adding one requires an additional pinned runtime and release while preserving older versions.
Exact dependency versions and hashes are in `docker/requirements-cellpose*.txt`.

Example requests:

- “Segment this TIFF crop with Cellpose-SAM using the original cpsam model.”
- “Repeat the crop with Cellpose 3.1.1.3 cyto3 and compare the masks.”
- “Use Cellpose-SAM cpsam_v2 on CPU for this small crop.”

Each invocation writes `masks.tif` and `provenance.json` in a new `outputs/` folder. Provenance
records the Cellpose and torch versions, model SHA256, device, channel selection, thresholds,
diameter and timing. It is an execution record, not scientific validation of the segmentation.
Existing result folders are never overwritten by the tool.

Inputs are TIFF or non-pickled NumPy arrays: one 2D numeric crop, optionally with channels, at
most 512 MiB on disk and 64 million array values. Multichannel inputs require an explicit channel
axis; channel indices are zero-based. SAM accepts up to three selected channels. Cellpose 3
accepts cytoplasm then nucleus, or one grayscale channel. Tile larger images using the existing
analysis tools, preserving spatial coordinates and reconciling boundaries before combining
masks. This tool does not implement a whole-TMA streaming pipeline.

SAM weights are baked into the image and checked against `deploy/cellpose-models.json`.
Execution cannot download another model or silently select a different version. The original
SAP workflow and its cached Cellpose 3 model remain available.

## GPU execution

The optional `gpu` tier uses `g2-standard-8`: one NVIDIA L4, eight vCPUs, 32 GiB RAM and 500 GB
disk. GPU eligibility is separate from CPU tier ceilings. CPU jobs never escalate automatically
to GPU, and GPU jobs never resize.

GPU submission requires confirmation in the requester's Slack conversation. Confirmation binds
the requester, project access, immutable image, launch choices, time allowance, compute/disk
estimate and model allowance. Defaults are on-demand and two hours. An explicit Spot choice
allows one same-tier, same-mode restart within the original deadline. Displayed cost is an
estimate, not a provider billing cap. Email GPU requests explain the Slack confirmation
requirement and do not launch without it.

Only the fixed segmentation adapter receives GPU devices. It runs in a networkless,
PID-isolated bubblewrap process, with the selected input mounted read-only and a new output
folder writable. File descriptors pin the mounts against workspace symlink changes. No parent
credentials or other project mounts are supplied. The ordinary SDK sandbox remains enabled
and does not expose GPU devices to generated shell commands.

## Build and release

Build runtime bases with `cloudbuild/build-cellpose.yaml` using a verified spatial Slack image
as `_SLACK_BASE`. Its existing flattening helper preserves image configuration and avoids Docker's
accumulated layer limit. Offline gates perform synthetic segmentation with both SAM models and
Cellpose 3, then repeat the SAP regression. CUDA wheels run these build gates on CPU; a successful
build alone does not verify GPU access.

Use the coordinated release pipeline with `_VERIFY_CELLPOSE=1`, the CPU base as `_SLACK_BASE`,
and `_GPU_BASE` / `_GPU_IMAGE` set for the GPU worker. Select `_MACHINE_TYPE=E2_HIGHCPU_32` for
model-loading gates. Before promotion, run `scripts/check_cellpose_gpu.py` on the final GPU image
in a bounded L4 Batch job with `VERIFY_GPU=1`. It checks CUDA inside the actual tool sandbox,
isolation and synthetic masks.

Deploy immutable CPU/GPU image pins together. Enable `AGENT_GPU_TIER_ENABLED`, set
`AGENT_GPU_IMAGE` to the verified digest, and configure `AGENT_GPU_MAX_SECONDS` plus the
operator-reviewed `AGENT_GPU_HOURLY_ESTIMATE_USD`. GPU execution refuses an unset image.
Future source releases must update both worker images when their application code changes.
To roll back, disable the GPU tier and restore prior API/worker pins; retain images referenced
by existing jobs.
