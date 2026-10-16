from __future__ import annotations

import json
from types import SimpleNamespace

import h5py
import numpy as np
import pytest
from test_development import ANALYSIS, REGRESSION, configuration, make_bundle
from test_technical_autonomy import development_task

from mica.agents.harness import AgentHarness
from mica.execution.backends import LocalExecutor
from mica.execution.checks import api_capabilities, runtime_probe
from mica.execution.workspace import WorkspaceStore
from mica.models import (
    CandidateReview,
    DecisionSet,
    DevelopmentAction,
    ImplementationPlan,
    MasterPlan,
    OutputSpec,
    ParameterBinding,
    SourceFile,
    TaskEnvelope,
    TaskRecord,
    TaskResult,
    TaskStatus,
    VerificationReport,
)
from mica.services.development import DevelopmentService


def plan(probe_id="probe"):
    return ImplementationPlan(
        probe_operation_id=probe_id,
        input_contracts=["values.json is a JSON array of numbers; stream is small"],
        library_calls=["json.load, built-in sum"],
        algorithm="Sum each input once and multiply by the authorized multiplier.",
        scale_analysis="O(N) additions, O(N) loaded values, fixture N=3; bounded input.",
        peak_memory_gib=0.01,
        validation_plan=["Independent (1+2+3)*2=12; verify emitted summary schema."],
        module_paths=["analysis.py", "tests/test_analysis.py"],
        output_specs=[
            OutputSpec(
                path="summary.json",
                requested_output="summary.json",
                format="json",
                required_fields=["answer", "seed"],
            )
        ],
    )


class Reviewer:
    def __init__(self, approved=True):
        self.approved = approved
        self.calls = []

    async def review_candidate(self, task_id, envelope, payload):
        self.calls.append(payload)
        return CandidateReview(
            approved=self.approved,
            issues=[]
            if self.approved
            else [
                "Quadratic distance work has no defensible full-data runtime estimate.",
                "DataFrame coordinates require positional indexing, not column selection.",
            ],
        )


async def prepared_task(repository, settings):
    task, store = await development_task(repository, settings)
    task.development.preparation_version = 1
    task.contract.requested_outputs = ["summary.json"]
    task.development.operations[0].result_ref = await store.put(
        TaskResult(
            summary="runtime and inputs inspected",
            exit_code=0,
            verification=VerificationReport(
                operation_id="probe",
                source_hash=task.development.source_hash,
                input_hash="inputs",
                config_hash="config",
                runtime_image="",
                passed=True,
            ),
        ).model_dump(mode="json")
    )
    return task, store


async def test_design_gate_blocks_coding_and_execution(repository, settings):
    task, store = await prepared_task(repository, settings)
    service = DevelopmentService(repository, settings, Reviewer(), LocalExecutor(settings))
    with pytest.raises(ValueError, match="reviewed implementation plan"):
        await service.apply_action(task, DevelopmentAction(action="edit_files"), {}, store)
    with pytest.raises(ValueError, match="reviewed implementation plan"):
        await service.reserve(task, "verify", store)
    assert task.development.code_revisions == 0


@pytest.mark.parametrize("defect", ["probe", "memory", "parameter", "outputs", "blockers"])
async def test_design_rejects_unverified_assumptions(repository, settings, defect):
    task, store = await prepared_task(repository, settings)
    design = plan()
    if defect == "probe":
        design.probe_operation_id = "invented-evidence"
    if defect == "memory":
        design.peak_memory_gib = task.resource_plan.memory_gib + 1
    if defect == "parameter":
        design.parameter_bindings = [
            ParameterBinding(parameter_id="invented", consumer="f", api_adaptation="none")
        ]
    if defect == "outputs":
        design.output_specs[0].requested_output = "unrequested"
    if defect == "blockers":
        design.blockers = ["Exact score runtime unresolved"]
    reviewer = Reviewer()
    service = DevelopmentService(repository, settings, reviewer, LocalExecutor(settings))
    with pytest.raises(ValueError):
        await service.apply_action(
            task,
            DevelopmentAction(
                action="plan_implementation",
                implementation_plan=design,
            ),
            {},
            store,
        )
    assert not reviewer.calls and not task.development.preparation_hash


async def test_review_blocks_unsafe_design_without_user_question(repository, settings):
    task, store = await prepared_task(repository, settings)
    reviewer = Reviewer(approved=False)
    service = DevelopmentService(repository, settings, reviewer, LocalExecutor(settings))
    task = await service.apply_action(
        task,
        DevelopmentAction(
            action="plan_implementation",
            implementation_plan=plan(),
        ),
        {},
        store,
    )
    assert task.status == TaskStatus.PLANNING and task.error is None
    assert not task.development.preparation_hash
    assert "Quadratic" in task.development.feedback and "DataFrame" in task.development.feedback
    assert reviewer.calls[0]["review_phase"] == "design"


async def test_reviewed_plan_survives_restart_but_not_runtime_change(repository, settings):
    task, store = await prepared_task(repository, settings)
    service = DevelopmentService(repository, settings, Reviewer(), LocalExecutor(settings))
    task = await service.apply_action(
        task,
        DevelopmentAction(
            action="plan_implementation",
            implementation_plan=plan(),
        ),
        {},
        store,
    )
    restored = await repository.get_task(task.id)
    restarted = DevelopmentService(repository, settings, Reviewer(), LocalExecutor(settings))
    restarted.require_preparation(restored)
    restored.development.runtime_image = "different-image"
    with pytest.raises(ValueError, match="reviewed implementation plan"):
        restarted.require_preparation(restored)


