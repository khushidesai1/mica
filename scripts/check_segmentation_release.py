"""Offline report/checker image gate; tiny synthetic inputs and no external calls."""

import importlib.util
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import shapely
import tifffile
from PIL import Image


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    source = Path(__file__).resolve().parents[1]
    plugin = Path("/opt/pistachio/plugin")
    for skill, script in [
        ("segmentation-report", "report.py"),
        ("spatial-analysis", "check_cosmx_sample.py"),
    ]:
        relative = Path("skills") / skill / "scripts" / script
        assert (plugin / relative).read_bytes() == (source / "skills" / relative).read_bytes()
    report = load(plugin / "skills/segmentation-report/scripts/report.py")
    check = load(plugin / "skills/spatial-analysis/scripts/check_cosmx_sample.py")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        os.chdir(root)
        raw = root / "raw/synthetic"
        extra = root / "extra/synthetic"
        (raw / "Morphology2D").mkdir(parents=True)
        extra.mkdir(parents=True)
        image = np.zeros((1, 32, 32), dtype="uint16")
        image[:, 8:20, 8:20] = 500
        tifffile.imwrite(
            raw / "Morphology2D/synthetic_F001.TIF",
            image,
            metadata={"axes": "CYX"},
            photometric="minisblack",
        )
        (raw / "Morphology_ChannelID_Dictionary.txt").write_text("ID Channel\n1 DNA\n")
        pd.DataFrame({"FOV": [1]}).to_csv(raw / "synthetic_fov_positions_file.csv.gz", index=False)
        pd.DataFrame({"x": [10]}).to_csv(raw / "synthetic_tx_file.csv.gz", index=False)
        pd.DataFrame({"x": [10]}).to_csv(raw / "synthetic-polygons.csv.gz", index=False)
        (extra / "synthetic.txt").write_text("synthetic fixture")
        paths = [p for p in root.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in paths)
        body = {
            "version": 1,
            "sample": "synthetic",
            "run_id": "smoke",
            "finished": "now",
            "n_files": len(paths),
            "total_bytes": total,
            "manifest": [
                {"name": str(p.relative_to(root)), "size": p.stat().st_size} for p in paths
            ],
        }
        (root / "record.json").write_text(json.dumps(body))
        for scope, folder in [("raw", raw), ("additional_files", extra)]:
            own = [p for p in paths if p.is_relative_to(folder)]
            fields = {
                "sample": "synthetic",
                "scope": scope,
                "status": "done",
                "verified": "crc32c",
                "scope_files": len(own),
                "scope_bytes": sum(p.stat().st_size for p in own),
                "sample_files": len(paths),
                "sample_bytes": total,
                "run_id": "smoke",
                "finished": "now",
                "record": "record.json",
            }
            (folder / "done.txt").write_text(
                "# AtoMx transfer marker v1\n"
                + "\n".join(f"{k}: {v}" for k, v in fields.items())
                + "\n\nSynthetic only.\n"
            )
        result = check.check(
            {
                "sample": "synthetic",
                "bucket_root": str(root),
                "raw": "raw/synthetic",
                "additional": "extra/synthetic",
            }
        )
        assert result["complete"], result
        pq.write_table(
            pa.table({"geometry": shapely.to_wkb([shapely.box(8, 8, 20, 20)])}),
            root / "cells.parquet",
        )
        result = report.make_report(
            {
                "sample": "synthetic",
                "method": {"name": "fixture", "version": "1"},
                "um_per_px": 0.5,
                "cells": {"path": str(root / "cells.parquet"), "units": "pixel"},
                "prior": {"path": str(root / "cells.parquet"), "units": "pixel"},
                "image": {
                    "path": str(raw / "Morphology2D/synthetic_F001.TIF"),
                    "channels": ["DNA"],
                    "nuclear_channel": "DNA",
                },
                "output_dir": "outputs/qc",
            }
        )
        assert result["cell_count"] == 1 and result["area_um2"]["median"] == 36
        assert result["report_version"] == 2
        assert all(c["cells"] == c["prior_cells"] == 1 for c in result["crops"])
        assert (root / "outputs/qc/report.pdf").read_bytes().startswith(b"%PDF")
        assert len(list((root / "outputs/qc/figures").glob("crop-*.png"))) == 6
        assert len(list((root / "outputs/qc/figures").glob("[0-9][0-9]-*.png"))) == 8
        crop = np.asarray(
            Image.open(root / "outputs/qc/figures/crop-1.png").convert("RGB"), dtype=float
        )
        for color in [[57, 217, 255], [255, 209, 102]]:
            assert np.sum(np.max(np.abs(crop - color), axis=2) < 5) > 10
        concise = root / "outputs/concise"
        concise.mkdir()
        report.render_concise(
            concise / "report.pdf",
            result,
            [],
            {
                "run_status": "completed",
                "publication_status": "verified",
                "outcome": "Synthetic segmentation completed and publication was verified.",
                "review": "Synthetic fixture only; no biological quality claim.",
            },
        )
        assert [p.name for p in concise.iterdir()] == ["report.pdf"]
        assert (concise / "report.pdf").read_bytes().startswith(b"%PDF")
    print("SEGMENTATION_REPORT_AND_COMPLETENESS_VERIFIED")


if __name__ == "__main__":
    main()
