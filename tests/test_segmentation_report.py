"""Real, tiny scientific fixtures for the bundled report and completeness checker."""

import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pypdfium2 as pdfium
import pytest
import shapely
import tifffile
import zarr

ROOT = Path(__file__).parents[1]


def script(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


report = script("segmentation_report", "skills/skills/segmentation-report/scripts/report.py")
check = script("check_cosmx", "skills/skills/spatial-analysis/scripts/check_cosmx_sample.py")


def fixture(root):
    root.mkdir(parents=True, exist_ok=True)
    labels = np.zeros((128, 192), dtype=np.uint32)
    polygons, positions = [], []
    for i in range(6):
        x, y = (i % 3) * 64 + 20, (i // 3) * 64 + 20
        labels[y : y + 8, x : x + 8] = i + 1
        polygons.append(shapely.box(x, y, x + 8, y + 8))
        positions.append(
            {"id": str(i), "x0": (i % 3) * 64, "y0": (i // 3) * 64, "width": 64, "height": 64}
        )
    yy, xx = np.mgrid[:128, :192]
    image = np.stack([(labels > 0) * 1000, ((xx + yy) % 30) + 10]).astype("uint16")
    image[:, 64:, 128:] = 0
    tifffile.imwrite(root / "image.tif", image, metadata={"axes": "CYX"}, photometric="minisblack")
    tifffile.imwrite(root / "labels.tif", labels)
    pq.write_table(pa.table({"geometry": shapely.to_wkb(polygons)}), root / "cells.parquet")
    pd.DataFrame(positions).to_csv(root / "fovs.csv", index=False)
    pd.DataFrame(
        {
            "x": [22, 25, 90, 92, 24, 27],
            "y": [22, 25, 23, 26, 90, 92],
            "cell_id": [0, 1, 2, 0, 4, 0],
        }
    ).to_parquet(root / "tx.parquet", index=False)
    return {
        "sample": "synthetic",
        "method": {"name": "fixture", "version": "1", "parameters": {}},
        "um_per_px": 0.25,
        "cells": {"path": str(root / "cells.parquet"), "units": "pixel"},
        "image": {
            "path": str(root / "image.tif"),
            "channels": ["DNA", "Membrane"],
            "nuclear_channel": "DNA",
            "membrane_channel": "Membrane",
        },
        "fovs": {"path": str(root / "fovs.csv")},
        "transcripts": str(root / "tx.parquet"),
        "review": "Synthetic fixture. Nuclear signal and outlines align. Last tile is blank.",
        "output_dir": "outputs/qc",
    }


@pytest.mark.parametrize("kind", ["polygons", "tiff", "zarr"])
def test_report_outputs_flags_and_pdf_pages(tmp_path, monkeypatch, kind):
    monkeypatch.chdir(tmp_path)
    request = fixture(tmp_path / "inputs")
    if kind != "polygons":
        path = tmp_path / "inputs/labels.tif"
        if kind == "zarr":
            array = zarr.open(
                str(tmp_path / "inputs/labels.zarr"),
                mode="w",
                shape=(128, 192),
                chunks=(32, 32),
                dtype="uint32",
            )
            array[:] = tifffile.imread(path)
            path = tmp_path / "inputs/labels.zarr"
        request["cells"] = {"path": str(path)}
    request["prior"] = {"path": str(tmp_path / "inputs/cells.parquet"), "units": "pixel"}
    request["cohort"] = [request["prior"]]
    summary = report.make_report(request)
    assert summary["report_version"] == 2
    assert summary["prior"]["cell_count"] == 6
    assert summary["cell_count"] == 6
    assert summary["area_um2"]["median"] == pytest.approx(4)
    assert summary["assigned_pct"] == 50
    assert summary["transcripts_sampled"] == 6
    assert summary["fov_score_metric"] == "assigned_fraction"
    assert all(r["score"] is None for r in summary["fovs"] if r["assigned_pct"] is None)
    flags = json.loads(Path("outputs/qc/flags.json").read_text())
    assert {f["code"] for f in flags} >= {"low_assignment", "small_cells", "zero_image"}
    assert len(summary["crops"]) == 6
    assert all(crop["cells"] >= 1 and crop["prior_cells"] >= 1 for crop in summary["crops"])
    with pdfium.PdfDocument("outputs/qc/report.pdf") as pdf:
        assert len(pdf) == 8
        text = "\n".join(page.get_textpage().get_text_range() for page in pdf)
        assert "Matched stain and mask comparison" in text
        assert "Input prior" in text and "Final segmentation" in text
        assert "sampled transcripts (n)" in text
        for page in pdf:
            rendered = page.render(scale=0.3).to_numpy()
            assert rendered.shape[0] > 100 and rendered.std() > 5
    assert len(list(Path("outputs/qc/figures").glob("crop-*.png"))) == 6
    assert json.loads(Path("outputs/qc/provenance.json").read_text())["seed"] == 17


def test_crops_move_into_off_center_cells_instead_of_empty_fov_centers():
    from types import SimpleNamespace

    cells = SimpleNamespace(xy=np.array([[80, 100], [95, 120], [120, 110], [100, 150], [150, 150]]))
    row = {"box": [0, 0, 4256, 4256], "cells": 5}
    box, count = report.crop_box(row, cells)
    assert count == 5
    assert report.inside(cells.xy, box).all()
    assert box[2] - box[0] == box[3] - box[1] == 512
    assert box[2] < 4256 / 2 and box[3] < 4256 / 2


def test_concise_report_is_one_pdf_with_checks_and_no_sidecars(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = fixture(tmp_path / "inputs") | {
        "format": "concise",
        "run_status": "completed",
        "publication_status": "verified",
        "outcome": "Segmentation finished and the data were saved; review the blank tile.",
    }
    summary = report.make_report(request)
    assert summary["cell_count"] == 6 and summary["assigned_pct"] == 50
    assert sorted(p.name for p in Path(request["output_dir"]).iterdir()) == ["report.pdf"]
    with pdfium.PdfDocument(str(Path(request["output_dir"]) / "report.pdf")) as pdf:
        assert len(pdf) == 1
        text = pdf[0].get_textpage().get_text_range()
        assert "Run: Completed" in text and "50.0%" in text
        assert "zero image: 1" in text and "low assignment: 1" in text
        assert "Last tile is blank" in text and "biological accuracy" in text
        assert pdf[0].render(scale=0.5).to_numpy().std() > 5


@pytest.mark.parametrize(
    "change",
    [{"run_status": None}, {"publication_status": None}, {"review": ""}, {"review": "x" * 651}],
)
def test_concise_report_requires_observed_status_and_bounded_review(tmp_path, monkeypatch, change):
    monkeypatch.chdir(tmp_path)
    request = (
        fixture(tmp_path / "inputs")
        | {
            "format": "concise",
            "run_status": "completed",
            "publication_status": "verified",
            "outcome": "Completed with review findings.",
        }
        | change
    )
    with pytest.raises(ValueError, match="Concise reports require"):
        report.make_report(request)
    assert not Path(request["output_dir"]).exists()


def test_sparse_assignment_estimates_are_not_ranked_or_flagged_as_bad(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = fixture(tmp_path / "inputs")
    # An unsupported zero-percent FOV must not be called the worst segmentation.
    frame = pd.DataFrame(
        {"x": [22] + [90] * 120, "y": [22] + [25] * 120, "cell_id": [0] + [2] * 108 + [0] * 12}
    )
    frame.to_parquet(tmp_path / "inputs/tx.parquet", index=False)
    monkeypatch.setattr(report, "render", lambda *a: None)
    summary = report.make_report(request)
    low = next(r for r in summary["fovs"] if r["id"] == "0")
    supported = next(r for r in summary["fovs"] if r["id"] == "1")
    assert low["transcripts_sampled"] == 1 and low["assigned_pct"] == 0
    assert low["score"] is None and not low["assignment_ranked"]
    assert supported["transcripts_sampled"] == 120 and supported["score"] == pytest.approx(0.9)
    flags = json.loads(Path("outputs/qc/flags.json").read_text())
    assert any(f["code"] == "limited_fov_sampling" for f in flags)
    assert not any(f["code"] == "low_fov_assignment" and f.get("fov") == "0" for f in flags)
    assert summary["crops"][0]["fov"] == "1"
    assert summary["crops"][0]["reason"] == "Higher sampled assignment"


def test_channel_names_must_match_ngff_metadata(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = fixture(tmp_path / "inputs")
    path = tmp_path / "inputs/image.ome.zarr"
    group = zarr.open_group(str(path), mode="w")
    group.create_array("s0", data=tifffile.imread(tmp_path / "inputs/image.tif"))
    group.attrs["ome"] = {
        "multiscales": [{"axes": ["c", "y", "x"], "datasets": [{"path": "s0"}]}],
        "omero": {"channels": [{"label": "DNA"}, {"label": "Membrane"}]},
    }
    request["image"]["path"] = str(path)
    request["image"]["channels"] = ["Membrane", "DNA"]
    with pytest.raises(ValueError, match="Channel names/order"):
        report.make_report(request)


def test_outlines_do_not_invent_mask_edges_along_crop_borders(tmp_path):
    path = tmp_path / "large.parquet"
    pq.write_table(pa.table({"geometry": shapely.to_wkb([shapely.box(0, 0, 1000, 1000)])}), path)
    cells = report.Cells({"path": str(path), "units": "pixel"}, 1)
    fig, ax = report.plt.subplots()
    try:
        cells.outlines(ax, [100, 100, 200, 200])
        assert len(ax.lines) == 1
        assert set(ax.lines[0].get_xdata()) == {0, 1000}
        assert set(ax.lines[0].get_ydata()) == {0, 1000}
    finally:
        report.plt.close(fig)


def test_optional_inputs_are_unavailable_not_zero_and_selection_repeats(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    request = fixture(tmp_path / "inputs")
    request.pop("transcripts")
    request.pop("fovs")
    summary = report.make_report(request)
    assert summary["assigned_pct"] is None and summary["transcripts_per_cell"] is None
    assert all(c["reason"].startswith("Repeat") for c in summary["crops"][1:])
    assert report.select_crops(summary["fovs"]) == report.select_crops(summary["fovs"])


def test_window_reads_are_bounded_and_overview_is_downsampled():
    class Array:
        shape = (2, 8000, 10000)

        def __getitem__(self, selection):
            _, y, x = selection
            assert isinstance(y, slice) and isinstance(x, slice)
            assert y.step > 1 or (y.stop - y.start <= 512 and x.stop - x.start <= 512)
            return np.zeros(
                (len(range(y.start, y.stop, y.step)), len(range(x.start, x.stop, x.step))),
                dtype="uint8",
            )

    raster = report.Raster.__new__(report.Raster)
    raster.arrays, raster.axes = [Array()], "CYX"
    raster.height, raster.width, raster.channels = 8000, 10000, 2
    assert raster.window((100, 100, 612, 612)).shape == (2, 512, 512)
    assert max(raster.overview([0, 1]).shape) <= 1536


def test_transcript_reservoir_is_bounded_and_deterministic(tmp_path):
    path = tmp_path / "tx.csv"
    pd.DataFrame({"x": np.arange(1000), "y": np.arange(1000), "cell_id": 1}).to_csv(
        path, index=False
    )
    first, count = report.transcript_sample(str(path), limit=50)
    second, _ = report.transcript_sample(str(path), limit=50)
    assert count == 1000 and len(first) == 50
    assert first.equals(second) and first.x.max() > 900


def transfer_fixture(root):
    raw, extra = root / "raw/sample", root / "extra/sample"
    (raw / "Morphology2D").mkdir(parents=True)
    extra.mkdir(parents=True)
    tifffile.imwrite(raw / "Morphology2D/sample_F001.TIF", np.ones((8, 8), dtype="uint8"))
    (raw / "Morphology_ChannelID_Dictionary.txt").write_text("ID Channel\n1 DNA\n")
    pd.DataFrame({"FOV": [1]}).to_csv(raw / "sample_fov_positions_file.csv.gz", index=False)
    pd.DataFrame({"x": [1]}).to_csv(raw / "sample_tx_file.csv.gz", index=False)
    pd.DataFrame({"x": [1]}).to_csv(raw / "sample-polygons.csv.gz", index=False)
    tifffile.imwrite(extra / "CellLabels_F001.tif", np.ones((8, 8), dtype="uint8"))
    paths = [p for p in root.rglob("*") if p.is_file()]
    manifest = [
        {"name": str(p.relative_to(root)), "size": p.stat().st_size, "crc32c": "fixture"}
        for p in paths
    ]
    body = {
        "version": 1,
        "sample": "sample",
        "provenance": "transfer",
        "run_id": "test",
        "finished": "2026-11-05T00:00:00Z",
        "manifest": manifest,
        "n_files": len(paths),
        "total_bytes": sum(p.stat().st_size for p in paths),
    }
    (root / "record.json").write_text(json.dumps(body))
    for scope, folder in [("raw", raw), ("additional_files", extra)]:
        selected = [p for p in paths if p.is_relative_to(folder)]
        fields = {
            "sample": "sample",
            "scope": scope,
            "status": "done",
            "verified": "crc32c",
            "scope_files": len(selected),
            "scope_bytes": sum(p.stat().st_size for p in selected),
            "sample_files": len(paths),
            "sample_bytes": body["total_bytes"],
            "run_id": "test",
            "finished": body["finished"],
            "record": "record.json",
        }
        (folder / "done.txt").write_text(
            "# AtoMx transfer marker v1\n"
            + "\n".join(f"{k}: {v}" for k, v in fields.items())
            + "\n\nFixture.\n"
        )
    return {
        "sample": "sample",
        "bucket_root": str(root),
        "raw": "raw/sample",
        "additional": "extra/sample",
    }


@pytest.mark.parametrize(
    "damage", ["none", "missing_tif", "row_mismatch", "backfill", "missing_marker", "bad_header"]
)
def test_completeness_fixtures(tmp_path, monkeypatch, damage):
    request = transfer_fixture(tmp_path)
    if damage == "missing_tif":
        (tmp_path / "raw/sample/Morphology2D/sample_F001.TIF").unlink()
    elif damage == "row_mismatch":
        pd.DataFrame({"FOV": [1, 2]}).to_csv(
            tmp_path / "raw/sample/sample_fov_positions_file.csv.gz", index=False
        )
    elif damage == "backfill":
        path = tmp_path / "raw/sample/done.txt"
        path.write_text(
            path.read_text().replace("verified: crc32c", "verified: listing-only (backfilled)")
        )
    elif damage == "missing_marker":
        (tmp_path / "extra/sample/done.txt").unlink()
    elif damage == "bad_header":
        (tmp_path / "raw/sample/Morphology2D/sample_F001.TIF").write_bytes(b"broken")
    monkeypatch.setattr(
        tifffile.TiffPage, "asarray", lambda *a, **kw: pytest.fail("decoded pixels")
    )
    result = check.check(request)
    assert result["complete"] is (damage == "none")
    if damage == "none":
        assert result["tiff_headers_checked"] == 2
    else:
        assert result["errors"]
