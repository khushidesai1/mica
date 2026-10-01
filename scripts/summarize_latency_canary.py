"""Summarize immutable canary evidence without repeating source code or full logs."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.cloud import storage
from google.oauth2.credentials import Credentials


def summarize(report_path: Path, project: str, bucket_name: str) -> dict:
    report = json.loads(report_path.read_text())
    development = report["task"]["development"]
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    bucket = storage.Client(project=project, credentials=Credentials(token)).bucket(bucket_name)

    def read(ref: str) -> dict:
        data = bucket.blob(ref).download_as_bytes()
        if hashlib.sha256(data).hexdigest() != Path(ref).stem:
            raise ValueError("Immutable evidence checksum mismatch")
        return json.loads(data)

    refs = development["action_refs"] + development["review_refs"]
    operations = [op for op in development["operations"] if op["result_ref"]]
    refs += [op["result_ref"] for op in operations]
    with ThreadPoolExecutor(max_workers=6) as pool:
        evidence = dict(zip(refs, pool.map(read, refs), strict=True))
    models = [
        {"kind": kind, "ref": ref, **evidence[ref].get("metrics", {})}
        for kind, field in (("author", "action_refs"), ("review", "review_refs"))
        for ref in development[field]
    ]
    return {
        "task_id": report["task_id"],
        "source_report": str(report_path),
        "worker_image": report["image"],
        "validated_preparation": report["validated"],
        "full_analysis_executed": report["full_analysis_executed"],
        "preparation_seconds": report["preparation_seconds"],
        "baseline_seconds": report["baseline_preparation_seconds"],
        "baseline_includes_failed_full_run": report["baseline_includes_failed_full_run"],
        "meets_preparation_time_target": bool(
            report["validated"]
            and report["preparation_seconds"] <= report["baseline_preparation_seconds"] * 0.5
        ),
        "source_versions": development["code_revisions"],
        "first_candidate_outcome": development["first_candidate_outcome"],
        "model_calls": development["model_turns"],
        "model_seconds": sum(m.get("model_duration_ms", 0) for m in models) / 1000,
        "input_tokens": sum(m.get("input_tokens", 0) for m in models),
        "cached_input_tokens": sum(m.get("cached_input_tokens", 0) for m in models),
        "output_tokens": sum(m.get("output_tokens", 0) for m in models),
        "models": models,
        "operations": [
            {
                "phase": op["phase"],
                "attempt": op["attempt"],
                "exit_code": op["exit_code"],
                "source_hash": op["source_hash"],
                "ref": op["result_ref"],
                "timings": evidence[op["result_ref"]].get("timings", {}),
                "phase_timestamps": evidence[op["result_ref"]].get("phase_timestamps", {}),
            }
            for op in operations
        ],
        "final_review": development.get("candidate_review"),
        "reserved_compute_cost_usd": development["reserved_cost_usd"],
        "billing_cost_usd": None,
        "limitations": [
            "One bounded preparation run; acknowledgement/planning percentiles were not measured.",
            "Historical baseline includes a failed full run; "
            "this canary stops before full analysis.",
            "Reserved compute cost is a budget estimate, not actual model or cloud billing.",
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", default="azizilab-aml")
    parser.add_argument("--bucket", default="pistachio-staging-artifacts-5304c19c")
    args = parser.parse_args()
    args.output.write_text(json.dumps(summarize(args.report, args.project, args.bucket), indent=2))
