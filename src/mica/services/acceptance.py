"""Stable acceptance criteria and compact runtime evidence for workflow two."""

from __future__ import annotations

import json
from typing import Any

from mica.models import TaskRecord


def checklist(task: TaskRecord) -> list[str]:
    assert task.master_plan
    return [
        "authority: preserve every authorized scientific parameter and requested output meaning",
        "inputs: use the generation-pinned input, observed representations and positional rows",
        "bindings: consume typed config parameters; verify installed API signatures/defaults",
        "scale: account for full population, backend thresholds, all resolutions and peak memory",
        "production: exercise production initialization/resource checks in the actual sandbox",
        "tests: independent expected values, real production functions, edge/partial batches",
        "integration: bounded real-input pipeline through analysis, scoring and serialization",
        "outputs: exact configured paths/formats, semantic checks and readable artifacts",
        "progress: report loading, composition, graph construction and each applicable resolution",
        *[f"deliverable: {value}" for value in task.master_plan.contract.requested_outputs],
    ]


def compact_observation(raw: dict[str, Any], phase: str) -> dict[str, Any]:
    """Keep machine evidence, not repeated package listings and successful logs."""
    out: dict[str, Any] = {"failure": raw.get("failure"), "timings": raw.get("timings", {})}
    stdout = raw.get("stdout", "")
    if phase == "probe" and raw.get("runtime_evidence"):
        out["runtime"] = raw["runtime_evidence"]
        return out
    if phase == "probe":
        for line in stdout.splitlines():
            try:
                probe = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(probe, dict) and "inputs" in probe:
                out["runtime"] = {
                    k: probe[k]
                    for k in ("inputs", "api_capabilities", "sandbox", "python", "architecture")
                    if k in probe
                }
                break
    elif raw.get("exit_code") != 0:
        out["stderr"] = raw.get("stderr", "")[-8000:]
    else:
        report = raw.get("verification") or {}
        out["checks"] = report.get("checks", [])
    return out


def helper_contracts() -> dict[str, str]:
    """Use actual installed signatures; a helper name alone is not an API contract."""
    import inspect

    from mica.execution import runtime_support

    return {
        name: str(inspect.signature(getattr(runtime_support, name)))
        for name in (
            "parameter",
            "positional_rows",
            "resource_limits",
            "require_memory",
            "input_path",
            "output_path",
            "progress",
        )
    }
