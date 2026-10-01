from __future__ import annotations

import json

from conftest import task_service

from pistachio.agents.harness import AgentStageTimeout
from pistachio.models import (
    AnalysisStep,
    ApprovalDecision,
    CodeBuildResult,
    DecisionSet,
    FigurePlan,
    MasterPlan,
    ParameterClass,
    ParameterSpec,
    Project,
    StageKind,
    StageStatus,
    TaskContract,
    TaskEnvelope,
    TaskStatus,
    ToolPlan,
)


class StagedHarness:
    def __init__(self) -> None:
        self.master_calls = 0
        self.figure_calls = 0
        self.scout_calls = 0

    async def create_master_plan(self, *, task_id, envelope, context):
        del context
        self.master_calls += 1
        return (
            MasterPlan(
                task_id=task_id,
                contract=TaskContract(
                    objective=envelope.prompt,
                    requested_outputs=["A concise answer"],
                    authorized_scientific_actions=[envelope.prompt],
                    requires_execution=False,
                ),
                steps=[
                    AnalysisStep(
                        id="cluster",
                        title="Cluster neighborhoods",
                        description="Cluster the requested composition vectors.",
                        parameter_ids=["cluster_count", "bar_normalization"],
                    )
                ],
                parameters=[
                    ParameterSpec(
                        id="cluster_count",
                        label="Candidate cluster counts",
                        classification=ParameterClass.SCIENTIFIC,
                        component_id="cluster",
                        proposed_value="2-8",
                        options=["2-8", "2-12"],
                        scientific_impact="Changes the selected neighborhood partition.",
                        requires_approval=True,
                    ),
                    ParameterSpec(
                        id="bar_normalization",
                        label="Stacked-bar normalization",
                        classification=ParameterClass.VISUAL_SEMANTIC,
                        component_id="cluster",
                        proposed_value="within-neighborhood proportions",
                        scientific_impact="Changes the interpretation of bar heights.",
                        requires_approval=True,
                    ),
                    ParameterSpec(
                        id="palette",
                        label="Palette",
                        classification=ParameterClass.PRESENTATION,
                        component_id="figure",
                        proposed_value="colorblind-safe",
                        requires_approval=True,
                    ),
                    ParameterSpec(
                        id="threads",
                        label="Threads",
                        classification=ParameterClass.TECHNICAL,
                        component_id="runtime",
                        proposed_value=2,
                        requires_approval=True,
                    ),
                ],
                response="Confirm the two interpretation-changing settings.",
            ),
            {"model_calls": 1, "model_duration_ms": 25},
        )

    async def create_figure_plan(self, *, task_id, envelope, master_plan, decisions, context):
        del task_id, envelope, master_plan, decisions, context
        self.figure_calls += 1
        return FigurePlan(), {"model_calls": 0, "skipped": True}

    async def scout_tools(self, **kwargs):
        del kwargs
        self.scout_calls += 1
        return ToolPlan(), {"model_calls": 1}

    async def compact_session(self, conversation_id: str) -> None:
        del conversation_id


class ExecutableStagedHarness(StagedHarness):
    def __init__(self) -> None:
        super().__init__()
        self.code_calls = 0

    async def create_master_plan(self, *, task_id, envelope, context):
        del context
        self.master_calls += 1
        return (
            MasterPlan(
                task_id=task_id,
                contract=TaskContract(
                    objective=envelope.prompt,
                    requested_outputs=["result.json"],
                    authorized_scientific_actions=[envelope.prompt],
                    requires_execution=True,
                ),
                steps=[
                    AnalysisStep(
                        id="analysis",
                        title="Requested GPU analysis",
                        description="This explicitly requires GPU compute.",
                    )
                ],
                response="The immutable plan is ready.",
            ),
            {"model_calls": 1},
        )

    async def build_code(self, **kwargs):
        del kwargs
        self.code_calls += 1
        return (
            CodeBuildResult(
                script=(
                    '"""Write the authorized deterministic result."""\n'
                    "import argparse\n"
                    "import json\n"
                    "from pathlib import Path\n"
                    "def main():\n"
                    "    parser = argparse.ArgumentParser()\n"
                    "    parser.add_argument('--input-dir', type=Path, required=True)\n"
                    "    parser.add_argument('--output-dir', type=Path, required=True)\n"
                    "    parser.add_argument('--seed', type=int, required=True)\n"
                    "    parser.add_argument('--preflight', action='store_true')\n"
                    "    args = parser.parse_args()\n"
                    "    if args.preflight:\n"
                    "        assert args.input_dir.is_dir()\n"
                    "        return\n"
                    "    args.output_dir.mkdir(parents=True, exist_ok=True)\n"
                    "    result_path = args.output_dir / 'result.json'\n"
                    "    result_path.write_text(json.dumps({'seed': args.seed}))\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n"
                ),
                response="Code generated from the approved plan.",
            ),
            {"model_calls": 1},
        )


