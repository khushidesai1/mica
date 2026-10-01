from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import task_service
from test_planner_v2 import StagedHarness

from pistachio.agents.harness import AgentHarness
from pistachio.models import (
    AnalysisStep,
    ConversationMessage,
    DecisionScope,
    DecisionSet,
    MasterPlan,
    MasterPlanAmendment,
    MemoryItem,
    MemoryKind,
    ParameterAmendment,
    ParameterClass,
    ParameterSpec,
    PlanHandoff,
    Project,
    TaskContextUpdate,
    TaskContract,
    TaskEnvelope,
    TaskRecord,
    TaskSource,
    TaskStatus,
)
from pistachio.services.amendments import (
    AMENDMENT_FAILURE_MESSAGE,
    AmendmentNeedsInput,
    apply_plan_amendment,
)

QUESTION = (
    "Use a reproducible uniform sample of 5,000 cells to estimate the mean Silhouette score "
    "when selecting the Louvain resolution? All cells will still be included in clustering."
)


def handoff() -> PlanHandoff:
    plan = MasterPlan(
        task_id="task", revision=3,
        contract=TaskContract(
            objective="Cluster all cells and select the resolution by Silhouette score.",
            requested_outputs=["Spatial plot"],
            prohibited_extensions=["Do not filter cells from clustering."],
        ),
        steps=[AnalysisStep(id="cluster", title="Cluster all cells", description="Use all cells.")],
        parameters=[ParameterSpec(
            id="seed", label="Random seed", component_id="cluster",
            classification=ParameterClass.SCIENTIFIC, proposed_value=0, requires_approval=True,
        )],
    )
    return PlanHandoff(
        master_plan=plan, contract=plan.contract.model_copy(deep=True),
        decision_set=DecisionSet(
            task_id="task", master_plan_id=plan.id, master_plan_revision=3,
            values={"seed": 42}, approved_by="alice",
        ),
        pending_question=QUESTION,
        conversation=[ConversationMessage(event_key="question", text=QUESTION, kind="input")],
        followups=[TaskContextUpdate(
            message="That’s fine", principal_id="alice", source=TaskSource.SLACK,
            external_event_id="consent",
        )],
    )


def sample_edit(value=5000, *, evidence=QUESTION, basis="accepted_proposal"):
    return MasterPlanAmendment(
        parameters=[ParameterAmendment(
            parameter=ParameterSpec(
                id="silhouette_sample_size", label="Silhouette sample size",
                component_id="cluster", classification=ParameterClass.SCIENTIFIC,
                proposed_value=value, confirmed_by_user=True,
            ),
            evidence=evidence, basis=basis,
        )],
        response="I’ll use the confirmed 5,000-cell sample for scoring and cluster all cells.",
        amendment_explanation="I’ll use the confirmed 5,000-cell sample for scoring.",
    )


def test_acceptance_changes_only_the_proposal_and_keeps_effective_approved_values():
    baseline = handoff()
    original = baseline.model_dump_json()
    plan = apply_plan_amendment(baseline, sample_edit())
    assert plan.id != baseline.master_plan.id and plan.revision == 4
    assert plan.contract == baseline.contract and plan.steps == baseline.master_plan.steps
    assert [(p.id, p.proposed_value) for p in plan.parameters] == [
        ("seed", 42), ("silhouette_sample_size", 5000),
    ]
    assert all(p.confirmed_by_user for p in plan.parameters)
    assert baseline.model_dump_json() == original


@pytest.mark.parametrize("value", [10000, "10,000", "10k", [10000]])
def test_acceptance_cannot_change_5000_to_10000(value):
    with pytest.raises(ValueError, match="does not match the quoted proposal"):
        apply_plan_amendment(handoff(), sample_edit(value))


def test_confirmation_without_edits_preserves_plan_and_user_edited_values():
    baseline = handoff()
    plan = apply_plan_amendment(baseline, MasterPlanAmendment(
        response="I’ll continue.", amendment_explanation="I’ll keep the confirmed settings.",
    ))
    assert plan.parameters[0].proposed_value == 42
    assert plan.contract == baseline.contract and plan.steps == baseline.master_plan.steps


