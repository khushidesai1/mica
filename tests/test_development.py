from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from agents import AgentOutputSchema

from mica.execution.backends import LocalExecutor
from mica.execution.checks import verify_outputs
from mica.execution.workspace import WorkspaceStore, digest, validate_sources
from mica.models import (
    AttachmentRef,
    CandidateReview,
    DecisionSet,
    DevelopmentAction,
    ExecutionBundle,
    MasterPlan,
    OutputSpec,
    ParameterClass,
    ParameterSpec,
    ResourcePlan,
    SourceFile,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskStatus,
    ToolPlan,
)
from mica.services.development import DevelopmentService

ANALYSIS = '''"""Sum the supplied numbers using the authorized multiplier."""
import argparse
import json
from pathlib import Path

def transform(values, multiplier):
    return sum(values) * multiplier

def main():
    parser = argparse.ArgumentParser()
    for key in ('input-dir', 'output-dir', 'config'):
        parser.add_argument('--' + key, type=Path, required=True)
    parser.add_argument('--seed', type=int, required=True)
    parser.add_argument('--mode', choices=['smoke', 'run'], required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    values = json.loads((args.input_dir / 'values.json').read_text())
    answer = transform(values, config['parameters']['multiplier'])
    (args.output_dir / 'summary.json').write_text(json.dumps({'answer': answer, 'seed': args.seed}))
if __name__ == '__main__':
    main()
'''
REGRESSION = """import unittest
from analysis import transform
class Tests(unittest.TestCase):
    def test_hand_computed(self):
        self.assertEqual(transform([2, 3], 2), 10)
    def test_empty(self):
        self.assertEqual(transform([], 2), 0)
"""


def configuration(settings):
    return settings.model_copy(
        update={
            "development_enabled": True,
            "development_preparation_enabled": False,  # Exercise persisted legacy sessions.
            "worker_sandbox": "test",
            "builder_protocol_version": 2,
            "development_command_seconds": 10,
        }
    )


def test_development_flags_load_from_environment(monkeypatch):
    from mica.config import Settings

    monkeypatch.setenv("MICA_BUILDER_PROTOCOL_VERSION", "2")
    monkeypatch.setenv("MICA_DEVELOPMENT_ENABLED", "true")
    monkeypatch.setenv("MICA_DOCKER_ARTIFACT_ROOT", "")
    settings = Settings(_env_file=None)
    assert settings.builder_protocol_version == 2
    assert settings.development_enabled
    assert settings.docker_artifact_root is None


def make_bundle(tmp_path: Path, phase="test", *, script=ANALYSIS, attempt=1):
    data = tmp_path / "values.json"
    data.write_text("[1, 2, 3]")
    files = {"analysis.py": script, "tests/test_analysis.py": REGRESSION}
    return ExecutionBundle(
        task_id="task_dev",
        builder_protocol_version=2,
        operation_id=f"op-{attempt}",
        operation_phase=phase,
        attempt=attempt,
        script=script,
        source_files=[SourceFile(path=k, content=v) for k, v in files.items()],
        source_hash=digest(files),
        runtime_image="test-process",
        command_timeout_seconds=10,
        contract=TaskContract(
            objective="Sum data", requested_outputs=["summary.json"], requires_execution=True
        ),
        resource_plan=ResourcePlan(),
        tool_plan=ToolPlan(),
        parameter_manifest=[
            ParameterSpec(
                id="multiplier",
                label="Multiplier",
                component_id="sum",
                classification=ParameterClass.SCIENTIFIC,
                proposed_value=2,
                confirmed_by_user=True,
            ),
            ParameterSpec(
                id="seed",
                label="Seed",
                component_id="sum",
                classification=ParameterClass.SCIENTIFIC,
                proposed_value=42,
                confirmed_by_user=True,
            ),
        ],
        output_specs=[
            OutputSpec(
                path="summary.json",
                requested_output="summary.json",
                format="json",
                required_fields=["answer", "seed"],
            )
        ],
        input_attachments=[
            AttachmentRef(
                filename="values.json",
                uri=data.as_uri(),
                checksum_sha256=hashlib.sha256(data.read_bytes()).hexdigest(),
                size_bytes=data.stat().st_size,
            )
        ],
    )