class TimeoutThenSuccessHarness(StagedHarness):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    async def create_master_plan(self, **kwargs):
        if self.fail_once:
            self.fail_once = False
            raise AgentStageTimeout(
                "Master Planner (gpt-5.6-sol) exceeded its 180-second model guardrail."
            )
        return await super().create_master_plan(**kwargs)


class RuntimeRepairHarness(ExecutableStagedHarness):
    def __init__(self, *, repair_succeeds: bool) -> None:
        super().__init__()
        self.repair_succeeds = repair_succeeds
        self.repair_requests: list[dict[str, object]] = []

    async def build_code(self, **kwargs):
        self.code_calls += 1
        if self.code_calls == 1:
            failure = "ValueError: cannot reshape array of size 2699997 into shape (50000,27)"
            body = f"raise ValueError({failure!r})"
        else:
            self.repair_requests.append(kwargs)
            body = (
                '(args.output_dir / "result.json").write_text(json.dumps({"repaired": True}))'
                if self.repair_succeeds
                else 'raise RuntimeError("the revised implementation still failed")'
            )
        script = (
            '"""Execute the approved task."""\n'
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "def main():\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('--input-dir', type=Path, required=True)\n"
            "    parser.add_argument('--output-dir', type=Path, required=True)\n"
            "    parser.add_argument('--seed', type=int, required=True)\n"
            "    parser.add_argument('--preflight', action='store_true')\n"
            "    args = parser.parse_args()\n"
            "    if args.preflight:\n"
            "        assert args.input_dir.is_dir()\n"
            "        return\n"
            "    args.output_dir.mkdir(parents=True, exist_ok=True)\n"
            f"    {body}\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        return CodeBuildResult(script=script, response="Code generated."), {"model_calls": 1}


class PreflightRepairHarness(ExecutableStagedHarness):
    def __init__(self) -> None:
        super().__init__()
        self.repair_requests: list[dict[str, object]] = []

    async def build_code(self, **kwargs):
        self.code_calls += 1
        if self.code_calls > 1:
            self.repair_requests.append(kwargs)
        assertion = (
            "assert observed == (3, 2), f'shape mismatch: {observed}'"
            if self.code_calls == 1
            else "assert observed == (2, 3)"
        )
        script = (
            '"""Validate shapes before writing the requested result."""\n'
            "import argparse\n"
            "import json\n"
            "from pathlib import Path\n"
            "def main():\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('--input-dir', type=Path, required=True)\n"
            "    parser.add_argument('--output-dir', type=Path, required=True)\n"
            "    parser.add_argument('--seed', type=int, required=True)\n"
            "    parser.add_argument('--preflight', action='store_true')\n"
            "    args = parser.parse_args()\n"
            "    if args.preflight:\n"
            "        observed = (2, 3)\n"
            f"        {assertion}\n"
            "        return\n"
            "    args.output_dir.mkdir(parents=True, exist_ok=True)\n"
            "    (args.output_dir / 'result.json').write_text(json.dumps({'ok': True}))\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        return CodeBuildResult(script=script, response="Code generated."), {"model_calls": 1}


async def test_v2_approval_resumes_without_rerunning_master(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="incremental", name="Incremental", owner="alice@example.org")
    )
    harness = StagedHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-v2",
            principal_id="alice@example.org",
            prompt="Cluster neighborhood composition vectors and summarize the result.",
        )
    )

    task = await service.process(task.id)
    assert task.status == TaskStatus.NEEDS_APPROVAL
    assert harness.master_calls == 1
    approval = task.approvals[0]
    assert [item["id"] for item in approval.payload["fields"]] == [
        "cluster_count",
        "bar_normalization",
    ]

    task = await service.decide_approval(
        task.id,
        approval.id,
        "alice@example.org",
        ApprovalDecision(
            approved=True,
            response=json.dumps(
                {
                    "values": {
                        "cluster_count": "2-12",
                        "bar_normalization": "within-neighborhood proportions",
                    }
                }
            ),
        ),
        defer_replan=True,
    )
    assert task.status == TaskStatus.RECEIVED
    assert isinstance(task.decision_set, DecisionSet)

    task = await service.process(task.id)
    assert task.status == TaskStatus.SUCCEEDED
    assert harness.master_calls == 1
    assert harness.scout_calls == 0
    assert task.decision_set and task.decision_set.values["cluster_count"] == "2-12"
    assert any(
        stage.kind == StageKind.MASTER_PLAN
        and stage.status == StageStatus.COMPLETED
        and stage.attempts == 1
        for stage in task.stage_summaries
    )
    assert all(
        parameter.requires_approval is False
        for parameter in task.master_plan.parameters
        if parameter.classification in {ParameterClass.PRESENTATION, ParameterClass.TECHNICAL}
    )