async def test_complete_first_draft_gets_review_before_real_execution(
    repository, settings, tmp_path
):
    settings = configuration(settings).model_copy(update={"development_preparation_enabled": True})
    bundle = make_bundle(tmp_path)
    master = MasterPlan(contract=bundle.contract, parameters=bundle.parameter_manifest)
    task = TaskRecord(
        id=bundle.task_id,
        builder_protocol_version=2,
        envelope=TaskEnvelope(
            project_id="p",
            conversation_id="c",
            principal_id="alice",
            prompt="Sum",
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
    reviewer = Reviewer()
    service = DevelopmentService(repository, settings, reviewer, LocalExecutor(settings))
    task = await service.advance(task)
    assert task.development.preparation_version == 1
    store = WorkspaceStore(settings, task.id)
    design = plan(task.development.operations[-1].id)
    design.parameter_bindings = [
        ParameterBinding(
            parameter_id=p.id, consumer="transform", api_adaptation="Use numeric value unchanged"
        )
        for p in bundle.parameter_manifest
    ]
    task = await service.apply_action(
        task,
        DevelopmentAction(
            action="plan_implementation",
            implementation_plan=design,
        ),
        {},
        store,
    )
    edit = DevelopmentAction(
        action="edit_files",
        base_revision=task.development.source_hash,
        files=[SourceFile(path="analysis.py", content=ANALYSIS)],
        output_specs=design.output_specs,
    )
    with pytest.raises(ValueError, match="every planned module"):
        await service.apply_action(task, edit, {}, store)
    edit.files.append(SourceFile(path="tests/test_analysis.py", content=REGRESSION))
    task = await service.apply_action(task, edit, {}, store)
    files = await store.get(task.development.source_ref)
    reviewer.approved = False
    task = await service.apply_action(task, DevelopmentAction(action="run_check"), files, store)
    assert len(task.development.operations) == 1  # No worker launched on rejected code.
    reviewer.approved = True
    task = await service.apply_action(task, DevelopmentAction(action="run_check"), files, store)
    assert task.development.operations[-1].exit_code == 0
    assert task.development.first_check_passed is True
    assert task.development.code_revisions == 1
    calls = len(reviewer.calls)
    task = await service.apply_action(
        task, DevelopmentAction(action="submit_candidate"), files, store
    )
    assert len(reviewer.calls) == calls  # Review is tied to unchanged source; no duplicate review.
    assert task.development.state == "verified"


def test_probe_reveals_dataframe_vs_array_without_loading_coordinates(tmp_path):
    with h5py.File(tmp_path / "cells.h5ad", "w") as f:
        obs = f.create_group("obs")
        obs.attrs["encoding-type"] = "dataframe"
        obs.attrs["_index"] = "_index"
        obs.attrs["column-order"] = ["sample"]
        obs.create_dataset("_index", data=np.arange(3))
        obs.create_dataset("sample", data=np.ones(3))
        frame = f.create_group("obsm/spatial")
        frame.attrs["encoding-type"] = "dataframe"
        frame.attrs["_index"] = "_index"
        frame.attrs["column-order"] = ["x", "y"]
        for name in ("_index", "x", "y"):
            frame.create_dataset(name, data=np.arange(3))
        f.create_dataset("obsm/embedding", shape=(3, 8), dtype="float32")
    result = runtime_probe(tmp_path)
    structure = result["inputs"][0]["obsm_structure"]
    assert structure["spatial"]["representation"] == "pandas.DataFrame"
    assert structure["spatial"]["row_selection"] == "iloc (positional rows)"
    assert structure["spatial"]["shape"] == [3, 2]
    assert structure["embedding"]["dtype"] == "float32"
    json.dumps(result)  # No HDF5 handles, dtype objects or NumPy scalars escape.


def test_capability_probe_uses_installed_api_and_reports_missing_calls():
    result = {
        x["entrypoint"]: x
        for x in api_capabilities(
            [
                "numpy.sum",
                "numpy.nonexistent_function",
                "os.system",
            ]
        )
    }
    assert result["numpy.sum"]["available"]
    assert "axis" in result["numpy.sum"]["signature"]
    assert not result["numpy.nonexistent_function"]["available"]
    assert "os.system" not in result


@pytest.mark.parametrize("initial", [True, False])
async def test_initial_authoring_gets_higher_reasoning_and_time(
    repository, settings, monkeypatch, initial
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    harness = AgentHarness(repository, settings)
    captured = {}

    async def run(agent, prompt, context, **kwargs):
        captured.update(effort=agent.model_settings.reasoning.effort, **kwargs)
        return SimpleNamespace(final_output=DevelopmentAction(action="inspect_inputs")), {}

    monkeypatch.setattr(harness, "_run_bounded", run)
    await harness.development_action(
        "task",
        TaskEnvelope(project_id="p", conversation_id="c", principal_id="a", prompt="x"),
        {"initial_effort": initial},
    )
    assert captured["effort"] == (
        settings.development_initial_reasoning_effort
        if initial
        else settings.openai_reasoning_effort
    )
    assert captured["timeout"] == (
        settings.development_initial_timeout_seconds if initial else settings.worker_timeout_seconds
    )
