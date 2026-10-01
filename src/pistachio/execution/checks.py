"""Trusted, sandbox-invoked probes and artifact validators (no model code imports)."""

from __future__ import annotations

import argparse
import csv
import importlib.metadata
import json
from pathlib import Path
from typing import Any


def hdf_structure(node: Any) -> dict[str, Any]:
    """Read structural metadata only; never materialize a coordinate/expression matrix."""
    encoding = str(node.attrs.get("encoding-type", "array" if hasattr(node, "shape") else "group"))
    result: dict[str, Any] = {"encoding": encoding}
    if hasattr(node, "shape"):
        result.update(shape=list(node.shape), dtype=str(node.dtype), representation="ndarray")
    elif encoding == "dataframe":
        index = node.attrs.get("_index", "_index")
        columns = node.attrs.get("column-order", [])
        result.update(
            representation="pandas.DataFrame", row_selection="iloc (positional rows)",
            shape=[len(node[index]), len(columns)], columns=[str(x) for x in columns[:100]],
        )
    elif encoding == "categorical":
        result.update(shape=list(node["codes"].shape),
                      category_count=len(node["categories"]), representation="categorical")
    else:
        result["representation"] = encoding
    return result


def api_capabilities(entrypoints: list[str]) -> list[dict[str, Any]]:
    import importlib
    import inspect

    records = []
    for name in sorted(set(entrypoints))[:20]:
        parts = name.split(".")
        if (len(parts) < 2 or not all(p.isidentifier() and not p.startswith("_") for p in parts)
            or parts[0] not in {"numpy", "pandas", "scipy", "sklearn", "scanpy", "anndata",
                                "igraph", "louvain", "matplotlib"}):
            continue
        record: dict[str, Any] = {"entrypoint": name}
        try:
            # Import the containing module where possible; lazy top-level exports vary.
            try:
                module = importlib.import_module(".".join(parts[:-1]))
                target = getattr(module, parts[-1])
            except (ImportError, AttributeError):
                target = importlib.import_module(parts[0])
                for part in parts[1:]:
                    target = getattr(target, part)
            record["available"] = True
            try:
                record["signature"] = str(inspect.signature(target))[:3000]
            except (TypeError, ValueError):
                record["signature"] = "unavailable; consult installed documentation"
            record["documentation"] = (inspect.getdoc(target) or "")[:1500]
            if name == "scanpy.tl.louvain":
                importlib.import_module("louvain")
                record["optional_backend"] = "louvain import passed"
        except Exception as exc:
            record.update(available=False, error=f"{type(exc).__name__}: {exc}")
        records.append(record)
    return records


def runtime_probe(input_dir: Path, entrypoints: list[str] | None = None) -> dict[str, Any]:
    import platform

    inputs: list[dict[str, Any]] = []
    for path in sorted(input_dir.iterdir()):
        record: dict[str, Any] = {"name": path.name, "bytes": path.stat().st_size}
        if path.suffix.lower() == ".h5ad":
            import h5py

            with h5py.File(path, "r") as data:
                record["keys"] = list(data.keys())
                for group in ("obs", "var", "obsm", "layers"):
                    if group in data:
                        record[group] = list(data[group].keys())[:100]
                if "obsm" in data:
                    record["obsm_structure"] = {
                        name: hdf_structure(data["obsm"][name])
                        for name in list(data["obsm"].keys())[:20]
                    }
                if "obs" in data:
                    record["obs_structure"] = hdf_structure(data["obs"])
                if "X" in data:
                    matrix = data["X"]
                    record["X_shape"] = (
                        list(matrix.shape)
                        if hasattr(matrix, "shape")
                        else (
                            matrix.attrs.get("shape", []).tolist()
                            if hasattr(matrix.attrs.get("shape", []), "tolist")
                            else []
                        )
                    )
                    record["X_encoding"] = str(matrix.attrs.get("encoding-type", "dense"))
        inputs.append(record)
    return {
        "python": platform.python_version(),
        "architecture": platform.machine(),
        "packages": sorted(
            f"{d.metadata['Name']}=={d.version}"
            for d in importlib.metadata.distributions()
            if d.metadata.get("Name")
        ),
        "sandbox": {"proc_meminfo_available": Path("/proc/meminfo").is_file(),
                    "host_memory_telemetry_required": False},
        "inputs": inputs,
        "api_capabilities": api_capabilities(entrypoints or []),
    }