async def test_v2_rejects_stale_plan_approval(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="stale", name="Stale", owner="alice@example.org")
    )
    service = task_service(repository, settings, StagedHarness())
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-stale",
            principal_id="alice@example.org",
            prompt="Cluster neighborhoods.",
        )
    )
    task = await service.process(task.id)
    approval = task.approvals[0]
    approval.payload["master_plan_revision"] = 99
    await repository.update_task(task)

    try:
        await service.decide_approval(
            task.id,
            approval.id,
            "alice@example.org",
            ApprovalDecision(
                approved=True,
                response=json.dumps(
                    {
                        "values": {
                            "cluster_count": "2-8",
                            "bar_normalization": "within-neighborhood proportions",
                        }
                    }
                ),
            ),
        )
    except ValueError as exc:
        assert "stale master-plan revision" in str(exc)
    else:
        raise AssertionError("stale approval was accepted")


async def test_v2_compute_approval_submits_without_replanning(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="compute", name="Compute", owner="alice@example.org")
    )
    harness = ExecutableStagedHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-compute",
            principal_id="alice@example.org",
            prompt="Run the authorized method; it requires GPU compute.",
        )
    )

    task = await service.process(task.id)
    assert task.status == TaskStatus.NEEDS_APPROVAL
    assert task.approvals[-1].kind.value == "COMPUTE"
    assert harness.master_calls == 1
    assert harness.code_calls == 1

    task = await service.decide_approval(
        task.id,
        task.approvals[-1].id,
        "alice@example.org",
        ApprovalDecision(approved=True),
        defer_replan=True,
    )
    assert task.status == TaskStatus.RECEIVED
    task = await service.process(task.id)
    assert task.status == TaskStatus.SUCCEEDED
    assert harness.master_calls == 1
    assert harness.code_calls == 1
    assert task.result and any(item.name == "result.json" for item in task.result.artifacts)


async def test_v2_timeout_preserves_failed_stage_and_retries_only_it(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="timeout", name="Timeout", owner="alice@example.org")
    )
    harness = TimeoutThenSuccessHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-timeout",
            principal_id="alice@example.org",
            prompt="Cluster neighborhoods.",
        )
    )

    task = await service.process(task.id)
    assert task.status == TaskStatus.FAILED
    failed = [stage for stage in task.stage_summaries if stage.kind == StageKind.MASTER_PLAN]
    assert len(failed) == 1
    assert failed[0].status == StageStatus.FAILED
    assert failed[0].metrics["outcome"] == "timeout"

    task = await service.process(task.id)
    master_stages = [stage for stage in task.stage_summaries if stage.kind == StageKind.MASTER_PLAN]
    assert [stage.status for stage in master_stages] == [
        StageStatus.FAILED,
        StageStatus.COMPLETED,
    ]
    assert harness.master_calls == 1


