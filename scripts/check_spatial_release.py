"""Synthetic CosMx canary: no research inputs, cloud access, Slack, or model APIs."""

import json
import sys
import tempfile
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import sap
import tifffile
import torch
import zarr
from dask.distributed import Client, LocalCluster

sys.path.insert(0, "/opt/pistachio/tools")
from sap_estimate import estimate  # noqa: E402


def main():
    torch.set_num_threads(1)
    with tempfile.TemporaryDirectory(prefix="sap-canary-") as tmp:
        root = Path(tmp)
        raw = root / "raw"
        (raw / "Morphology2D").mkdir(parents=True)
        store = root / "store"
        for name in ("images", "points", "shapes", "tables"):
            (store / name).mkdir(parents=True)
        (raw / "Morphology_ChannelID_Dictionary.txt").write_text("ID Channel\n1 DNA\n2 Membrane\n")
        y, x = np.mgrid[:128, :128]
        nuc = np.zeros((128, 128))
        cyto = nuc.copy()
        centers = [(32, 32), (32, 96), (96, 32), (96, 96)]
        for cy, cx in centers:
            radius = (x - cx) ** 2 + (y - cy) ** 2
            nuc += np.exp(-radius / 32)
            cyto += np.exp(-radius / 200)
        img = np.stack([nuc, cyto])
        img = (img / img.max() * 60000).astype(np.uint16)
        rng = np.random.default_rng(17)
        fovs = []
        tx = []
        poly = []
        for fov, (ox, oy) in enumerate([(0, 0), (128, 0), (0, 128), (128, 128)], 1):
            tifffile.imwrite(
                raw / "Morphology2D" / f"synthetic_F{fov:03}.TIF",
                img[:, ::-1, :],
                photometric="minisblack",
            )
            fovs.append(
                dict(
                    FOV=fov,
                    x_global_px=ox,
                    y_global_px=oy,
                    x_global_mm=ox * 0.0005,
                    y_global_mm=oy * 0.0005,
                )
            )
            for cy, cx in centers:
                for _ in range(300):
                    dx, dy = rng.normal(0, 3, 2)
                    tx.append(
                        dict(
                            fov=fov,
                            x_global_px=ox + cx + dx,
                            y_global_px=oy + cy + dy,
                            z=float(rng.uniform(0, 2)),
                            target="gene" + str(rng.integers(0, 10)),
                        )
                    )
                for dx, dy in [(-8, -8), (8, -8), (8, 8), (-8, 8)]:
                    poly.append(dict(x_global_px=ox + cx + dx, y_global_px=oy + cy + dy))
        pd.DataFrame(fovs).to_csv(raw / "synthetic_fov_positions_file.csv.gz", index=False)
        pd.DataFrame(tx).to_csv(raw / "synthetic_tx_file.csv.gz", index=False)
        pd.DataFrame(poly).to_csv(raw / "synthetic-polygons.csv.gz", index=False)
        sizing = estimate(raw)
        assert sizing["canvas_shape"] == [2, 256, 256]
        assert sizing["dense_canvas_gib"] == 2 * 256 * 256 * 2 / 1024**3
        print("SYNTHETIC_COSMX_ESTIMATE_PASSED", flush=True)
        with (
            LocalCluster(
                processes=False,
                protocol="inproc://",
                dashboard_address=None,
                n_workers=1,
                threads_per_worker=2,
                memory_limit=0,
            ) as cluster,
            Client(cluster),
        ):
            sap.ingest.cosmx(str(raw), str(store))
            transcripts = pd.read_parquet(store / "points/initial_transcripts.parquet")
            assert len(transcripts) == 4800
            image = zarr.open(str(store / "images/initial_imaging.ome.zarr"), mode="r")
            metadata = image.attrs.asdict()["ome"]
            assert [c["label"] for c in metadata["omero"]["channels"]] == ["DNA", "Membrane"]
            transforms = metadata["multiscales"][0]["datasets"][0]["coordinateTransformations"]
            assert next(t["scale"] for t in transforms if t["type"] == "scale") == [1, 0.5, 0.5]
            level = metadata["multiscales"][0]["datasets"][0]["path"]
            assert image[level].shape == (2, 256, 256)
            assert np.array_equal(image[level][:, :128, :128], img)
            print("SYNTHETIC_COSMX_INGEST_PASSED", flush=True)
            sap.segmentation.cellpose(
                str(store),
                ["DNA"],
                ["Membrane"],
                diameter=20,
                gpu=False,
                batch_size=1,
                chunk_overlap=16,
                temp_dir=str(root / "cellpose-temp"),
            )
            polygons = gpd.read_parquet(store / "shapes/cellpose_polygons.parquet")
            assert len(polygons) > 0 and polygons.geometry.is_valid.all()
            print("SYNTHETIC_CELLPOSE_PASSED", len(polygons), flush=True)
        print(
            json.dumps(
                {
                    "synthetic": True,
                    "ingest": "passed",
                    "cellpose": "passed",
                    "proseg": "import-only; no refinement canary",
                    "scientific_validation": False,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    main()