@pytest.mark.asyncio
async def test_clean_verification_full_run_and_portable_package(settings, tmp_path):
    settings = configuration(settings)
    executor = LocalExecutor(settings)
    bundle = make_bundle(tmp_path, "verify")
    verified = await executor.execute(bundle)
    assert verified.exit_code == 0, verified.model_dump()
    assert verified.verification.passed
    assert verified.artifacts == []
    full = bundle.model_copy(
        update={
            "operation_id": "op-2",
            "operation_phase": "run",
            "attempt": 2,
            "verification": verified.verification,
        }
    )
    result = await executor.execute(full)
    assert result.exit_code == 0, result.model_dump()
    assert result.publication_committed
    names = {a.name for a in result.artifacts}
    assert names == {"summary.json", "reproduction.zip"}
    output = settings.artifact_root / bundle.task_id / "attempt-2"
    assert json.loads((output / "outputs/summary.json").read_text()) == {"answer": 12, "seed": 42}
    with zipfile.ZipFile(output / "delivery/reproduction.zip") as archive:
        assert archive.read("reproduce/source/analysis.py").decode() == ANALYSIS
        assert "reproduce/dependencies.lock" in archive.namelist()
        assert not any("values.json" in name for name in archive.namelist())
    repeated = await executor.execute(full)
    assert repeated == result
    (output / "outputs/summary.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity"):
        await executor.execute(full)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        "pass",
        "(args.output_dir / 'summary.json').write_text('{}')",
        "(args.output_dir / 'extra.txt').write_text('unrequested')",
    ],
)
async def test_zero_exit_is_not_success(settings, tmp_path, replacement):
    script = ANALYSIS.replace(
        "(args.output_dir / 'summary.json').write_text("
        "json.dumps({'answer': answer, 'seed': args.seed}))",
        replacement,
    )
    result = await LocalExecutor(configuration(settings)).execute(
        make_bundle(tmp_path, script=script)
    )
    assert result.exit_code != 0
    assert result.failure.phase == "output-validation"
    assert result.failure.category == "validation"
    assert result.artifacts == []


@pytest.mark.asyncio
async def test_full_run_rejects_modified_configuration(settings, tmp_path):
    executor = LocalExecutor(configuration(settings))
    bundle = make_bundle(tmp_path, "verify")
    result = await executor.execute(bundle)
    modified = bundle.model_copy(deep=True)
    modified.attempt = 2
    modified.operation_id = "op2"
    modified.operation_phase = "run"
    modified.verification = result.verification
    modified.parameter_manifest[0].proposed_value = 9
    run = await executor.execute(modified)
    assert run.exit_code != 0
    assert "changed" in run.failure.message


@pytest.mark.asyncio
async def test_timeout_retains_diagnostics(settings, tmp_path):
    script = ANALYSIS.replace(
        "answer = transform(values,",
        'print("before timeout", flush=True)\n    import time\n'
        "    time.sleep(10)\n    answer = transform(values,",
    )
    bundle = make_bundle(tmp_path, script=script).model_copy(update={"command_timeout_seconds": 1})
    result = await LocalExecutor(configuration(settings)).execute(bundle)
    assert result.exit_code == 124
    assert result.failure.category == "resource"
    assert "before timeout" in result.stdout
    assert result.audit_artifacts


@pytest.mark.parametrize("path", ["../escape.py", "/root.py", "a//b.py", "a/./b.py", "a\\b.py"])
def test_reject_workspace_escape(path):
    with pytest.raises(ValueError):
        validate_sources([SourceFile(path=path, content="pass")])


