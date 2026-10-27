import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.cellpose_segment import load_image, validate_shape


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
