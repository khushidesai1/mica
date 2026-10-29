---
name: cellpose
description: Use Cellpose or Cellpose-SAM segmentation, version/model selection, tiling, checkpoints and GPU provenance.
---



Cellpose tools:
- Call cellpose_versions to inspect installed versions. Use cellpose_segment with an explicit
  version and model: 3.1.1.3/cyto3, or 4.2.1.1/cpsam or cpsam_v2. Honor a requested version/model;
  never substitute another. Use cpsam for an unspecified Cellpose-SAM model. The versions are
  isolated; selecting one does not upgrade/downgrade the existing SAP environment.
- The tool segments one 2D TIFF or NumPy crop and returns masks.tif and provenance.json in a new
  outputs/ folder. Select channel_axis and zero-based channels explicitly for multichannel data.
  Prepare/crop/tile larger images with ordinary analysis tools. Preserve coordinates when
  combining masks. Record the version, model and parameters in the methods and delivered result.
- On a compute worker each completed Cellpose result is saved durably before the tool returns.
  Keep a tile manifest with coordinates and parameters in code/ or notes/. Use saved_checkpoints
  to discover and restore completed tiles in this project/thread before recomputing. Compare
  the saved input hash, version/model and parameters to the current request. A save error means
  inference already finished: retry checkpoint_stage on that output folder, not segmentation.
- Estimates are guidance. Long and uncertain processes are supported within the approved total
  allowance; inspect compute_allowance during long work. Save completed stages for other tools
  with checkpoint_stage. If the allowance is insufficient, preserve progress and ask for an
  additional allowance. Never lower resolution, omit tiles or change models just to fit a timer.
- GPU access is supplied only to this segmentation tool on an approved GPU worker. General Bash
  runs remain CPU sandboxed. Never report GPU use unless the tool provenance says gpu. CPU SAM
  is slower; use bounded crops. Do not install, replace or download Cellpose or model weights.

