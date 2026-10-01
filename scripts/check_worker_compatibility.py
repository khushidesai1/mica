"""Export the API execution contract, or check it inside a candidate worker image."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pistachio.execution.development import validation_suite_hash
from pistachio.models import ExecutionBundle, ParameterSpec, ResourcePlan, TaskContract, ToolPlan


def contract() -> dict:
    bundle = ExecutionBundle(
        task_id="deployment_contract_check",
        builder_protocol_version=2,
        workflow_version=2,
        script="",
        contract=TaskContract(objective="API/worker compatibility check"),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        parameter_manifest=[
            ParameterSpec(
                id=f"setting_{scope}",
                label=scope,
                classification="SCIENTIFIC",
                component_id="analysis",
                decision_scope=scope,
            )
            for scope in ("implementation", "method_default", "research_design")
        ],
    )
    return {
        "schema": ExecutionBundle.model_json_schema(),
        "bundle": bundle.model_dump(mode="json"),
        "runtime_contract_hash": validation_suite_hash(),
    }


def check(expected: dict) -> None:
    if expected.get("runtime_contract_hash") != validation_suite_hash():
        raise ValueError("API/worker trusted helper and validation contracts differ")
    # Parse before comparing schemas so a mismatch includes useful field-level diagnostics.
    parsed = ExecutionBundle.model_validate(expected["bundle"])
    if parsed.model_dump(mode="json") != expected["bundle"]:
        raise ValueError("Worker changes the API execution bundle during deserialization")
    if ExecutionBundle.model_json_schema() != expected["schema"]:
        raise ValueError(
            "API and worker execution schemas differ; deploy matching application code"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("export", "check"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if args.mode == "export":
        payload = json.dumps(contract(), sort_keys=True)
        if str(args.path) == "-":
            print(payload)
        else:
            args.path.write_text(payload)
    else:
        check(json.loads(args.path.read_text()))
        print("API_WORKER_CONTRACT_COMPATIBLE")
