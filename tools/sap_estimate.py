"""Header-only CosMx sizing for SAP de967639; time estimates still require probes."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from pathlib import Path


def estimate(root: Path) -> dict:
    import tifffile

    positions = list(root.glob("*_fov_positions_file.csv.gz"))
    if len(positions) != 1:
        raise ValueError("Expected exactly one FOV positions file.")
    with gzip.open(positions[0], "rt") as stream:
        rows = list(csv.DictReader(stream))
    images = {}
    for path in (root / "Morphology2D").glob("*_F*.TIF"):
        fov = int(path.stem.split("_F")[-1])
        if fov in images:
            raise ValueError("Duplicate FOV images.")
        images[fov] = path
    ids = [int(row["FOV"]) for row in rows]
    if not ids or len(set(ids)) != len(ids) or set(ids) - images.keys():
        raise ValueError("FOV positions are empty/duplicated or expected images are missing.")
    if not list(root.glob("*_tx_file.csv.gz")) or not list(root.glob("*-polygons.csv.gz")):
        raise ValueError("Required transcripts or polygons are missing.")
    channels = (root / "Morphology_ChannelID_Dictionary.txt").read_text().splitlines()[1:]
    shapes = []
    for fov in ids:
        with tifffile.TiffFile(images[fov]) as tif:
            shapes.append((tif.series[0].shape, tif.series[0].dtype.itemsize))
    if len(set(shapes)) != 1 or len(shapes[0][0]) != 3:
        raise ValueError("All FOVs must have the same channel-first 3D shape and dtype.")
    (c, tile_h, tile_w), itemsize = shapes[0]
    if c != len(channels):
        raise ValueError("Image channels disagree with the channel dictionary.")
    xs, ys = [float(r["x_global_px"]) for r in rows], [float(r["y_global_px"]) for r in rows]
    if not all(math.isfinite(v) and v >= 0 for v in xs + ys):
        raise ValueError("Unsupported nonfinite or negative global coordinates.")
    # Match the pinned SAP allocation: maximum coordinate plus tile, not max-minus-min.
    height, width = int(max(ys)) + tile_h, int(max(xs)) + tile_w
    dense = c * height * width * itemsize
    tile = c * tile_h * tile_w * itemsize
    return {
        "fovs": len(ids),
        "channels": c,
        "canvas_shape": [c, height, width],
        "dense_canvas_gib": dense / 1024**3,
        "tile_gib": tile / 1024**3,
        "source_image_gb": sum(images[f].stat().st_size for f in ids) / 1e9,
        "notes": (
            "Header sizing, not measured peak memory or runtime. SAP reads all image channels. "
            "Global-coordinate gaps enlarge the canvas. Probe sparse and dense regions with "
            "the original coordinate extent; include float buffers, task concurrency, pyramid, "
            "transcripts, spill, completed stages and temporary archive copies. Closed, complete "
            "exports must be verified separately. "
            "Do not assume chunking removes global allocations."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    print(json.dumps(estimate(parser.parse_args().input), indent=2, allow_nan=False))
