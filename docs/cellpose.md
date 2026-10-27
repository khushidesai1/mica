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
diameter, model precision and timing. It is an execution record, not scientific validation of the segmentation.
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

The optional `gpu` tier supports a per-request `gpu_type` of `t4` or `l4`:

| GPU | Machine | GPU memory | CPUs / system RAM | Disk |
| --- | --- | --- | --- | --- |
| T4 | `n1-standard-8` + one `nvidia-tesla-t4` | 16 GiB | 8 / 30 GiB | 500 GB |
| L4 | `g2-standard-8` | 24 GiB | 8 / 32 GiB | 500 GB |

Ask “Run Cellpose-SAM on a T4” or “Use an L4 for this segmentation.” Mica honors an explicit
model; the default for an unspecified GPU is T4. The router sees the currently enabled choices
and can select L4 for work needing more GPU memory. A hardware change requires a new confirmed
request; Spot retries retain the approved model. GPU eligibility is separate from CPU tier
ceilings. CPU jobs never escalate automatically to GPU, and GPU jobs never resize.

SAM uses FP32 on T4 and BF16 on GPUs with native BF16 support, including L4. CPU inference
remains FP32. The adapter records the precision and actual GPU name in provenance. Supported
hardware does not guarantee immediate provider capacity or that an arbitrary crop fits memory.

GPU jobs use the pinned public Batch Debian 12 host image in `batch.py`. Batch installs the
NVIDIA driver and toolkit. A fixed, bounded host setup on the dedicated VM configures NVIDIA
JIT-CDI and skips only `disable-device-node-modification`, whose `/proc` overmount prevents
nested private procfs. This follows NVIDIA's [hook configuration](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).
Docker device cgroups still restrict device access. The worker explicitly runs as UID 65532,
drops all capabilities including MKNOD, and cannot gain privileges; the fixed segmentation
sandbox exposes only the allocated device nodes. Mica adds no privileged Docker flag.
The live gate verifies effective and bounding capabilities, no-new-privileges, and absence of
parent credentials through procfs, in addition to network and device isolation. CPU jobs do
not run this host setup.

GPU submission requires confirmation in the requester's Slack conversation. Confirmation binds
the requester, project access, immutable image, GPU model, launch choices, time allowance, compute/disk
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
and `_GPU_BASE` / `_GPU_IMAGE` set for the GPU worker. The pipeline uses `E2_HIGHCPU_32` for
model-loading gates. Before promotion, run `scripts/check_cellpose_gpu.py` on the final GPU image
in a bounded Batch job for each enabled GPU model with `VERIFY_GPU=1` and
`VERIFY_GPU_TYPE=t4` or `VERIFY_GPU_TYPE=l4`. It checks the actual device and precision, CUDA
inside the tool sandbox, isolation and synthetic masks. T4 quota is separate from L4 quota.

Deploy immutable CPU/GPU image pins together. Enable `AGENT_GPU_TIER_ENABLED`, set
`AGENT_GPU_IMAGE` to the verified digest, and set `AGENT_GPU_TYPES` to the comma-separated models
whose live checks passed. Set `AGENT_GPU_DEFAULT_TYPE` to an enabled model (default `t4`).
Configure `AGENT_GPU_MAX_SECONDS` and the operator-reviewed hourly compute/disk estimates:
`AGENT_GPU_T4_HOURLY_ESTIMATE_USD` for T4 (default $1.50), and
`AGENT_GPU_HOURLY_ESTIMATE_USD` for L4 (default $2). GPU execution refuses an unset image or a
model excluded from `AGENT_GPU_TYPES`.
Future source releases must update both worker images when their application code changes.
To roll back, disable the GPU tier and restore prior API/worker pins; retain images referenced
by existing jobs.

Hardware configuration follows [Google's GPU machine types](https://docs.cloud.google.com/compute/docs/gpus/)
and [Batch GPU configuration](https://docs.cloud.google.com/batch/docs/create-run-job-gpus).
Precision uses Cellpose's [documented `use_bfloat16` option](https://cellpose.readthedocs.io/en/latest/api.html).