def verify_outputs(root: Path, specs: list[dict[str, Any]]) -> list[str]:
    actual = set()
    for item in root.rglob("*"):
        if item.is_symlink():
            raise ValueError("output symlinks are forbidden")
        if item.is_file():
            actual.add(item.relative_to(root).as_posix())
        elif not item.is_dir():
            raise ValueError("output must contain only regular files")
    expected = {item["path"] for item in specs}
    if actual != expected:
        raise ValueError(
            f"output mismatch: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    checks = []
    for spec in specs:
        path = root / spec["path"]
        if not path.stat().st_size:
            raise ValueError(f"empty artifact: {spec['path']}")
        kind = spec["format"]
        fields = set(spec["required_fields"])
        if kind == "json":

            def invalid_constant(value: str) -> None:
                raise ValueError(f"nonfinite JSON constant: {value}")

            value = json.loads(path.read_text(), parse_constant=invalid_constant)
            if not value and not spec["allow_empty"]:
                raise ValueError(f"empty JSON result: {path.name}")
            if fields and (not isinstance(value, dict) or not fields <= value.keys()):
                raise ValueError(f"missing JSON fields: {path.name}")
        elif kind in {"csv", "tsv"}:
            with path.open(newline="") as stream:
                reader = csv.DictReader(stream, delimiter="\t" if kind == "tsv" else ",")
                if not fields <= set(reader.fieldnames or []):
                    raise ValueError(f"missing table fields: {path.name}")
                count = 0
                for row in reader:
                    if None in row or any(x is None for x in row.values()):
                        raise ValueError(f"malformed table row: {path.name}")
                    count += 1
                if count == 0 and not spec["allow_empty"]:
                    raise ValueError(f"empty table: {path.name}")
        elif kind in {"npy", "npz"}:
            import numpy as np

            value = np.load(path, allow_pickle=False)
            if kind == "npz":
                with value:
                    if not value.files and not spec["allow_empty"]:
                        raise ValueError("empty array archive")
                    if not fields <= set(value.files):
                        raise ValueError("missing array names")
                    for key in value.files:
                        array = value[key]
                        if not array.size and not spec["allow_empty"]:
                            raise ValueError("empty array")
            elif not value.size and not spec["allow_empty"]:
                raise ValueError("empty array")
        elif kind in {"h5ad", "hdf5"}:
            import h5py

            with h5py.File(path, "r") as data:
                if not fields <= set(data.keys()):
                    raise ValueError("missing HDF5 fields")
                if kind == "h5ad" and not {"obs", "var"} <= set(data.keys()):
                    raise ValueError("missing AnnData axes")
            if kind == "h5ad":
                import anndata

                data = anndata.read_h5ad(path, backed="r")
                try:
                    if not data.n_obs and not spec["allow_empty"]:
                        raise ValueError("empty AnnData observations")
                finally:
                    data.file.close()
        elif kind == "parquet":
            import pyarrow.parquet as parquet

            if not fields <= set(parquet.read_schema(path).names):
                raise ValueError("missing Parquet columns")
            if not parquet.read_metadata(path).num_rows and not spec["allow_empty"]:
                raise ValueError("empty Parquet table")
        elif kind == "png":
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                if min(image.size) < 8:
                    raise ValueError("figure is too small to inspect")
        elif kind == "pdf":
            # Opening every page is stronger than checking the PDF magic header.
            import pypdf

            document = pypdf.PdfReader(path)
            if not document.pages:
                raise ValueError("PDF has no pages")
            for page in document.pages:
                page.get_contents()
        elif kind == "svg":
            from defusedxml import ElementTree

            tree = ElementTree.parse(path)
            if not tree.getroot().tag.endswith("svg"):
                raise ValueError("invalid SVG root")
            for element in tree.iter():
                if element.tag.endswith("script") or any(
                    key.lower().startswith("on")
                    or (key.endswith("href") and not val.startswith("#"))
                    for key, val in element.attrib.items()
                ):
                    raise ValueError("active/external SVG content is forbidden")
        elif kind == "text":
            path.read_text(encoding="utf-8")
        checks.append(f"artifact:{spec['path']}")
    return checks


def run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("probe", "outputs"))
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if args.mode == "probe":
        config = json.loads(args.config.read_text())
        print(json.dumps(runtime_probe(args.input_dir, config.get("runtime_entrypoints", [])),
                         sort_keys=True))
    else:
        specs = json.loads(args.config.read_text())["output_specs"]
        print(json.dumps({"checks": verify_outputs(args.output_dir, specs)}))


if __name__ == "__main__":
    run()
