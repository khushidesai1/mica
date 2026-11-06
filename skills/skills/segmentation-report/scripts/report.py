"""Windowed segmentation QC. Coordinates are global image pixels, x right and y down."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import shapely
import tifffile
import zarr
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.collections import PatchCollection
from matplotlib.patches import Rectangle

DEFAULTS = {
    "cells_per_prior": 0.9,
    "assigned_pct": 90,
    "fov_assigned_pct": 80,
    "small_area_um2": 10,
    "small_cell_fraction": 0.1,
    "sparse_cells": 2,
}


class Raster:
    """A TIFF or NGFF image with basic slice reads, including compressed TIFFs."""

    def __init__(self, spec, labels=False):
        spec = {"path": spec} if isinstance(spec, str) else spec
        self.spec, self.resources = spec, []
        path = Path(spec["path"])
        axes = spec.get("axes")
        if path.suffix.lower() in {".tif", ".tiff"}:
            tif = tifffile.TiffFile(path)
            self.resources.append(tif)
            series = tif.series[0]
            self.arrays = []
            for level in series.levels:
                store = level.aszarr()
                self.resources.append(store)
                self.arrays.append(zarr.open(store, mode="r"))
            axes = axes or series.axes
        else:
            node = zarr.open(str(path), mode="r")
            if hasattr(node, "shape"):
                self.arrays = [node]
            else:
                meta = dict(node.attrs)
                meta = meta.get("ome", meta)
                multi = meta["multiscales"][0]
                axes = axes or "".join(
                    a if isinstance(a, str) else a["name"] for a in multi["axes"]
                )
                self.arrays = [node[d["path"]] for d in multi["datasets"]]
            if not axes:
                axes = "YX" if labels else None
        self.axes = (axes or "").upper().replace("S", "C")
        if (
            len(self.axes) != len(self.arrays[0].shape)
            or "Y" not in self.axes
            or "X" not in self.axes
            or len(set(self.axes)) != len(self.axes)
        ):
            raise ValueError("Provide unambiguous image axes (e.g. CYX or YX)")
        for i, axis in enumerate(self.axes):
            if axis not in "CYX" and self.arrays[0].shape[i] != 1:
                raise ValueError("Select a single 2D plane before reporting")
        self.height = self.arrays[0].shape[self.axes.index("Y")]
        self.width = self.arrays[0].shape[self.axes.index("X")]
        self.channels = self.arrays[0].shape[self.axes.index("C")] if "C" in self.axes else 1
        if labels and self.channels != 1:
            raise ValueError("Label inputs must be a single 2D label plane")

    def window(self, box, level=0, channels=None, step=1):
        x0, y0, x1, y1 = box
        array = self.arrays[level]
        sy = self.height / array.shape[self.axes.index("Y")]
        sx = self.width / array.shape[self.axes.index("X")]
        layers = []
        for channel in channels if channels is not None else range(self.channels):
            selection = []
            for axis in self.axes:
                selection.append(
                    slice(
                        max(0, int(y0 / sy)),
                        min(math.ceil(y1 / sy), math.ceil(self.height / sy)),
                        step,
                    )
                    if axis == "Y"
                    else slice(
                        max(0, int(x0 / sx)),
                        min(math.ceil(x1 / sx), math.ceil(self.width / sx)),
                        step,
                    )
                    if axis == "X"
                    else channel
                    if axis == "C"
                    else 0
                )
            plane = np.asarray(array[tuple(selection)])
            if self.axes.index("X") < self.axes.index("Y"):
                plane = plane.T
            layers.append(plane)
        return np.stack(layers)

    def overview(self, channels):
        level = len(self.arrays) - 1
        size = max(self.arrays[level].shape[self.axes.index(a)] for a in "YX")
        step = max(1, math.ceil(size / 1536))
        return self.window((0, 0, self.width, self.height), level, channels, step)

    def close(self):
        for resource in reversed(self.resources):
            resource.close()


def polygon_data(spec, scale):
    spec = {"path": spec, "units": "pixel"} if isinstance(spec, str) else spec
    if spec.get("units") not in {"pixel", "um"}:
        raise ValueError("Polygon units must be explicitly pixel or um")
    parquet = pq.ParquetFile(spec["path"])
    column = spec.get("geometry", "geometry")
    geoms = shapely.from_wkb(parquet.read(columns=[column])[column].to_numpy())
    if not len(geoms) or np.any(shapely.is_empty(geoms)) or not np.all(shapely.is_valid(geoms)):
        raise ValueError("Cells must contain nonempty valid polygons")
    if not np.all(np.isin(shapely.get_type_id(geoms), [3, 6])):
        raise ValueError("Cell geometries must be polygons or multipolygons")
    if spec["units"] == "um":
        geoms = shapely.transform(geoms, lambda xy: xy / scale)
    return geoms


class Cells:
    def __init__(self, spec, scale):
        spec = {"path": spec} if isinstance(spec, str) else spec
        self.labels = self.geoms = self.tree = None
        if Path(spec["path"]).suffix.lower() in {".parquet", ".geoparquet"}:
            self.geoms = polygon_data(spec, scale)
            self.tree = shapely.STRtree(self.geoms)
            centers = shapely.centroid(self.geoms)
            self.xy = np.column_stack([shapely.get_x(centers), shapely.get_y(centers)])
            self.areas = shapely.area(self.geoms) * scale**2
        else:
            self.labels = Raster(spec, labels=True)
            totals = {}
            for y in range(0, self.labels.height, 512):
                for x in range(0, self.labels.width, 512):
                    tile = self.labels.window((x, y, x + 512, y + 512))[0]
                    if not np.issubdtype(tile.dtype, np.integer) or np.any(tile < 0):
                        raise ValueError("Labels must be nonnegative integers; zero is background")
                    yy, xx = np.nonzero(tile)
                    ids, inverse, counts = np.unique(
                        tile[yy, xx], return_inverse=True, return_counts=True
                    )
                    xs, ys = (
                        np.bincount(inverse, weights=xx + x),
                        np.bincount(inverse, weights=yy + y),
                    )
                    for i, label in enumerate(ids):
                        row = totals.setdefault(int(label), np.zeros(3))
                        row += [counts[i], xs[i], ys[i]]
            values = np.array(list(totals.values()))
            if not len(values):
                raise ValueError("No segmented cells in label input")
            self.areas = values[:, 0] * scale**2
            self.xy = values[:, 1:] / values[:, :1]

    def outlines(self, ax, box):
        if self.labels:
            tile = self.labels.window(box)[0]
            boundary = np.zeros(tile.shape, dtype=bool)
            boundary[1:] |= tile[1:] != tile[:-1]
            boundary[:, 1:] |= tile[:, 1:] != tile[:, :-1]
            boundary &= tile != 0
            ax.contour(
                boundary,
                levels=[0.5],
                colors=["#ffca63"],
                linewidths=0.45,
                extent=(box[0], box[2], box[3], box[1]),
            )
        else:
            region = shapely.box(*box)
            selected = self.geoms[self.tree.query(region, predicate="intersects")]
            for geometry in shapely.intersection(selected, region):
                parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
                for polygon in parts:
                    if hasattr(polygon, "exterior"):
                        x, y = polygon.exterior.xy
                        ax.plot(x, y, color="#ffca63", linewidth=0.45)


def transcript_sample(spec, limit=100_000, seed=17):
    if not spec:
        return None, 0
    spec = {"path": spec} if isinstance(spec, str) else spec
    columns = [spec.get(k, k) for k in ["x", "y", "cell_id"]]
    path = Path(spec["path"])
    if path.suffix == ".parquet":
        chunks = (
            batch.to_pandas()
            for batch in pq.ParquetFile(path).iter_batches(batch_size=100_000, columns=columns)
        )
    else:
        chunks = pd.read_csv(path, usecols=columns, chunksize=100_000)
    rng, selected, ranks, total = np.random.default_rng(seed), None, np.empty(0), 0
    for chunk in chunks:
        total += len(chunk)
        chunk.columns = [
            dict(zip(columns, ["x", "y", "cell_id"], strict=True))[c] for c in chunk.columns
        ]
        keys = rng.random(len(chunk))
        selected = chunk if selected is None else pd.concat([selected, chunk], ignore_index=True)
        ranks = np.concatenate([ranks, keys])
        if len(ranks) > limit:
            take = np.argpartition(ranks, limit - 1)[:limit]
            selected, ranks = selected.iloc[take].reset_index(drop=True), ranks[take]
    if selected is not None:
        selected["x"] = pd.to_numeric(selected["x"], errors="raise")
        selected["y"] = pd.to_numeric(selected["y"], errors="raise")
        if not np.isfinite(selected[["x", "y"]].to_numpy()).all():
            raise ValueError("Transcript coordinates must be finite")
        unassigned = {
            str(v)
            for v in spec.get("unassigned", [0, -1, "", "nan", "None", "UNASSIGNED", "unassigned"])
        }
        selected["assigned"] = ~selected.cell_id.isna() & ~selected.cell_id.astype(str).isin(
            unassigned
        )
    return selected, total


def fov_boxes(spec, width, height):
    if not spec:
        return [
            {"id": f"tile-{x}-{y}", "box": [x, y, min(x + 1024, width), min(y + 1024, height)]}
            for y in range(0, height, 1024)
            for x in range(0, width, 1024)
        ]
    spec = {"path": spec} if isinstance(spec, str) else spec
    frame = pd.read_csv(spec["path"])
    boxes = []
    for i, row in frame.iterrows():
        x, y = (
            float(row.get("x0", row.get("x_global_px", np.nan))),
            float(row.get("y0", row.get("y_global_px", np.nan))),
        )
        w, h = (
            float(row.get("width", spec.get("width", np.nan))),
            float(row.get("height", spec.get("height", np.nan))),
        )
        if not np.isfinite([x, y, w, h]).all() or min(w, h) <= 0:
            raise ValueError("FOVs need pixel x0/y0 and width/height in the image frame")
        box = [max(0, x), max(0, y), min(width, x + w), min(height, y + h)]
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("FOV bounds do not overlap the image")
        boxes.append({"id": str(row.get("FOV", row.get("id", i))), "box": box})
    if not boxes or len({b["id"] for b in boxes}) != len(boxes):
        raise ValueError("FOV identifiers must be nonempty and unique")
    return boxes


def inside(xy, box):
    return (xy[:, 0] >= box[0]) & (xy[:, 0] < box[2]) & (xy[:, 1] >= box[1]) & (xy[:, 1] < box[3])


def composite(planes):
    colors = np.array([[0.55, 0.45, 1.0], [0.2, 0.95, 0.7]])
    rgb = np.zeros((*planes.shape[1:], 3), dtype=np.float32)
    for i, plane in enumerate(planes[:2]):
        lo, hi = np.percentile(plane, [1, 99.5])
        norm = np.clip((plane.astype(np.float32) - lo) / max(float(hi - lo), 1e-9), 0, 1)
        rgb += norm[..., None] * colors[i]
    return np.clip(rgb, 0, 1)


def select_crops(rows, seed=17):
    tissue = [r for r in rows if not r["zero_image"]]
    if not tissue:
        tissue = rows
    ordered = sorted(
        [r for r in tissue if r["score"] is not None], key=lambda r: (r["score"], r["id"])
    )
    picks, used = [], set()
    for reason, choices in [("Best QC", ordered[::-1]), ("Worst QC", ordered)]:
        for row in [r for r in choices if r["id"] not in used][:2]:
            picks.append((row, reason))
            used.add(row["id"])
    remaining = [r for r in tissue if r["id"] not in used]
    rng = np.random.default_rng(seed)
    for i in rng.permutation(len(remaining))[:2]:
        row = remaining[int(i)]
        picks.append((row, "Seeded random tissue"))
        used.add(row["id"])
    for row in tissue:
        if len(picks) < 6 and row["id"] not in used:
            picks.append((row, "Additional tissue: insufficient ranked FOVs"))
            used.add(row["id"])
    # Tiny fixtures may have fewer than six distinct FOVs; label repeats honestly.
    while len(picks) < 6:
        picks.append((tissue[len(picks) % len(tissue)], "Repeat: fewer than six tissue FOVs"))
    return picks


def area_summary(areas, thresholds):
    return {
        "median": float(np.median(areas)),
        "p05": float(np.percentile(areas, 5)),
        "p95": float(np.percentile(areas, 95)),
        "small_fraction": float(np.mean(areas < thresholds["small_area_um2"])),
    }


def make_report(request):
    scale = float(request["um_per_px"])
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("um_per_px must be a positive finite value from metadata")
    if len(request.get("cohort", [])) > 100:
        raise ValueError("At most 100 cohort inputs")
    output = Path(request["output_dir"]).resolve()
    if not output.is_relative_to(Path("outputs").resolve()) or output == Path("outputs").resolve():
        raise ValueError("Use a new folder under outputs/")
    output.mkdir(parents=True, exist_ok=False)
    figures = output / "figures"
    figures.mkdir()
    thresholds = {**DEFAULTS, **request.get("thresholds", {})}
    if set(thresholds) != set(DEFAULTS) or not all(
        math.isfinite(float(v)) and v >= 0 for v in thresholds.values()
    ):
        raise ValueError("Unknown or invalid QC thresholds")
    image_spec = request["image"]
    if isinstance(image_spec, str):
        raise ValueError("image must include path, channel names and nuclear_channel")
    image = Raster(image_spec)
    cells = None
    try:
        channels = image_spec["channels"]
        if len(channels) != image.channels:
            raise ValueError("Channel names do not match the image")
        chosen = [channels.index(image_spec["nuclear_channel"])]
        if image_spec.get("membrane_channel"):
            chosen.append(channels.index(image_spec["membrane_channel"]))
        cells = Cells(request["cells"], scale)
        if (
            np.any(cells.xy < 0)
            or np.any(cells.xy[:, 0] >= image.width)
            or np.any(cells.xy[:, 1] >= image.height)
        ):
            raise ValueError("Cell coordinates lie outside image; verify scale, origin and y frame")
        if cells.labels and (cells.labels.width, cells.labels.height) != (
            image.width,
            image.height,
        ):
            raise ValueError("Label and image shapes must match")
        overview = image.overview(chosen)
        boxes = fov_boxes(request.get("fovs"), image.width, image.height)
        tx, total_tx = transcript_sample(request.get("transcripts"))
        rows = []
        for item in boxes:
            box = item["box"]
            count = int(inside(cells.xy, box).sum())
            ox, oy = overview.shape[2] / image.width, overview.shape[1] / image.height
            view = overview[
                :,
                int(box[1] * oy) : max(int(box[1] * oy) + 1, math.ceil(box[3] * oy)),
                int(box[0] * ox) : max(int(box[0] * ox) + 1, math.ceil(box[2] * ox)),
            ]
            zero = not bool(np.any(view))
            assigned = None
            if tx is not None and len(tx):
                local = tx[inside(tx[["x", "y"]].to_numpy(), box)]
                if len(local):
                    assigned = 100 * float(local.assigned.mean())
            density = count / max(1, (box[2] - box[0]) * (box[3] - box[1]))
            rows.append(
                {
                    **item,
                    "cells": count,
                    "assigned_pct": assigned,
                    "zero_image": zero,
                    "score": (assigned / 100 if assigned is not None else None)
                    if tx is not None
                    else density,
                }
            )
        picks = select_crops(rows)
        assigned = 100 * float(tx.assigned.mean()) if tx is not None and len(tx) else None
        summary = {
            "schema_version": 1,
            "sample": request["sample"],
            "method": request["method"],
            "um_per_px": scale,
            "cell_count": len(cells.xy),
            "area_um2": area_summary(cells.areas, thresholds),
            "assigned_pct": assigned,
            "transcripts_per_cell": total_tx * assigned / 100 / len(cells.xy)
            if assigned is not None
            else None,
            "transcripts_observed": total_tx,
            "transcripts_sampled": 0 if tx is None else len(tx),
            "fovs": rows,
            "fov_score_metric": "assigned_fraction" if tx is not None else "cells_per_pixel2",
            "crops": [{"fov": r["id"], "reason": why} for r, why in picks],
        }
        cohort = []
        for spec in request.get("cohort", []):
            path = Path(spec if isinstance(spec, str) else spec["path"])
            if path.suffix == ".json":
                entry = json.loads(path.read_text())
                if entry.get("schema_version") != 1:
                    raise ValueError("Unsupported cohort summary schema")
            else:
                polygons = polygon_data(spec, scale)
                entry = {
                    "sample": path.stem,
                    "cell_count": len(polygons),
                    "area_um2": area_summary(shapely.area(polygons) * scale**2, thresholds),
                }
            if entry["sample"] != summary["sample"]:
                cohort.append(entry)
        flags = []

        def flag(code, text, fov=None):
            flags.append({"code": code, "text": text, **({"fov": fov} if fov else {})})

        if request.get("prior"):
            prior = polygon_data(request["prior"], scale)
            summary["cells_per_prior"] = len(cells.xy) / len(prior)
            if summary["cells_per_prior"] < thresholds["cells_per_prior"]:
                flag("low_cells_per_prior", f"Cells/prior cells: {summary['cells_per_prior']:.3f}")
        if assigned is not None and assigned < thresholds["assigned_pct"]:
            flag("low_assignment", f"Estimated transcripts assigned: {assigned:.1f}%")
        if summary["area_um2"]["small_fraction"] > thresholds["small_cell_fraction"]:
            flag(
                "small_cells",
                f"{summary['area_um2']['small_fraction']:.1%} of cells below "
                f"{thresholds['small_area_um2']} µm²",
            )
        for row in rows:
            if (
                row["assigned_pct"] is not None
                and row["assigned_pct"] < thresholds["fov_assigned_pct"]
            ):
                flag(
                    "low_fov_assignment",
                    f"Estimated assigned: {row['assigned_pct']:.1f}%",
                    row["id"],
                )
            if row["zero_image"]:
                flag("zero_image", "All-zero image tile in the overview", row["id"])
            elif row["cells"] <= thresholds["sparse_cells"]:
                flag("stain_without_cells", f"Stain present, only {row['cells']} cells", row["id"])
        for name, current, values in [
            ("cell_count", summary["cell_count"], [e["cell_count"] for e in cohort]),
            (
                "median_area",
                summary["area_um2"]["median"],
                [e["area_um2"]["median"] for e in cohort],
            ),
        ]:
            if len(values) >= 2:
                low, high = np.percentile(values, [5, 95])
                if not low <= current <= high:
                    flag(
                        "cohort_" + name,
                        f"{name}: {current:.2f}, outside cohort 5–95% ({low:.2f}–{high:.2f})",
                    )
        summary["flag_count"] = len(flags)
        provenance = {
            "schema_version": 1,
            "request": request,
            "thresholds": thresholds,
            "seed": 17,
            "transcript_sampling": (
                "Uniform reservoir, at most 100000 rows; "
                "assignment and per-cell counts are estimates"
            ),
            "coordinates": (
                "Image pixels; x right, y down; polygon µm coordinates divided by um_per_px"
            ),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "versions": {
                "numpy": np.__version__,
                "shapely": shapely.__version__,
                "tifffile": tifffile.__version__,
                "zarr": zarr.__version__,
            },
        }
        render(
            output,
            figures,
            image,
            chosen,
            cells,
            overview,
            summary,
            cohort,
            picks,
            flags,
            str(request.get("review", "Not yet visually reviewed.")),
            provenance,
        )
        for name, value in [
            ("segmentation_summary.json", summary),
            ("flags.json", flags),
            ("provenance.json", provenance),
        ]:
            (output / name).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
        return summary
    finally:
        image.close()
        if cells and cells.labels:
            cells.labels.close()


def render(
    output,
    figures,
    image,
    channels,
    cells,
    overview,
    summary,
    cohort,
    picks,
    flags,
    review,
    provenance,
):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "#fbfbf8",
        }
    )
    with PdfPages(output / "report.pdf") as pdf:

        def page(title, number):
            fig = plt.figure(figsize=(11.7, 8.3))
            fig.suptitle(
                title, x=0.06, y=0.96, ha="left", fontsize=19, fontweight="bold", color="#193b39"
            )
            fig.text(0.06, 0.025, f"Mica · {summary['sample']} · Segmentation QC", color="#526360")
            fig.text(0.94, 0.025, f"{number} / 6", ha="right", color="#526360")
            return fig

        def save(fig, name):
            fig.savefig(figures / (name + ".png"), dpi=120)
            pdf.savefig(fig)
            plt.close(fig)

        fig = page("Segmentation report", 1)
        method = summary["method"]
        rows = [
            ("Sample", summary["sample"]),
            ("Method", method["name"]),
            ("Version", method.get("version", "Unspecified")),
            ("Scale", f"{summary['um_per_px']} µm/pixel"),
            ("Cells", f"{summary['cell_count']:,}"),
            ("Median area", f"{summary['area_um2']['median']:.2f} µm²"),
            (
                "Transcripts assigned (estimate)",
                "Unavailable"
                if summary["assigned_pct"] is None
                else f"{summary['assigned_pct']:.1f}%",
            ),
            ("Automatic flags", str(len(flags))),
        ]
        ax = fig.add_axes([0.06, 0.39, 0.88, 0.45])
        ax.axis("off")
        table = ax.table(cellText=rows, colWidths=[0.4, 0.6], loc="center", cellLoc="left")
        table.auto_set_font_size(False)
        table.set_fontsize(11)
        table.scale(1, 1.9)
        for (_r, c), cell in table.get_celld().items():
            cell.set_edgecolor("white")
            cell.set_facecolor("#e5eeea" if c == 0 else "#f1f4ef")
        details = "Parameters: " + json.dumps(method.get("parameters", {}), sort_keys=True)
        details += "\nReport code: " + provenance["script_sha256"][:16]
        details += "\n" + provenance["transcript_sampling"]
        fig.text(
            0.06,
            0.31,
            "\n".join(textwrap.fill(line, 120) for line in details.splitlines())[:1800],
            va="top",
            fontsize=9,
        )
        save(fig, "01-summary")
        fig = page("Whole-sample overview", 2)
        axes = fig.subplots(1, 2)
        fig.subplots_adjust(top=0.85, bottom=0.12, wspace=0.25)
        axes[0].imshow(composite(overview), extent=(0, image.width, image.height, 0))
        axes[0].set_title("Nuclear / membrane stains · low resolution")
        density, _, _ = np.histogram2d(
            cells.xy[:, 1], cells.xy[:, 0], bins=128, range=[[0, image.height], [0, image.width]]
        )
        im = axes[1].imshow(density, extent=(0, image.width, image.height, 0), cmap="viridis")
        axes[1].set_title("Cell density · cells per grid bin")
        fig.colorbar(im, ax=axes[1], shrink=0.65)
        for ax in axes:
            ax.set_xlabel("x (pixels)")
            ax.set_ylabel("y (pixels)")
        save(fig, "02-overview")
        fig = page("Full-resolution crop review", 3)
        axes = fig.subplots(2, 3)
        fig.subplots_adjust(top=0.87, bottom=0.1, hspace=0.35, wspace=0.2)
        for index, (ax, (row, why)) in enumerate(zip(axes.flat, picks, strict=True)):
            x0, y0, x1, y1 = row["box"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            box = [
                max(int(x0), int(cx - 256)),
                max(int(y0), int(cy - 256)),
                min(int(x1), int(cx + 256)),
                min(int(y1), int(cy + 256)),
            ]
            rgb = composite(image.window(box, channels=channels))
            ax.imshow(rgb, extent=(box[0], box[2], box[3], box[1]))
            cells.outlines(ax, box)
            ax.set_title(f"{row['id']} · {why}", fontsize=8)
            ax.set_xlim(box[0], box[2])
            ax.set_ylim(box[3], box[1])
            ax.set_xticks([])
            ax.set_yticks([])
            crop = plt.figure(figsize=(6, 6))
            crop_ax = crop.add_subplot()
            crop_ax.imshow(rgb, extent=(box[0], box[2], box[3], box[1]))
            cells.outlines(crop_ax, box)
            crop_ax.set_title(f"{row['id']} · {why}")
            crop_ax.set_xlim(box[0], box[2])
            crop_ax.set_ylim(box[3], box[1])
            crop.savefig(figures / f"crop-{index + 1}.png", dpi=170)
            plt.close(crop)
        save(fig, "03-crops")
        fig = page("Cohort comparison", 4)
        axes = fig.subplots(2, 2)
        fig.subplots_adjust(top=0.86, bottom=0.13, hspace=0.55, wspace=0.25)
        entries = cohort + [summary]
        metrics = [
            ("Cell count", "cell_count"),
            ("Area µm² · median and 5–95%", "area_um2"),
            ("Estimated transcripts per cell", "transcripts_per_cell"),
            ("Estimated assigned (%)", "assigned_pct"),
        ]
        for ax, (label, key) in zip(axes.flat, metrics, strict=True):
            for i, entry in enumerate(entries):
                value = entry.get(key)
                if value is None:
                    continue
                color = "#c45c32" if i == len(entries) - 1 else "#91a9a3"
                if key == "area_um2":
                    ax.plot([i, i], [value["p05"], value["p95"]], color=color, linewidth=2)
                    value = value["median"]
                ax.scatter([i], [value], color=color, s=36, zorder=3)
            ax.set_title(label)
            ax.set_xticks(range(len(entries)))
            ax.set_xticklabels(
                [e["sample"] for e in entries], rotation=70, fontsize=max(4, 8 - len(entries) // 20)
            )
        save(fig, "04-cohort")
        fig = page("Per-FOV QC map", 5)
        ax = fig.add_axes([0.1, 0.13, 0.73, 0.72])
        patches = [
            Rectangle(
                (r["box"][0], r["box"][1]), r["box"][2] - r["box"][0], r["box"][3] - r["box"][1]
            )
            for r in summary["fovs"]
        ]
        collection = PatchCollection(patches, cmap="viridis", edgecolor="white", linewidth=0.2)
        collection.set_array(
            np.ma.masked_invalid(
                np.array([r["score"] for r in summary["fovs"]], dtype=float)
            )
        )
        collection.cmap.set_bad("#d5d5d5")
        ax.add_collection(collection)
        ax.set_xlim(0, image.width)
        ax.set_ylim(image.height, 0)
        ax.set_aspect("equal")
        ax.set_xlabel("x (pixels)")
        ax.set_ylabel("y (pixels)")
        fig.colorbar(collection, ax=ax, label=summary["fov_score_metric"].replace("_", " "))
        if summary["fov_score_metric"] == "assigned_fraction":
            fig.text(
                0.1, 0.07, "Gray FOVs have no sampled transcripts; they are not QC-ranked.",
                fontsize=9,
            )
        save(fig, "05-fov-qc")
        fig = page("Flags and visual review", 6)
        lines = [f"{len(flags)} automatic flags. Thresholds and all details are in flags.json.", ""]
        for flag in flags[:18]:
            lines.extend(textwrap.wrap(f"• {flag.get('fov', 'Sample')}: {flag['text']}", 110))
        if len(flags) > 18:
            lines.append(f"… {len(flags) - 18} additional flags in flags.json.")
        lines += ["", "Visual review"] + textwrap.wrap(review, 110)[:12]
        fig.text(0.06, 0.86, "\n".join(lines), va="top", fontsize=10, linespacing=1.5)
        save(fig, "06-flags")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    request = json.loads(parser.parse_args().request)
    summary = make_report(request)
    print(
        json.dumps(
            {
                "sample": summary["sample"],
                "cell_count": summary["cell_count"],
                "flag_count": summary["flag_count"],
                "report": request["output_dir"] + "/report.pdf",
            }
        )
    )


if __name__ == "__main__":
    main()