async def test_v2_runtime_failure_repairs_code_and_reruns(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="runtime-repair", name="Runtime repair", owner="alice@example.org")
    )
    harness = RuntimeRepairHarness(repair_succeeds=True)
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-runtime-repair",
            principal_id="alice@example.org",
            prompt="Create the requested result.",
        )
    )

    task = await service.process(task.id)
    if task.status == TaskStatus.NEEDS_APPROVAL:
        task = await service.decide_approval(
            task.id,
            task.approvals[-1].id,
            "alice@example.org",
            ApprovalDecision(approved=True),
            defer_replan=True,
        )
        task = await service.process(task.id)

    assert task.status == TaskStatus.SUCCEEDED
    assert task.attempts == 2
    assert harness.code_calls == 2
    assert len(harness.repair_requests) == 1
    request = harness.repair_requests[0]
    assert "cannot reshape array" in str(request["repair_error"])
    assert "raise ValueError" in str(request["existing_script"])
    assert task.result and [item.name for item in task.result.artifacts] == [
        "result.json",
        "generated_analysis.py",
    ]
    assert any("retrying automatically" in item.message for item in task.progress_updates)
    execution_stages = [
        stage for stage in task.stage_summaries if stage.kind == StageKind.EXECUTION
    ]
    assert [stage.status for stage in execution_stages] == [
        StageStatus.FAILED,
        StageStatus.COMPLETED,
    ]


async def test_v2_real_data_preflight_repairs_before_full_analysis(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="shape-preflight", name="Shape preflight", owner="alice@example.org")
    )
    harness = PreflightRepairHarness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-shape-preflight",
            principal_id="alice@example.org",
            prompt="Create the requested result.",
        )
    )

    task = await service.process(task.id)
    if task.status == TaskStatus.NEEDS_APPROVAL:
        task = await service.decide_approval(
            task.id,
            task.approvals[-1].id,
            "alice@example.org",
            ApprovalDecision(approved=True),
            defer_replan=True,
        )
        task = await service.process(task.id)

    assert task.status == TaskStatus.SUCCEEDED
    assert task.attempts == 2
    assert harness.code_calls == 2
    assert len(harness.repair_requests) == 1
    assert "shape mismatch" in str(harness.repair_requests[0]["repair_error"])
    assert "Real-data preflight failed" in str(harness.repair_requests[0]["repair_error"])


async def test_v2_runtime_failure_stops_after_repaired_rerun(repository, settings) -> None:
    settings = settings.model_copy(update={"planner_version": 2})
    project = await repository.create_project(
        Project(slug="runtime-exhausted", name="Runtime exhausted", owner="alice@example.org")
    )
    harness = RuntimeRepairHarness(repair_succeeds=False)
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="conv-runtime-exhausted",
            principal_id="alice@example.org",
            prompt="Create the requested result.",
        )
    )

    task = await service.process(task.id)
    if task.status == TaskStatus.NEEDS_APPROVAL:
        task = await service.decide_approval(
            task.id,
            task.approvals[-1].id,
            "alice@example.org",
            ApprovalDecision(approved=True),
            defer_replan=True,
        )
        task = await service.process(task.id)

    assert task.status == TaskStatus.FAILED
    assert task.attempts == 2
    assert harness.code_calls == 2
    assert task.error and "could not be repaired automatically" in task.error
    assert "Traceback" not in task.error
    execution_stages = [
        stage for stage in task.stage_summaries if stage.kind == StageKind.EXECUTION
    ]
    assert [stage.status for stage in execution_stages] == [
        StageStatus.FAILED,
        StageStatus.FAILED,
    ]
    assert any("RuntimeError" in stage.error for stage in execution_stages)
