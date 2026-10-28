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

Ask “Run Cellpose-SAM on a T4” or “Prefer L4 for this segmentation.” GPU choices are preferences
by default; the confirmation covers the preferred GPU and compatible alternatives, at the highest
of their hourly estimates. If the provider reports a capacity error, or a job remains queued for
`AGENT_GPU_QUEUE_WAIT_SECONDS` (default 600), Mica tries the alternative once within the original
time/model allowance. It confirms deletion of the first job before starting another and reports
the actual GPU. An atomic control-plane receipt prevents fallback once worker credentials have
been issued, so computation cannot run twice. Spot restarts retain the selected hardware.

Say “L4 only” to require that hardware. A known requirement above 16 GiB excludes T4. The default
preference is T4; if a preferred GPU is disabled, Mica offers an enabled compatible GPU in the
initial confirmation. Existing approvals remain bound to their original choices. GPU eligibility
is separate from CPU tier
ceilings. CPU jobs never escalate automatically to GPU, and GPU jobs never resize.

SAM uses FP32 on T4 and BF16 on GPUs with native BF16 support, including L4. CPU inference
remains FP32. The adapter records the precision and actual GPU name in provenance. Supported
hardware does not guarantee immediate provider capacity or that an arbitrary crop fits memory.

GPU jobs use the pinned public Batch Debian 12 host image in `batch.py`. Batch installs the
NVIDIA driver and toolkit. A fixed, bounded host setup on the dedicated VM configures NVIDIA
JIT-CDI and the generated CDI file used by newer Docker versions, skipping only
`disable-device-node-modification`, whose `/proc` overmount prevents nested private procfs.
CPU jobs omit the boot-image selector entirely so Batch chooses its supported CPU host.
The refresh service keeps the same setting, and setup verifies the generated spec before
starting the worker. This follows NVIDIA's [hook configuration](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/cdi-support.html).
Docker device cgroups still restrict device access. The worker explicitly runs as UID 65532,
drops all capabilities including MKNOD, and cannot gain privileges; the fixed segmentation
sandbox exposes only the allocated device nodes. Mica adds no privileged Docker flag.
The live gate verifies effective and bounding capabilities, no-new-privileges, and absence of
parent credentials through procfs, absence of conflicting proc submounts, and network/device isolation. CPU jobs do
not run this host setup.

GPU submission requires confirmation in the requester's Slack conversation. Confirmation binds
the requester, project access, immutable image, GPU model, launch choices, time allowance, compute/disk
estimate and model allowance. Defaults are on-demand with an eight-hour proposal, adjustable before confirmation.
Spot recoveries and checkpointed continuations retain the original approved deadline. Displayed cost is an
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
model-loading gates. When the GPU image, inference or sandbox changes, run
`scripts/check_cellpose_gpu.py` on the final GPU image in a bounded Batch job with `VERIFY_GPU=1` and
`VERIFY_GPU_TYPE=t4` or `VERIFY_GPU_TYPE=l4`, and the fake test marker
`MICA_SANDBOX_PARENT_SENTINEL=sentinel-not-a-credential` in the controller environment.
The launcher receives only PATH, matching production; the probe verifies that the controller's
marker is absent from the tool's procfs. It checks the actual device and precision, CUDA
inside the tool sandbox, isolation and synthetic masks. T4 quota is separate from L4 quota.
Keep per-hardware inference evidence explicit. Controller-only changes can reuse the exact
verified worker image and test capacity fallback with simulated provider failures; do not spend
another deployment waiting on a stockout. Unobserved L4 inference remains a verification gap,
while compatible T4 execution can proceed under the approved fallback policy.

GPU approvals and startup receipts use `write-access/compute-approvals/`, the existing
control-plane-only storage prefix. They must never use worker-writable task/project prefixes.

Deploy immutable CPU/GPU image pins together when changing worker code. Controller-only placement
updates can retain the verified worker images after compatibility checks. Enable `AGENT_GPU_TIER_ENABLED`, set
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

## Long runs and saved progress

GPU confirmation now lets the requester choose a total allowance: two hours, eight hours,
one day, three days or seven days (bounded by deployment policy). The default proposal is eight
hours. Natural-language requests can specify a different duration or an estimated compute budget;
Mica converts that budget using the highest hourly estimate among the approved GPUs. The displayed
cost remains an estimate, not a provider billing cap. Time includes startup and all worker attempts.
Existing approvals keep their original allowance; deployments do not extend them.

Runtime estimates are advisory. A healthy computation may run continuously throughout its approved
allowance. `compute_allowance` reports remaining time. At a saved stage boundary, `recover_worker`
with `cause=continue` can move to a fresh worker without another approval; the controller waits for
the previous worker to finish and preserves the original deadline and cumulative model allowance.
New GPU requests permit up to eight verified Spot recoveries and 32 explicit continuations within
that envelope, without resizing or changing approved hardware. Exhausting an allowance requires a
new confirmation; Mica asks the requester to continue with an additional allowance.

Each successful Cellpose tool call now automatically saves its output folder, input SHA256 and
method fingerprint before reporting success. These immutable recovery copies live under
`projects/_threads/<thread>/stages/<project>/` in the existing artifact bucket. They survive terminal
temporary cleanup and are scoped to the current project and thread. They are internal recovery
copies, not publication to a project's data bucket. Normal workspace snapshots still exclude raw
datasets, masks and scratch. Completed stages from other tools can be explicitly saved with
`checkpoint_stage`; running processes cannot be checkpointed by that tool.

`saved_checkpoints` lists saved stages in bounded pages and restores an explicitly selected one.
Restoration verifies the archive hash and scoped path, and never overwrites an existing destination.
Before reusing a tile, compare its input hash, model/version and parameters and preserve tile
coordinates in a manifest. New requests can resume completed work without repeating segmentation.
If a save fails after inference, the tool reports that the local result exists and requests a save
retry instead of silently rerunning the computation. Failed requests also retain their latest
available code/notes checkpoint. Saved copies follow artifact-bucket retention policy.
