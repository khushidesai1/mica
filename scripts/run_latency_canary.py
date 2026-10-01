"""Isolated preparation benchmark using an existing authorized task; never runs full analysis.

Uses the deployed secret and operator's gcloud identity in memory. Original task records are
read-only. Benchmark artifacts use a distinct prefix; reusable workers are stopped in finally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import google.auth
from google.cloud import firestore
from google.oauth2.credentials import Credentials

from pistachio.agents.harness import AgentHarness
from pistachio.config import Settings
from pistachio.execution.backends import GcpBatchExecutor
from pistachio.models import TaskRecord, TaskStatus
from pistachio.services.development import DevelopmentService
from pistachio.storage.memory import InMemoryRepository


async def benchmark(args):
    token = subprocess.check_output(["gcloud", "auth", "print-access-token"], text=True).strip()
    credentials = Credentials(token)
    os.environ["OPENAI_API_KEY"] = subprocess.check_output(
        [
            "gcloud",
            "secrets",
            "versions",
            "access",
            "2",
            "--secret=pistachio-staging-openai-api-key",
            "--project=" + args.project,
        ],
        text=True,
    ).strip()
    with patch.object(google.auth, "default", return_value=(credentials, args.project)):
        raw = (
            firestore.Client(project=args.project)
            .collection("tasks")
            .document(args.task)
            .get()
            .to_dict()
        )
        original = TaskRecord.model_validate(raw)
        task_id = "latency_canary_" + str(int(time.time()))
        settings = Settings(
            _env_file=None,
            env="staging",
            store_backend="memory",
            execution_backend="gcp_batch",
            gcp_project=args.project,
            gcp_region="us-central1",
            gcs_bucket=args.bucket,
            batch_image=args.image,
            batch_service_account="pistachio-staging-worker@azizilab-aml.iam.gserviceaccount.com",
            batch_nested_sandbox_opt_in=True,
            development_enabled=True,
            development_latency_enabled=True,
            worker_sessions_enabled=True,
            development_max_operations=6,
            development_seconds=1200,
            development_wall_seconds=1800,
            development_command_seconds=300,
            completion_topic="",
        )
        task = TaskRecord(
            id=task_id,
            envelope=original.envelope.model_copy(update={"external_event_id": task_id}),
            builder_protocol_version=2,
            planner_version=2,
            contract=original.contract,
            master_plan=original.master_plan,
            decision_set=original.decision_set,
            figure_specs=original.figure_specs,
            dataset_inspections=original.dataset_inspections,
            tool_plan=original.tool_plan,
            resource_plan=original.resource_plan.model_copy(
                update={"timeout_hours": 0.25, "estimated_cost_usd": 3.0}
            ),
        )
        prior_history = []
        if args.resume:
            prior = json.loads(Path(args.resume).read_text())
            task = TaskRecord.model_validate(prior["task"])
            prior_history = prior["history"]
        repository = InMemoryRepository()
        task = await repository.create_task(task)
        executor = GcpBatchExecutor(settings)
        harness = AgentHarness(repository, settings)
        service = DevelopmentService(repository, settings, harness, executor)
        from pistachio.models import utcnow

        started = time.monotonic() - (utcnow() - task.created_at).total_seconds()
        history = prior_history
        try:
            while time.monotonic() - started < 1800:
                active = (
                    next(
                        (
                            o
                            for o in reversed(task.development.operations)
                            if o.status != "completed"
                        ),
                        None,
                    )
                    if task.development
                    else None
                )
                if active and active.status == "queued":
                    try:
                        result = await executor.load_job_result(task.id, active.attempt)
                    except Exception:
                        state = await executor.get_job_status(task.execution_job_name)
                        if state.state in {"FAILED", "SUCCEEDED"}:
                            raise RuntimeError(
                                "Worker terminated before publishing operation result: "
                                + state.detail
                            ) from None
                        await asyncio.sleep(5)
                        continue
                    task = await service.advance(task, result)
                else:
                    task = await service.advance(task)
                d = task.development
                entry = {
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                    "status": task.status.value,
                    "state": d.state if d else "",
                    "revisions": d.code_revisions if d else 0,
                    "model_calls": d.model_turns if d else 0,
                    "phases": [(o.phase, o.status, o.exit_code) for o in d.operations] if d else [],
                }
                history.append(entry)
                print(json.dumps(entry), flush=True)
                tested_third = (
                    d
                    and d.code_revisions >= 3
                    and d.operations
                    and d.operations[-1].source_hash == d.source_hash
                    and d.operations[-1].status == "completed"
                )
                if d and (d.state == "verified" or tested_third):
                    break
                if task.status in {
                    TaskStatus.FAILED,
                    TaskStatus.NEEDS_INPUT,
                    TaskStatus.NEEDS_APPROVAL,
                }:
                    break
            d = task.development
            report = {
                "task_id": task.id,
                "source_task": args.task,
                "image": args.image,
                "validated": bool(d and d.state == "verified"),
                "history": history,
                "preparation_seconds": time.monotonic() - started,
                "baseline_preparation_seconds": 4485.25,
                "baseline_includes_failed_full_run": True,
                "full_analysis_executed": False,
                "task": task.model_dump(mode="json"),
            }
            Path(args.output).write_text(json.dumps(report, indent=2))
            if not report["validated"]:
                raise RuntimeError(
                    "Canary did not reach validated preparation; see retained evidence"
                )
        finally:
            await executor.stop_session(task.id)
            if task.development and task.development.worker_session_job:
                await executor.cancel(task.id, task.development.worker_session_job)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="azizilab-aml")
    parser.add_argument("--bucket", default="pistachio-staging-artifacts-5304c19c")
    parser.add_argument("--task", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--resume")
    asyncio.run(benchmark(parser.parse_args()))
