"""Decode untrusted media only when invoked by the agent's sandboxed Bash."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import warnings
from pathlib import Path

import numpy as np
from PIL import Image

from mica.agents.analysis.limits import PREVIEW_MAX_BYTES, PREVIEW_MAX_EDGE

Image.MAX_IMAGE_PIXELS = 250_000_000


def save_preview(image: Image.Image, stem: Path) -> Path:
    if image.mode.startswith("I;16") or image.mode in {"I", "F"}:
        values = np.asarray(image, dtype=np.float32)
        finite = values[np.isfinite(values)]
        low, high = np.percentile(finite, [0.5, 99.5]) if finite.size else (0, 1)
        values = np.nan_to_num(values, nan=float(low), posinf=float(high), neginf=float(low))
        scaled = np.clip((values - low) * (255 / max(float(high - low), 1)), 0, 255)
        image = Image.fromarray(scaled.astype(np.uint8))
    if "A" in image.getbands() or "transparency" in image.info:
        rgba = image.convert("RGBA")
        base = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(base, rgba)
    image = image.convert("RGB")
    image.thumbnail((PREVIEW_MAX_EDGE, PREVIEW_MAX_EDGE), Image.Resampling.LANCZOS)
    path = stem.with_suffix(".png")
    image.save(path, optimize=True)
    if path.stat().st_size <= PREVIEW_MAX_BYTES:
        return path
    path.unlink()
    path = stem.with_suffix(".jpg")
    while True:
        image.save(path, quality=85, optimize=True)
        if path.stat().st_size <= PREVIEW_MAX_BYTES:
            return path
        image = image.resize(
            (max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))),
            Image.Resampling.LANCZOS,
        )


def preview(source: Path, page: int = 1, text: bool = False) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("Source must be a regular file")
    with source.open("rb") as stream:
        head = stream.read(12)
    is_pdf = head.startswith(b"%PDF-")
    is_image = head.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"II*\0", b"MM\0*", b"BM")
    ) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    if not (is_pdf or is_image):
        raise ValueError("Unsupported image or document; render a PNG instead")
    if page < 1:
        raise ValueError("Page numbers start at 1")
    directory = Path.cwd() / ".previews"
    if directory.is_symlink():
        raise ValueError("Preview directory must not be a symlink")
    directory.mkdir(exist_ok=True)
    stat = source.stat()
    stamp = hashlib.sha256(f"{source}:{stat.st_mtime_ns}:{stat.st_size}:{page}".encode())
    name = re.sub(r"[^A-Za-z0-9_-]", "_", source.stem)[:80].lstrip(".") or "file"
    stem = directory / f"{name}-{stamp.hexdigest()[:8]}{f'-p{page}' if is_pdf else ''}"
    pages = 1
    if is_pdf:
        import pypdfium2 as pdfium

        with pdfium.PdfDocument(source) as document:
            pages = len(document)
            if page > pages:
                raise ValueError(f"PDF has {pages} pages; requested page {page}")
            pdf_page = document[page - 1]
            width, height = pdf_page.get_size()
            if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                raise ValueError("Invalid PDF page dimensions")
            bitmap = pdf_page.render(scale=min(1, PREVIEW_MAX_EDGE / max(width, height)))
            try:
                output = save_preview(bitmap.to_pil(), stem)
            finally:
                bitmap.close()
            if text:
                text_page = pdf_page.get_textpage()
                try:
                    value = text_page.get_text_range(0, min(text_page.count_chars(), 50_000))
                    stem.with_suffix(".txt").write_bytes(
                        value.encode("utf-8")[:50_000].decode("utf-8", errors="ignore").encode()
                    )
                finally:
                    text_page.close()
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(source) as original:
                width, height = original.size
                if width * height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError("Image exceeds the 250 million pixel limit")
                original.seek(0)
                output = save_preview(original, stem)
    with Image.open(output) as rendered:
        return dict(
            preview=str(output),
            width=rendered.width,
            height=rendered.height,
            source_width=width,
            source_height=height,
            page=page,
            pages=pages,
            downscaled=rendered.width < width or rendered.height < height,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--text", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(preview(args.source, args.page, args.text)))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
