"""Small trusted helpers mounted as task_runtime inside every workflow-two sandbox."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def parameter(config: dict[str, Any], name: str, expected_type: type[Any]) -> Any:
    value = config["parameters"][name]
    if not isinstance(value, expected_type) or (expected_type is int and isinstance(value, bool)):
        raise ValueError(f"Parameter {name} must be {expected_type.__name__}")
    return value


def positional_rows(value: Any, positions: Any) -> Any:
    """AnnData obsm may return either a DataFrame or an ndarray."""
    return value.iloc[positions] if hasattr(value, "iloc") else value[positions]


def resource_limits(config: dict[str, Any] | None = None) -> dict[str, Any]:
    if config is None:
        config = json.loads(Path(os.environ["MICA_CONFIG"]).read_text())
    return dict(config["resources"])


def require_memory(gib: float, config: dict[str, Any] | None = None) -> None:
    limits = resource_limits(config)
    if gib < 0 or gib > limits["memory_gib"]:
        raise MemoryError("Required memory exceeds the configured worker limit")
    # Host telemetry is optional in the isolated namespace; the configured limit is authoritative.
    try:
        import psutil

        available = psutil.virtual_memory().available / 1024**3
    except (ImportError, OSError, RuntimeError):
        return
    if available < gib:
        raise MemoryError("Required memory exceeds currently available memory")


def progress(
    message: str, current: int | None = None, total: int | None = None, item: str = "analysis"
) -> None:
    event = {"status": "info", "message": message, "current": current, "total": total, "item": item}
    print("MICA_PROGRESS " + json.dumps(event), flush=True)


def input_path(input_dir: str | Path, filename: str) -> Path:
    root = Path(input_dir).resolve()
    path = root / filename
    if Path(filename).name != filename or not path.is_file() or path.resolve().parent != root:
        raise ValueError("Expected input is missing or outside the staged input directory")
    return path


def output_path(config: dict[str, Any], requested_output: str, output_dir: str | Path) -> Path:
    specs = [s for s in config["output_specs"] if s["requested_output"] == requested_output]
    if len(specs) != 1:
        raise ValueError("Deliverable must identify exactly one configured output")
    root = Path(output_dir).resolve()
    path = (root / str(specs[0]["path"])).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Output path leaves its designated directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