def test_output_checks_reject_symlinks_and_invalid_json(tmp_path):
    spec = {"path": "result.json", "format": "json", "required_fields": [], "allow_empty": False}
    (tmp_path / "result.json").write_text('{"value": NaN}')
    with pytest.raises(ValueError, match="nonfinite"):
        verify_outputs(tmp_path, [spec])
    (tmp_path / "result.json").unlink()
    (tmp_path / "result.json").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        verify_outputs(tmp_path, [spec])


def test_agent_schemas_are_strict():
    for output in [DevelopmentAction, CandidateReview]:
        assert (
            AgentOutputSchema(output, strict_json_schema=True).json_schema()["additionalProperties"]
            is False
        )


class RepairingDeveloper:
    def __init__(self):
        self.calls = 0
        self.result_review = CandidateReview(approved=True)

    async def development_action(self, task_id, envelope, payload):
        self.calls += 1
        if self.calls <= 2:
            source = (
                ANALYSIS.replace(
                    "return sum(values) * multiplier", "return sum(values) + multiplier"
                )
                if self.calls == 1
                else ANALYSIS
            )
            return DevelopmentAction(
                action="edit_files",
                base_revision=payload["source_hash"],
                design="Test sums on real data.",
                files=[
                    SourceFile(path="analysis.py", content=source),
                    SourceFile(path="tests/test_analysis.py", content=REGRESSION),
                ],
                output_specs=[
                    OutputSpec(
                        path="summary.json",
                        requested_output="summary.json",
                        format="json",
                        required_fields=["answer", "seed"],
                    )
                ],
            )
        return DevelopmentAction(action="submit_candidate")

    async def review_candidate(self, task_id, envelope, payload):
        return CandidateReview(approved=True)

    async def review_results(self, task_id, envelope, payload):
        assert payload["actual_results"][0]["name"] == "summary.json"
        assert '"answer": 12' in payload["actual_results"][0]["content"]
        return self.result_review


@pytest.mark.asyncio
@pytest.mark.parametrize("review_outcome", ["approve", "repair", "amend", "method_default"])
async def test_durable_development_repairs_and_resumes(
    repository, settings, tmp_path, review_outcome
):
    settings = configuration(settings)
    bundle = make_bundle(tmp_path)
    master = MasterPlan(
        task_id=bundle.task_id, contract=bundle.contract, parameters=bundle.parameter_manifest
    )
    task = TaskRecord(
        id=bundle.task_id,
        builder_protocol_version=2,
        planner_version=2,
        envelope=TaskEnvelope(
            project_id="project",
            conversation_id="conversation",
            principal_id="alice",
            prompt="Sum data",
            attachments=bundle.input_attachments,
        ),
        contract=bundle.contract,
        master_plan=master,
        decision_set=DecisionSet(
            task_id=bundle.task_id, master_plan_id=master.id, master_plan_revision=1
        ),
        resource_plan=bundle.resource_plan,
        tool_plan=bundle.tool_plan,
    )
    await repository.create_task(task)
    developer = RepairingDeveloper()
    if review_outcome != "approve":
        developer.result_review = CandidateReview(
            approved=False,
            issues=["A result-level issue requires attention."],
            scientific_amendment_required=review_outcome in {"amend", "method_default"},
            scientific_impact="Changing the cohort would change the research population."
            if review_outcome == "amend" else "Graph defaults can change the labels.",
            decision_scope="research_design" if review_outcome == "amend" else "method_default",
        )
    service = DevelopmentService(repository, settings, developer, LocalExecutor(settings))
    task = await service.advance(task)  # runtime/input probe
    task = await service.advance(task)  # failing candidate
    store = WorkspaceStore(settings, task.id)
    # Execute a check, then reconstruct the service as if the API restarted.
    task = await service.apply_action(
        task,
        DevelopmentAction(action="run_check"),
        await store.get(task.development.source_ref),
        store,
    )
    assert task.development.operations[-1].exit_code != 0
    service = DevelopmentService(repository, settings, developer, LocalExecutor(settings))
    task = await service.advance(await repository.get_task(task.id))  # repair
    task = await service.advance(task)  # review + clean verification
    assert task.development.state == "verified", task.development.feedback
    frozen = task.development.source_hash
    with pytest.raises(ValueError, match="immutable"):
        await service.apply_action(
            task,
            DevelopmentAction(
                action="edit_files",
                base_revision=frozen,
                files=[SourceFile(path="analysis.py", content=ANALYSIS + "\n# revised\n")],
                output_specs=[
                    OutputSpec(
                        path="summary.json", requested_output="summary.json", format="text"
                    )
                ],
            ),
            await store.get(task.development.source_ref),
            store,
        )
    assert task.development.source_hash == frozen
    task = await service.advance(task)  # full run
    assert task.status == TaskStatus.PLANNING
    assert task.result is None
    task = await service.advance(task)  # inspect actual result evidence before delivery
    assert task.status == {
        "approve": TaskStatus.SUCCEEDED,
        "repair": TaskStatus.PLANNING,
        "amend": TaskStatus.NEEDS_INPUT,
        "method_default": TaskStatus.PLANNING,
    }[review_outcome], task.error
    assert task.development.code_revisions == 2
    assert task.development.full_runs == 1
    assert len(task.development.operations) == 4
    if review_outcome == "approve":
        assert task.result.publication_committed
    else:
        assert task.result is None
    assert task.development.model_turns == 5


