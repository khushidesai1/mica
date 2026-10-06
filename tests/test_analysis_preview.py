import json
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

TOOL = Path(__file__).resolve().parents[1] / "tools/preview.py"


def preview(path, *args):
    return subprocess.run(
        [sys.executable, str(TOOL), str(path), *args],
        cwd=path.parent,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("name", ["large.png", "wrong.csv"])
def test_large_image_is_bounded_and_sniffed(tmp_path, name):
    source = tmp_path / name
    Image.new("RGB", (6000, 4000), "red").save(source, format="PNG")
    before = source.read_bytes()
    result = preview(source)
    assert result.returncode == 0, result.stderr
    info = json.loads(result.stdout)
    output = Path(info["preview"])
    assert output.parent == tmp_path / ".previews"
    assert max(info["width"], info["height"]) <= 1568
    assert output.stat().st_size <= 1_500_000
    assert info["source_width"] == 6000 and info["downscaled"]
    assert source.read_bytes() == before


def test_16_bit_tiff(tmp_path):
    source = tmp_path / "gray.tiff"
    Image.fromarray(np.arange(65536, dtype=np.uint16).reshape(256, 256)).save(source)
    result = preview(source)
    assert result.returncode == 0, result.stderr
    with Image.open(json.loads(result.stdout)["preview"]) as img:
        assert img.mode == "RGB" and img.getextrema()[0] == (0, 255)


def test_pdf_page_and_text(tmp_path):
    import matplotlib

    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    source = tmp_path / "two.pdf"
    with PdfPages(source) as pdf:
        for text in ("First page", "Second page"):
            fig, ax = plt.subplots()
            ax.text(0.2, 0.5, text)
            pdf.savefig(fig)
            plt.close(fig)
    result = preview(source, "--page", "2", "--text")
    assert result.returncode == 0, result.stdout + result.stderr
    info = json.loads(result.stdout)
    assert info["page"] == 2 and info["pages"] == 2
    assert max(info["width"], info["height"]) <= 1568
    assert "Second page" in Path(info["preview"]).with_suffix(".txt").read_text()
    assert preview(source, "--page", "3").returncode == 2


def test_decompression_bomb_rejected(tmp_path):
    def chunk(kind, value):
        return (
            struct.pack(">I", len(value))
            + kind
            + value
            + struct.pack(">I", zlib.crc32(kind + value))
        )

    source = tmp_path / "bomb.png"
    source.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 25001, 10000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", b"")
    )
    result = preview(source)
    assert result.returncode == 2
    assert "pixel" in json.loads(result.stdout)["error"].lower()


def test_svg_rejected(tmp_path):
    source = tmp_path / "figure.svg"
    source.write_text("<svg/>")
    result = preview(source)
    assert result.returncode == 2
    assert "render a PNG instead" in json.loads(result.stdout)["error"]


def test_noisy_image_uses_jpeg_and_stays_within_budget(tmp_path):
    source = tmp_path / "noise.png"
    rng = np.random.default_rng(42)
    Image.fromarray(rng.integers(0, 256, (1568, 1568, 3), dtype=np.uint8)).save(source)
    result = preview(source)
    assert result.returncode == 0
    path = Path(json.loads(result.stdout)["preview"])
    assert path.suffix == ".jpg" and path.stat().st_size <= 1_500_000
