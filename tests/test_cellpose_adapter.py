import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skills.skills.cellpose.scripts import segment as cellpose_segment
from skills.skills.cellpose.scripts.segment import load_image, validate_shape


@pytest.mark.parametrize(
    "shape,kind", [((100000, 100000), "f"), ((3,), "u"), ((0, 3), "u"), ((2, 2), "O")]
)
def test_unbounded_or_non_numeric_image_header_is_rejected(shape, kind):
    with pytest.raises(ValueError, match="crop"):
        validate_shape(shape, kind)


def test_large_compressed_tiff_is_rejected_before_decoding(monkeypatch):
    series = SimpleNamespace(
        shape=(100000, 100000), dtype=SimpleNamespace(kind="u"), asarray=MagicMock()
    )
    handle = MagicMock()
    handle.__enter__.return_value.series = [series]
    monkeypatch.setitem(sys.modules, "tifffile", SimpleNamespace(TiffFile=lambda _: handle))
    with pytest.raises(ValueError, match="crop"):
        load_image(Path("compressed.tif"))
    series.asarray.assert_not_called()


def test_numpy_is_read_only_memory_mapped_before_size_validation(monkeypatch):
    loader = MagicMock(
        return_value=SimpleNamespace(shape=(100000, 100000), dtype=SimpleNamespace(kind="f"))
    )
    monkeypatch.setitem(sys.modules, "numpy", SimpleNamespace(load=loader))
    monkeypatch.setitem(sys.modules, "tifffile", SimpleNamespace())
    with pytest.raises(ValueError, match="crop"):
        load_image(Path("large.npy"))
    loader.assert_called_once_with(Path("large.npy"), allow_pickle=False, mmap_mode="r")


@pytest.mark.parametrize(
    "device,native_bf16,expected",
    [("gpu", False, "float32"), ("gpu", True, "bfloat16"), ("cpu", False, "float32")],
)
def test_sam_uses_supported_precision_and_records_it(
    monkeypatch, tmp_path, device, native_bf16, expected
):
    import numpy as np

    weights = tmp_path / "cpsam"
    weights.write_bytes(b"test model")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"models": {"cpsam": {"sha256": hashlib.sha256(weights.read_bytes()).hexdigest()}}}
        )
    )
    monkeypatch.setattr(cellpose_segment, "MODEL_MANIFEST", manifest)
    monkeypatch.setitem(cellpose_segment.MODEL_DIRS, "4.2.1.1", str(tmp_path))
    (tmp_path / "crop.tif").write_bytes(b"synthetic")
    monkeypatch.setattr(cellpose_segment, "version", lambda _: "4.2.1.1")
    monkeypatch.setattr(
        cellpose_segment, "load_image", lambda _: np.zeros((8, 8), dtype=np.float32)
    )
    cuda = SimpleNamespace(
        is_available=lambda: True,
        is_bf16_supported=MagicMock(return_value=native_bf16),
        get_device_name=lambda _: "L4" if native_bf16 else "T4",
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=cuda, set_num_threads=lambda _: None, __version__="2.7.1"),
    )
    model = SimpleNamespace(
        device=SimpleNamespace(type="cuda" if device == "gpu" else "cpu"),
        eval=MagicMock(return_value=(np.zeros((8, 8)),)),
    )
    factory = MagicMock(return_value=model)
    monkeypatch.setitem(
        sys.modules, "cellpose", SimpleNamespace(models=SimpleNamespace(CellposeModel=factory))
    )
    monkeypatch.setitem(sys.modules, "tifffile", SimpleNamespace(imwrite=MagicMock()))
    cellpose_segment.run(
        {
            "model_dir": str(tmp_path),
            "input_path": str(tmp_path / "crop.tif"),
            "version": "4.2.1.1",
            "device": device,
            "model": "cpsam",
            "channel_axis": None,
            "channels": [],
            "diameter": 20,
            "batch_size": 8,
            "flow_threshold": 0.4,
            "cellprob_threshold": 0,
            "min_size": 15,
            "output_dir": str(tmp_path),
        }
    )
    assert factory.call_args.kwargs["use_bfloat16"] == (expected == "bfloat16")
    if device == "gpu":
        cuda.is_bf16_supported.assert_called_once_with(including_emulation=False)
    else:
        cuda.is_bf16_supported.assert_not_called()
    provenance = json.loads((tmp_path / "provenance.json").read_text())
    assert provenance["model_precision"] == expected and provenance["device"] == device