@pytest.mark.asyncio
async def test_cas_rejects_stale_worker_and_cancellation(repository):
    task = TaskRecord(
        envelope=TaskEnvelope(project_id="p", conversation_id="c", principal_id="a", prompt="x")
    )
    await repository.create_task(task)
    saved = await repository.compare_and_swap_task(task, 0)
    assert saved.development_version == 1
    assert await repository.compare_and_swap_task(task, 0) is None
    cancelled = saved.model_copy(update={"interaction_revision": 1, "status": TaskStatus.CANCELLED})
    await repository.update_task(cancelled)
    assert await repository.compare_and_swap_task(saved, 1) is None


def test_checkpoint_keys_follow_producer_dependencies():
    from mica.execution.checkpoints import checkpoint_key
    from mica.models import CheckpointSpec

    files = {
        "composition.py": "import helper\ndef compose(): return helper.value\n",
        "helper.py": "value = 1\n",
        "assessment.py": "value = 1\n",
    }
    spec = CheckpointSpec(
        id="compositions", source_paths=["composition.py"], required_fields=["values"]
    )
    key = checkpoint_key(spec, files, {"k": 30}, "input-hash", "runtime-hash")
    repaired = {**files, "assessment.py": "value = 2\n"}
    assert checkpoint_key(spec, repaired, {"k": 30}, "input-hash", "runtime-hash") == key
    assert (
        checkpoint_key(
            spec, {**files, "helper.py": "value = 2\n"}, {"k": 30}, "input-hash", "runtime-hash"
        )
        != key
    )
    assert checkpoint_key(spec, files, {"k": 29}, "input-hash", "runtime-hash") != key
    assert checkpoint_key(spec, files, {"k": 30}, "other-input", "runtime-hash") != key


