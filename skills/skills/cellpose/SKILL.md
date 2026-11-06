---
name: cellpose
description: Use Cellpose or Cellpose-SAM segmentation, version/model selection, tiling, checkpoints and GPU provenance.
---

Use the named environments in the toolkit: `cellpose-3.1.1.3` for `cyto3`, or
`cellpose-4.2.1.1` for `cpsam` and `cpsam_v2`. Honor an explicit version/model; default to
`cpsam` for an unspecified Cellpose-SAM model. These are isolated environments, so selecting
one does not upgrade or downgrade SAP. Do not install, replace or download Cellpose or weights.

The bundled `scripts/segment.py` accepts `--request '<JSON>'` and writes `masks.tif` plus
`provenance.json`. Copy it to `code/segment.py` before execution. Required request keys are
`input_path`, `output_dir`, `version` and `model`. Set `device` explicitly to `cpu` or `gpu`.
For multichannel data, specify `channel_axis` and zero-based `channels` (cytoplasm then nucleus
for version 3). Other keys include `diameter`, `batch_size`, `flow_threshold`,
`cellprob_threshold` and `min_size`; record the exact parameters in the delivered methods.

Use one 2D TIFF or NumPy crop per call, at most 512 MiB and 64 million values. Prepare and tile
larger images with ordinary analysis scripts, preserve coordinates, and keep a tile manifest in
`code/` or `notes/`. Always choose a new `outputs/` folder so prior results remain available.

For bounded CPU crops, run the copied script in Bash with the interpreter listed for the
selected environment. For example, the SAM interpreter is `/opt/cellpose/4.2.1.1/bin/python`:

```bash
/opt/cellpose/4.2.1.1/bin/python code/segment.py --request '{"input_path":"crop.tif","output_dir":"outputs/crop-cpu","version":"4.2.1.1","model":"cpsam","device":"cpu"}'
```

For substantial CUDA segmentation, request a suitably assessed GPU worker. Bash remains
CPU-only even on that worker. Call `run_gpu` with the selected environment id, `script` set to
`code/segment.py`, and `arguments` containing `--request` plus the JSON string. Set both the
request's `output_dir` and the tool's `output_dir` to the same new folder, and set the request's
`device` to `gpu`. Include input versions, tiling coordinates and parameters in the tool's
`input_fingerprint`. Version 4 uses the `cellpose-4.2.1.1` environment.

Never report GPU use unless `provenance.json` says `device: gpu` and `model_device: cuda`.
The provenance also records the actual device name, model precision, input hash, version,
model and parameters. Available hardware alone does not prove GPU execution.

Successful `run_gpu` results save durably before the tool returns. If the save fails, inference
has already finished: preserve the folder and retry `checkpoint_stage`, not segmentation.
For CPU scripts, save completed expensive stages with `checkpoint_stage` explicitly. Use
`saved_checkpoints` to discover and restore completed tiles; compare input hashes, model,
version and parameters before reuse. Scratch and running processes do not survive a restart.

Estimates are advisory. Long healthy processes can continue within the approved allowance;
check `compute_allowance` during long work. Save progress and request additional allowance if
needed. Never lower resolution, omit tiles, or change models to fit a timer.

After whole-sample or multi-FOV segmentation, use `mica:segmentation-report` to produce the
standard PDF and summary. Inspect the crop overlays, add visual review notes, and publish the
summary alongside the result. Keep the exact model/version and measured pixel scale in the report.
