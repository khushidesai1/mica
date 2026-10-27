"""Fixed offline adapter shared by the supported, separately installed Cellpose versions."""

import argparse
import hashlib
import json
import math
import os
import time
from importlib.metadata import version
from pathlib import Path


def validate_shape(shape, kind):
    if (
        kind not in "uif"
        or len(shape) not in (2, 3)
        or min(shape) < 1
        or math.prod(shape) > 64_000_000
    ):
        raise ValueError("Use one 2D numeric image crop with at most 64 million values")


def load_image(source):
    import numpy as np
    import tifffile

    if source.suffix.lower() == ".npy":
        image = np.load(source, allow_pickle=False, mmap_mode="r")
        validate_shape(image.shape, image.dtype.kind)
        return image
    with tifffile.TiffFile(source) as handle:
        series = handle.series[0]
        validate_shape(series.shape, series.dtype.kind)
        return series.asarray()


def run(request):
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = request["model_dir"]
    import numpy as np
    import tifffile

    image = load_image(Path(request["input_path"]))
    if not np.isfinite(image).all():
        raise ValueError("Image contains non-finite values")

    import torch
    from cellpose import models

    actual = version("cellpose")
    if actual != request["version"]:
        raise ValueError(f"Expected Cellpose {request['version']}; found {actual}")
    gpu = request["device"] == "gpu"
    if gpu and not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable inside the segmentation sandbox")
    torch.set_num_threads(min(8, os.cpu_count() or 1))
    if image.ndim == 3:
        axis = request["channel_axis"]
        if axis is None:
            raise ValueError("Specify channel_axis for a multi-channel image")
        image = np.moveaxis(image, axis, -1)
        if request["channels"]:
            image = image[..., request["channels"]]
        if not 1 <= image.shape[-1] <= (2 if actual.startswith("3.") else 3):
            raise ValueError("Select at most two channels for Cellpose 3 or three for SAM")
    elif request["channel_axis"] is not None or request["channels"]:
        raise ValueError("A grayscale image has no channel axis or channel selection")
    weights = Path(request["model_dir"]) / request["model"]
    if not weights.is_file():
        raise ValueError("The requested model weights are not installed; downloading is disabled")
    with weights.open("rb") as handle:
        weight_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual.startswith("4."):
        manifest = json.loads(Path("/opt/cellpose/manifest.json").read_text())
        if weight_hash != manifest["models"][request["model"]]["sha256"]:
            raise ValueError("Model checksum differs from the reviewed model")
        model = models.CellposeModel(gpu=gpu, pretrained_model=str(weights), use_bfloat16=gpu)
        channels = None
    else:
        model = models.Cellpose(model_type="cyto3", gpu=gpu)
        channels = [1, 2] if image.ndim == 3 and image.shape[-1] == 2 else [0, 0]
    started = time.monotonic()
    result = model.eval(
        image,
        channels=channels,
        channel_axis=-1 if image.ndim == 3 else None,
        diameter=request["diameter"],
        batch_size=request["batch_size"],
        flow_threshold=request["flow_threshold"],
        cellprob_threshold=request["cellprob_threshold"],
        min_size=request["min_size"],
    )
    masks = np.asarray(result[0], dtype=np.uint32)
    if masks.ndim != 2 or masks.shape != image.shape[:2]:
        raise ValueError("Unexpected mask dimensions")
    output = Path(request["output_dir"])
    tifffile.imwrite(output / "masks.tif", masks)
    provenance = {
        "version": actual,
        "model": request["model"],
        "model_sha256": weight_hash,
        "device": request["device"],
        "torch_version": torch.__version__,
        "input_shape": list(image.shape),
        "cell_count": int(len(np.unique(masks)) - (0 in masks)),
        "parameters": {
            k: v for k, v in request.items() if k not in ("model_dir", "output_dir", "input_label")
        },
        "segmentation_seconds": time.monotonic() - started,
        "outputs": ["masks.tif", "provenance.json"],
    }
    provenance["parameters"]["input_path"] = request.get("input_label", request["input_path"])
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(json.dumps(provenance))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    run(json.loads(parser.parse_args().request))