def test_explicit_later_instruction_can_change_the_value():
    baseline = handoff()
    baseline.master_plan = apply_plan_amendment(baseline, sample_edit())
    baseline.decision_set = DecisionSet(
        task_id="task", master_plan_id=baseline.master_plan.id, master_plan_revision=4,
        values={"seed": 42, "silhouette_sample_size": 5000}, approved_by="alice",
    )
    baseline.followups.append(TaskContextUpdate(
        message="Actually use 10k cells for scoring.", principal_id="alice",
        source=TaskSource.SLACK, external_event_id="change",
    ))
    plan = apply_plan_amendment(baseline, sample_edit(
        10000, evidence="Actually use 10k cells for scoring.", basis="user_request",
    ))
    assert plan.parameters[-1].proposed_value == 10000
    assert plan.parameters[0].proposed_value == 42


def test_invented_confirmation_evidence_is_rejected():
    with pytest.raises(ValueError, match="reference the relevant saved"):
        apply_plan_amendment(handoff(), sample_edit(10000, evidence="Use 10,000 cells."))


@pytest.mark.parametrize("message", [
    "10,000 cells is fine, uniform sampling works and seed can be chosen by you it doesnt matter",
    "10k, uniform, pick the seed",
    "- cells: 10_000\n- scheme: uniform\n- seed: your choice",
    '{"cells": 10000, "scheme": "uniform", "seed": "choose for me"}',
    "ten thousand cells; uniform; choose a seed",
    "１０，０００ cells, uniform, choose a seed",
    "10 000 cells, uniform, choose a seed",
])
def test_freeform_followup_uses_saved_sources_and_scoped_delegation(message):
    baseline = handoff()
    # Reproduce the second incident: the original proposal had no sample count, and a later
    # clarification replaced pending_question. Both turns must remain usable as authority.
    proposal = "Use reproducible sampled Silhouette estimates? Clustering still uses all cells."
    baseline.conversation[0].text = proposal
    baseline.pending_question = "What sample size, sampling scheme, and random seed should be used?"
    reply = TaskContextUpdate(
        message=message, principal_id="alice", source=TaskSource.SLACK,
        external_event_id="details",
    )
    baseline.followups.append(reply)
    amendment = sample_edit(10000, evidence="Paraphrasing is not authority", basis="user_request")
    amendment.parameters[0].evidence_refs = [reply.id]
    amendment.parameters.extend([
        ParameterAmendment(
            parameter=ParameterSpec(
                id="sampling", label="Sampling scheme", component_id="cluster",
                classification=ParameterClass.SCIENTIFIC, proposed_value="uniform",
                confirmed_by_user=True,
            ), evidence_refs=[reply.id], basis="user_request",
        ),
        ParameterAmendment(
            parameter=ParameterSpec(
                id="silhouette_seed", label="Silhouette seed", component_id="cluster",
                classification=ParameterClass.SCIENTIFIC, proposed_value=42,
                requires_approval=True, confirmed_by_user=True,
            ), evidence_refs=[reply.id], basis="delegated_choice",
        ),
        ParameterAmendment(
            parameter=ParameterSpec(
                id="silhouette_evaluation", label="Silhouette evaluation", component_id="cluster",
                classification=ParameterClass.SCIENTIFIC, proposed_value="sampled",
                confirmed_by_user=True,
            ), evidence_refs=[baseline.conversation[0].id, baseline.followups[0].id],
            basis="accepted_proposal",
        ),
    ])
    before = amendment.model_dump_json()
    plan = apply_plan_amendment(baseline, amendment)
    parameters = {item.id: item for item in plan.parameters}
    assert parameters["silhouette_sample_size"].proposed_value == 10000
    assert parameters["silhouette_sample_size"].source.endswith(message)
    assert parameters["sampling"].proposed_value == "uniform"
    assert parameters["silhouette_seed"].proposed_value == 42
    assert not parameters["silhouette_seed"].confirmed_by_user
    assert not parameters["silhouette_seed"].requires_approval
    assert parameters["silhouette_seed"].decision_scope == DecisionScope.IMPLEMENTATION
    assert parameters["seed"].proposed_value == 42
    assert proposal in parameters["silhouette_evaluation"].source
    assert plan.contract == baseline.contract and plan.steps == baseline.master_plan.steps
    assert amendment.model_dump_json() == before