@pytest.mark.asyncio
async def test_downstream_repair_reuses_private_checkpoint(settings, tmp_path):
    from mica.models import CheckpointSpec

    settings = configuration(settings)
    source = ANALYSIS.replace(
        "answer = transform(values,",
        """from task_checkpoints import load, save
    cached = load('compositions')
    if cached is None:
        print('computing compositions')
        save('compositions', {'values': values})
    else:
        print('reusing compositions')
        values = cached['values']
    if args.mode == 'run':
        raise RuntimeError('downstream scoring bug')
    answer = transform(values,""",
    )
    bundle = make_bundle(tmp_path, "verify", script=source)
    bundle.source_files.append(
        SourceFile(path="composition.py", content="def produce(x): return x\n")
    )
    bundle.source_hash = digest({f.path: f.content for f in bundle.source_files})
    bundle.checkpoint_specs = [
        CheckpointSpec(
            id="compositions", source_paths=["composition.py"], required_fields=["values"]
        )
    ]
    executor = LocalExecutor(settings)
    verify = await executor.execute(bundle)
    assert verify.exit_code == 0, verify.model_dump()
    failed_bundle = bundle.model_copy(
        update={
            "operation_phase": "run",
            "attempt": 2,
            "operation_id": "failed-run",
            "verification": verify.verification,
        }
    )
    failed = await executor.execute(failed_bundle)
    assert failed.exit_code != 0
    cached = [a for a in failed.audit_artifacts if a.name.startswith("checkpoints/")]
    assert len(cached) == 1
    repaired = bundle.model_copy(deep=True)
    repaired.source_files[0].content = source.replace(
        "raise RuntimeError('downstream scoring bug')", "pass"
    )
    repaired.source_hash = digest({f.path: f.content for f in repaired.source_files})
    repaired.attempt = 3
    repaired.operation_id = "repair-verify"
    verified = await executor.execute(repaired)
    assert verified.exit_code == 0, verified.model_dump()
    repaired.operation_phase = "run"
    repaired.attempt = 4
    repaired.operation_id = "repair-run"
    repaired.verification = verified.verification
    repaired.checkpoint_inputs = cached
    result = await executor.execute(repaired)
    assert result.exit_code == 0, result.model_dump()
    assert "reusing compositions" in result.stdout


@pytest.mark.asyncio
async def test_task_service_routes_new_protocol_and_preserves_legacy_default(repository, settings):
    from conftest import task_service

    from mica.agents.harness import AgentHarness
    from mica.models import ApprovalDecision, ApprovalStatus, Project

    settings = configuration(settings).model_copy(update={"planner_version": 2})

    class IntegratedHarness(AgentHarness):
        async def development_action(self, task_id, envelope, payload):
            if payload["files"]:
                return DevelopmentAction(action="submit_candidate")
            source = ANALYSIS.replace(
                "values = json.loads((args.input_dir / 'values.json').read_text())",
                "values = [2, 3]",
            ).replace(
                "config['parameters']['multiplier']", "config['parameters'].get('multiplier', 2)"
            )
            return DevelopmentAction(
                action="edit_files",
                base_revision=payload["source_hash"],
                files=[
                    SourceFile(path="analysis.py", content=source),
                    SourceFile(path="tests/test_analysis.py", content=REGRESSION),
                ],
                output_specs=[
                    OutputSpec(
                        path="summary.json",
                        requested_output="summary.json",
                        format="json",
                        required_fields=["answer", "seed"],
                    )
                ],
            )

        async def review_candidate(self, task_id, envelope, payload):
            return CandidateReview(approved=True)

        async def review_results(self, task_id, envelope, payload):
            return CandidateReview(approved=True)

    project = await repository.create_project(Project(slug="dev", name="Dev", owner="alice"))
    harness = IntegratedHarness(repository, settings)
    service = task_service(repository, settings, harness)
    task, _ = await service.create(
        TaskEnvelope(
            project_id=project.id,
            conversation_id="c",
            principal_id="alice",
            prompt="Calculate (2 + 3) times 2. Create summary.json.",
        )
    )
    assert task.builder_protocol_version == 2
    for _ in range(12):
        task = await service.process(task.id)
        if task.status == TaskStatus.NEEDS_APPROVAL:
            for approval in task.approvals:
                if approval.status == ApprovalStatus.PENDING:
                    task = await service.decide_approval(
                        task.id,
                        approval.id,
                        "alice",
                        ApprovalDecision(approved=True),
                        defer_replan=True,
                    )
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
            break
    assert task.status == TaskStatus.SUCCEEDED, task.error
    assert task.development.full_runs == 1
    assert TaskRecord(envelope=task.envelope).builder_protocol_version == 1
