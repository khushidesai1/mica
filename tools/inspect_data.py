"""Inspect metadata and observation columns; never materialize assay matrices."""

import argparse
import json
import os
from itertools import islice
from pathlib import Path


def shape(node):
    return list(node.shape) if hasattr(node, "shape") else list(node.attrs.get("shape", []))


def inspect(path, limit=20):
    import h5py
    import pandas as pd

    path = Path(path)
    if path.is_dir():
        with os.scandir(path) as entries:
            return {
                "entries": [e.name for e in islice(entries, 100) if not e.name.startswith(".")],
                "coverage": "At most 100 immediate entries; not recursive or exhaustive",
            }
    result = {"path": str(path), "size_bytes": path.stat().st_size}
    if path.suffix.lower() == ".h5ad":
        from anndata.io import read_elem

        with h5py.File(path, "r") as handle:

            def frame_length(key):
                group = handle[key]
                index = group.attrs.get("_index", "_index")
                node = group[index]
                if isinstance(node, h5py.Group):
                    node = node["values"] if "values" in node else node["codes"]
                return node.shape[0]

            result.update(n_obs=frame_length("obs"), n_vars=frame_length("var"), obs={})
            for name in handle["obs"].attrs.get("column-order", []):
                column = pd.Series(read_elem(handle["obs"][name]))
                summary = {
                    "dtype": str(column.dtype),
                    "unique": int(column.nunique()),
                    "missing": int(column.isna().sum()),
                }
                if pd.api.types.is_numeric_dtype(column.dtype):
                    summary.update(
                        min=None if column.dropna().empty else float(column.min()),
                        max=None if column.dropna().empty else float(column.max()),
                    )
                else:
                    summary["top"] = {
                        str(k): int(v) for k, v in column.value_counts().head(limit).items()
                    }
                result["obs"][name] = summary
            for key in ("obsm", "layers", "varm"):
                result[key] = {
                    name: {
                        "shape": shape(node),
                        "dtype": str(getattr(node, "dtype", "sparse/group")),
                    }
                    for name, node in handle.get(key, {}).items()
                }
            result["uns"] = list(handle.get("uns", {}))
            result["guesses"] = {
                label: next((n for n in result[group] if any(t in n.lower() for t in tokens)), None)
                for label, group, tokens in [
                    ("sample", "obs", ("sample", "library", "slide")),
                    ("cell_type", "obs", ("cell_type", "celltype", "annotation")),
                    ("spatial", "obsm", ("spatial",)),
                ]
            }
    elif path.suffix.lower() in {".csv", ".tsv"}:
        data = pd.read_csv(path, sep="\t" if path.suffix.lower() == ".tsv" else ",", nrows=10_000)
        result.update(
            dtypes={k: str(v) for k, v in data.dtypes.items()},
            preview=json.loads(data.head().to_json(orient="records")),
            sampled_rows=len(data),
        )
    elif path.suffix.lower() == ".parquet":
        import pyarrow.parquet as pq

        file = pq.ParquetFile(path)
        batch = next(file.iter_batches(batch_size=5), None)
        result.update(
            schema=str(file.schema_arrow),
            rows=file.metadata.num_rows,
            preview=batch.to_pylist() if batch else [],
        )
    elif path.suffix.lower() == ".loom":
        with h5py.File(path, "r") as handle:
            result.update(
                shape=shape(handle["matrix"]),
                row_attrs=list(handle.get("row_attrs", {})),
                col_attrs=list(handle.get("col_attrs", {})),
            )
    else:
        raise ValueError(f"Unsupported data format: {path.suffix}")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--max-categories", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inspect(args.path, args.max_categories)
    if args.json:
        print(json.dumps(result, default=str, allow_nan=False))
    else:
        for key, value in result.items():
            print(f"{key}: {json.dumps(value, indent=2, default=str)}")