def test_saved_references_still_reject_numeric_drift_and_invented_sources():
    baseline = handoff()
    amendment = sample_edit(10000)
    amendment.parameters[0].evidence_refs = [
        baseline.conversation[0].id, baseline.followups[0].id,
    ]
    with pytest.raises(ValueError, match="does not match"):
        apply_plan_amendment(baseline, amendment)
    amendment.parameters[0].evidence_refs = ["invented-message-id"]
    with pytest.raises(ValueError, match="existing message IDs"):
        apply_plan_amendment(baseline, amendment)
    amendment.parameters[0].evidence_refs = [baseline.conversation[0].id]
    with pytest.raises(ValueError, match="relevant saved"):
        apply_plan_amendment(baseline, amendment)


def test_routine_choices_need_no_numeric_input_but_cannot_change_an_approved_value():
    baseline = handoff()
    amendment = sample_edit(42, basis="automatic_choice")
    spec = amendment.parameters[0].parameter
    spec.id = "silhouette_seed"
    spec.decision_scope = DecisionScope.METHOD_DEFAULT
    amendment.parameters[0].evidence_refs = [baseline.master_plan.id]
    plan = apply_plan_amendment(baseline, amendment)
    assert plan.parameters[-1].proposed_value == 42
    assert not plan.parameters[-1].confirmed_by_user
    assert not plan.parameters[-1].requires_approval
    spec.id, spec.proposed_value = "seed", 99
    with pytest.raises(ValueError, match="cannot override approved values"):
        apply_plan_amendment(baseline, amendment)
    spec.id = "new_scientific_choice"
    spec.decision_scope = DecisionScope.RESEARCH_DESIGN
    with pytest.raises(ValueError, match="research choices"):
        apply_plan_amendment(baseline, amendment)


async def test_internal_evidence_questions_are_repaired_then_fail_without_user_formatting_request(
    repository, settings, monkeypatch,
):
    settings = settings.model_copy(update={"planner_version": 2})
    real_harness = AgentHarness(repository, settings)
    calls = []
    baseline = handoff()

    async def run(agent, prompt, context, **kwargs):
        calls.append(prompt)
        return SimpleNamespace(final_output=MasterPlanAmendment(
            clarification_question="Please quote the actual exchange as amendment evidence.",
            response="", amendment_explanation="",
        )), {"model_calls": 1}

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(real_harness, "_run_bounded", run)

    class Harness(StagedHarness):
        async def create_master_plan(self, **kwargs):
            return await real_harness.create_master_plan(**kwargs)

    service = task_service(repository, settings, Harness())
    project = await repository.create_project(Project(
        slug="internal", name="Internal", owner="alice",
    ))
    task = await repository.create_task(TaskRecord(
        envelope=TaskEnvelope(
            project_id=project.id, conversation_id="c", principal_id="alice", prompt="That’s fine",
        ), plan_handoff=baseline, planner_version=2,
    ))
    revised = await service.process(task.id)
    assert len(calls) == 2
    assert "Previous output" in calls[1] and baseline.followups[0].id in calls[1]
    assert revised.status == TaskStatus.FAILED
    assert revised.error == AMENDMENT_FAILURE_MESSAGE
    assert revised.plan_handoff == baseline
    assert revised.master_plan is None and revised.decision_set is None
    assert any("Resolve internal evidence" in stage.error for stage in revised.stage_summaries)


