"""Offline Cellpose adapter for the versioned environments in the Mica toolkit."""

import argparse
import hashlib
import json
import math
import os
import time
from importlib.metadata import version
from pathlib import Path

MODEL_MANIFEST = Path("/opt/cellpose/manifest.json")


VERSIONS = {"3.1.1.3": {"cyto3"}, "4.2.1.1": {"cpsam", "cpsam_v2"}}
MODEL_DIRS = {"3.1.1.3": "/opt/sap/models", "4.2.1.1": "/opt/cellpose/models"}
DEFAULTS = {
    "device": "cpu",
    "channel_axis": None,
    "channels": [],
    "diameter": None,
    "batch_size": 1,
    "flow_threshold": 0.4,
    "cellprob_threshold": 0,
    "min_size": 15,
}


def normalize(request):
    if not isinstance(request, dict):
        raise ValueError("The request must be a JSON object")
    allowed = set(DEFAULTS) | {
        "input_path",
        "input_label",
        "output_dir",
        "version",
        "model",
        "model_dir",
    }
    if set(request) - allowed:
        raise ValueError("Unknown segmentation request field")
    result = {**DEFAULTS, **request}
    for field in ("input_path", "output_dir", "version", "model"):
        if not isinstance(result.get(field), str) or not result[field]:
            raise ValueError(f"A nonempty {field} is required")
    if result["version"] not in VERSIONS or result["model"] not in VERSIONS[result["version"]]:
        raise ValueError("Choose an installed version and one of its installed models")
    if not isinstance(result["device"], str) or result["device"] not in {"cpu", "gpu", "auto"}:
        raise ValueError("device must be cpu, gpu or auto")
    if "input_label" in result and not isinstance(result["input_label"], str):
        raise ValueError("input_label must be text")
    if result["channel_axis"] is not None and (
        type(result["channel_axis"]) is not int or result["channel_axis"] not in (0, 1, 2)
    ):
        raise ValueError("channel_axis must be null, 0, 1 or 2")
    channels = result["channels"]
    if (
        not isinstance(channels, list)
        or len(channels) > 3
        or any(type(value) is not int or not 0 <= value <= 255 for value in channels)
        or len(channels) != len(set(channels))
    ):
        raise ValueError("Use unique zero-based channel indices")
    for field, minimum, maximum, integer in (
        ("diameter", 0, 10000, False),
        ("batch_size", 1, 16, True),
        ("flow_threshold", 0, 3, False),
        ("cellprob_threshold", -20, 20, False),
        ("min_size", 1, 1000000, True),
    ):
        value = result[field]
        if field == "diameter" and value is None:
            continue
        if (
            type(value) not in (int, float)
            or (integer and type(value) is not int)
            or not math.isfinite(value)
            or not minimum <= value <= maximum
            or (field == "diameter" and value == 0)
        ):
            raise ValueError(f"Invalid {field}")
    models = MODEL_DIRS[result["version"]]
    if result.get("model_dir", models) != models:
        raise ValueError("Only the installed offline model directory is supported")
    result["model_dir"] = models
    return result


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
    request = normalize(request)
    os.environ["CELLPOSE_LOCAL_MODELS_PATH"] = request["model_dir"]
    import numpy as np
    import tifffile

    source = Path(request["input_path"])
    if not source.is_file() or source.stat().st_size > 512 * 1024**2:
        raise ValueError("Use one regular image crop of at most 512 MiB")
    output = Path(request["output_dir"])
    if any((output / name).exists() for name in ("masks.tif", "provenance.json")):
        raise ValueError("Choose a new output folder; previous results are preserved")
    output.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as handle:
        input_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    image = load_image(source)
    if not np.isfinite(image).all():
        raise ValueError("Image contains non-finite values")

    import torch
    from cellpose import models

    actual = version("cellpose")
    if actual != request["version"]:
        raise ValueError(f"Expected Cellpose {request['version']}; found {actual}")
    if request["device"] == "auto":
        request["device"] = "gpu" if torch.cuda.is_available() else "cpu"
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
    use_bfloat16 = False
    if actual.startswith("4."):
        manifest = json.loads(MODEL_MANIFEST.read_text())
        if weight_hash != manifest["models"][request["model"]]["sha256"]:
            raise ValueError("Model checksum differs from the reviewed model")
        # T4 has no native BF16. Keep CPU/T4 on FP32; use BF16 on capable GPUs such as L4.
        use_bfloat16 = gpu and torch.cuda.is_bf16_supported(including_emulation=False)
        model = models.CellposeModel(
            gpu=gpu, pretrained_model=str(weights), use_bfloat16=use_bfloat16
        )
        channels = None
    else:
        model = models.Cellpose(model_type="cyto3", gpu=gpu)
        channels = [1, 2] if image.ndim == 3 and image.shape[-1] == 2 else [0, 0]
    actual_device = model.device.type
    if actual_device != ("cuda" if gpu else "cpu"):
        raise RuntimeError("Cellpose selected a different device; fallback is disabled")
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
        "input_sha256": input_hash,
        "version": actual,
        "model": request["model"],
        "model_sha256": weight_hash,
        "device": request["device"],
        "model_device": actual_device,
        "gpu_name": torch.cuda.get_device_name(model.device) if gpu else None,
        "model_precision": "bfloat16" if use_bfloat16 else "float32",
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