async def test_ambiguous_reply_pauses_without_replacing_authority(repository, settings):
    settings = settings.model_copy(update={"planner_version": 2})
    question = "Do you mean the 5,000-cell or 10,000-cell proposal?"

    class Harness(StagedHarness):
        async def create_master_plan(self, **kwargs):
            self.master_calls += 1
            return apply_plan_amendment(kwargs["context"].plan_handoff, MasterPlanAmendment(
                clarification_question=question, response=question, amendment_explanation="",
            )), {}

    harness = Harness()
    service = task_service(repository, settings, harness)
    project = await repository.create_project(Project(
        slug="ambiguous", name="Ambiguous", owner="alice",
    ))
    baseline = handoff()
    task = await repository.create_task(TaskRecord(
        envelope=TaskEnvelope(
            project_id=project.id, conversation_id="c", principal_id="alice", prompt="That’s fine",
        ), plan_handoff=baseline, planner_version=2,
    ))
    revised = await service.process(task.id)
    assert revised.status == TaskStatus.NEEDS_INPUT and revised.error == question
    assert revised.master_plan is None and revised.decision_set is None
    assert revised.plan_handoff == baseline
    await service.process(task.id)
    assert harness.master_calls == 1
    revised, _ = await service.add_context(
        task.id, "alice", "5,000 cells.", source=TaskSource.SLACK, external_event_id="clarified",
    )
    assert revised.plan_handoff.pending_question == question
    assert revised.plan_handoff.followups[-1].message == "5,000 cells."
    assert not revised.stage_summaries


def test_clarification_never_applies_even_a_valid_parameter_edit():
    amendment = sample_edit()
    amendment.clarification_question = "Which proposal do you mean?"
    with pytest.raises(AmendmentNeedsInput):
        apply_plan_amendment(handoff(), amendment)


@pytest.mark.parametrize("protocol", [1, 2])
async def test_screenshot_exchange_survives_restart_and_repairs_drift(
    repository, settings, monkeypatch, protocol,
):
    settings = settings.model_copy(update={
        "planner_version": 2, "builder_protocol_version": protocol,
    })
    project = await repository.create_project(Project(
        slug="continuity", name="Continuity", owner="alice",
    ))
    real_harness = AgentHarness(repository, settings)
    calls = []

    async def run(agent, prompt, context, **kwargs):
        assert agent is real_harness.plan_amender
        assert QUESTION in prompt and "That’s fine" in prompt
        assert '"seed": 42' in prompt
        calls.append(prompt)
        # Reproduce the reported drift; the first result must not become authority or a reply.
        return SimpleNamespace(final_output=sample_edit(10000 if len(calls) == 1 else 5000)), {
            "model_calls": 1,
        }

    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    monkeypatch.setattr(real_harness, "_run_bounded", run)

    class Harness(StagedHarness):
        async def structure_user_request(self, envelope):
            raise AssertionError("Do not re-interpret bare assent without its conversation")

        async def create_master_plan(self, **kwargs):
            self.master_calls += 1
            return await real_harness.create_master_plan(**kwargs)

    harness = Harness()
    service = task_service(repository, settings, harness)
    task, _ = await service.create(TaskEnvelope(
        project_id=project.id, conversation_id="conversation", principal_id="alice",
        prompt="Cluster all cells and select the resolution by Silhouette score.",
    ))
    baseline = handoff()
    task.master_plan = baseline.master_plan
    task.decision_set = baseline.decision_set
    task.contract = baseline.contract
    task.error = QUESTION
    task.status = TaskStatus.NEEDS_INPUT
    task.conversation.extend(baseline.conversation)
    await repository.update_task(task)
    revised, added = await service.add_context(
        task.id, "alice", "That’s fine", source=TaskSource.SLACK, external_event_id="consent",
    )
    assert added and revised.master_plan is None
    assert revised.plan_handoff.master_plan == baseline.master_plan
    assert revised.plan_handoff.pending_question == QUESTION
    # Reload through serialized persistence and a different service process.
    await repository.update_task(TaskRecord.model_validate_json(revised.model_dump_json()))
    service = task_service(repository, settings, harness)
    revised = await service.process(task.id)
    assert revised.status == TaskStatus.SUCCEEDED, revised.error
    assert len(calls) == 2
    assert "Internal validation rejected" in calls[1]
    assert revised.master_plan.revision == 4
    assert revised.decision_set.values == {"seed": 42, "silhouette_sample_size": 5000}
    assert not revised.approvals
    assert "5,000" in revised.followup_response
    assert all("10,000" not in item.text for item in revised.conversation)
    assert any(item.kind == "task_handoff" for item in revised.context_bundle.manifest)


async def test_consecutive_followups_retain_the_original_question_and_baseline(
    repository, settings,
):
    project = await repository.create_project(Project(slug="double", name="Double", owner="alice"))
    service = task_service(repository, settings, StagedHarness())
    task, _ = await service.create(TaskEnvelope(
        project_id=project.id, conversation_id="c", principal_id="alice", prompt="Cluster cells.",
    ))
    baseline = handoff()
    task.master_plan, task.decision_set = baseline.master_plan, baseline.decision_set
    task.contract, task.error = baseline.contract, QUESTION
    task.status = TaskStatus.NEEDS_INPUT
    await repository.update_task(task)
    await service.add_context(
        task.id, "alice", "That’s fine", source=TaskSource.SLACK, external_event_id="first",
    )
    revised, _ = await service.add_context(
        task.id, "alice", "Keep all cells in the plot too.", source=TaskSource.WEB,
        external_event_id="second",
    )
    assert revised.plan_handoff.master_plan == baseline.master_plan
    assert revised.plan_handoff.pending_question == QUESTION
    assert [item.message for item in revised.plan_handoff.followups] == [
        "That’s fine", "Keep all cells in the plot too.",
    ]


async def test_handoff_is_protected_when_advisory_memory_exceeds_budget(repository, settings):
    project = await repository.create_project(Project(slug="budget", name="Budget", owner="alice"))
    service = task_service(repository, settings, StagedHarness())
    task = TaskRecord(
        envelope=TaskEnvelope(
            project_id=project.id, conversation_id="c", principal_id="alice",
            prompt="Sample cells. That’s fine.",
        ), plan_handoff=handoff(), interaction_revision=1,
    )
    protected = await service.context_builder.build(
        project=project, task=task, contract=task.plan_handoff.contract,
    )
    service.context_builder.settings = settings.model_copy(update={
        "context_token_budget": protected.estimated_tokens + 100,
    })
    memory = await service.memory_manager.promote(MemoryItem(
        project_id=project.id, kind=MemoryKind.USER_PREFERENCE,
        content="Sample cells. " * 500, provenance="earlier task",
    ))
    context = await service.context_builder.build(
        project=project, task=task, contract=task.plan_handoff.contract,
    )
    assert memory.id not in {item.id for item in context.memories}
    assert QUESTION in context.rendered and "That’s fine" in context.rendered
    assert context.plan_handoff.decision_set.values["seed"] == 42
    assert any(item.kind == "task_handoff" for item in context.manifest)


async def test_conversation_agent_receives_question_and_effective_values(monkeypatch, settings):
    import json

    from pistachio.connectors.presentation import TaskConversationAgent, UserFacingMessage

    baseline = handoff()
    plan = apply_plan_amendment(baseline, sample_edit())
    task = TaskRecord(
        envelope=TaskEnvelope(
            project_id="project", conversation_id="c", principal_id="alice",
            prompt="Cluster cells.",
        ), master_plan=plan, plan_handoff=baseline, context_updates=baseline.followups,
    )
    agent = TaskConversationAgent(settings)
    agent.agent = object()

    async def run(_agent, prompt, **kwargs):
        payload = json.loads(prompt)
        assert payload["pending_question"] == QUESTION
        assert payload["latest_followup"] == "That’s fine"
        assert {item["label"]: item["value"] for item in payload["plan_settings"]} == {
            "Random seed": "42", "Silhouette sample size": "5000",
        }
        return SimpleNamespace(final_output=UserFacingMessage(text=plan.amendment_explanation))

    monkeypatch.setattr("pistachio.connectors.presentation.Runner.run", run)
    assert await agent.render_replan(task) == plan.amendment_explanation
