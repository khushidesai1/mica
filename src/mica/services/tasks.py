"""Task orchestration across planning, approval, execution, and memory."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import re
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel

from mica.agents.context import ContextBuilder
from mica.agents.harness import AgentHarness
from mica.agents.memory import MemoryManager, MemoryPolicyViolation, ProjectCompactor
from mica.agents.session import session_key
from mica.config import Settings
from mica.connectors.notifications import TaskNotifier
from mica.connectors.presentation import TaskConversationAgent
from mica.execution.backends import BatchStatusBackend, ExecutionBackend
from mica.execution.validation import ScriptRejected, validate_script
from mica.models import (
    ApprovalDecision,
    ApprovalKind,
    ApprovalRequest,
    ApprovalStatus,
    ConversationMessage,
    DecisionScope,
    DecisionSet,
    ExecutionBundle,
    FigurePlan,
    MasterPlan,
    MemoryStatus,
    ParameterClass,
    PlanHandoff,
    ProgressStatus,
    Project,
    ProjectStatus,
    StageKind,
    StageStatus,
    StructuredUserRequest,
    TaskContextUpdate,
    TaskContract,
    TaskEnvelope,
    TaskProgress,
    TaskRecord,
    TaskResult,
    TaskSource,
    TaskStage,
    TaskStatus,
    ToolInvocation,
    ToolRecord,
    ToolStatus,
    new_id,
    utcnow,
)
from mica.services.amendments import (
    AMENDMENT_FAILURE_MESSAGE,
    AmendmentNeedsInput,
    AmendmentRepairExhausted,
)
from mica.services.conversation import communication_kind, communication_phase
from mica.services.datasets import DatasetInspector
from mica.services.development import DevelopmentConflict, DevelopmentService
from mica.services.planning import (
    deterministic_resource_plan,
    effective_parameter_manifest,
    resolve_registered_tools,
)
from mica.services.policy import ExecutionPolicy
from mica.services.projects import ProjectAccessDenied, ProjectNotFound
from mica.services.snapshots import SnapshotStore
from mica.services.tools import ToolBuildService
from mica.services.uploads import attachment_tag
from mica.storage.base import Repository


class TaskNotFound(LookupError):
    pass


class TaskInteractionConflict(ValueError):
    pass


class TaskInteractionSuperseded(RuntimeError):
    """An in-flight worker observed a newer user interaction and should stop."""


class ExecutionSubmissionPending(RuntimeError):
    """A reserved cloud execution attempt could not yet be submitted."""


class TaskService:
    MAX_EXECUTION_ATTEMPTS = 2
    MAX_SUBMISSION_ATTEMPTS = 3
    EXECUTION_DIAGNOSTIC_LIMIT = 12_000

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        harness: AgentHarness,
        context_builder: ContextBuilder,
        memory_manager: MemoryManager,
        compactor: ProjectCompactor,
        executor: ExecutionBackend,
        notifier: TaskNotifier | None = None,
        dataset_inspector: DatasetInspector | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.harness = harness
        self.context_builder = context_builder
        self.memory_manager = memory_manager
        self.compactor = compactor
        self.executor = executor
        self.notifier = notifier
        self.conversation_agent = TaskConversationAgent(settings)
        self.dataset_inspector = dataset_inspector
        self.development_service = DevelopmentService(
            repository, settings, harness, executor, self._record_progress
        )
        self.policy = ExecutionPolicy(settings)
        self.snapshots = SnapshotStore(settings)
        self.tool_builds = ToolBuildService(repository, settings)
        self._locks: dict[str, asyncio.Lock] = {}
        self._maintenance: set[asyncio.Task[object]] = set()

    async def create(self, envelope: TaskEnvelope) -> tuple[TaskRecord, bool]:
        project = await self.repository.get_project(envelope.project_id)
        if project is None:
            raise ProjectNotFound(envelope.project_id)
        if project.status != ProjectStatus.ACTIVE or not project.can_access(envelope.principal_id):
            raise ProjectAccessDenied(envelope.project_id)
        self._validate_attachments(envelope)
        if not await self.repository.mark_event(envelope.external_event_id):
            tasks = await self.repository.list_tasks(envelope.project_id, limit=100)
            duplicate = next(
                (
                    task
                    for task in tasks
                    if task.envelope.external_event_id == envelope.external_event_id
                ),
                None,
            )
            if duplicate:
                return duplicate, False
            raise ValueError("event was already processed")
        task = await self.repository.create_task(
            TaskRecord(
                envelope=envelope,
                conversation=[
                    ConversationMessage(
                        event_key="request",
                        role="user",
                        text=envelope.prompt,
                        kind="request",
                        source=envelope.source,
                    )
                ],
                planner_version=self.settings.planner_version,
                builder_protocol_version=self.settings.builder_protocol_version,
            )
        )
        project.last_active_at = utcnow()
        project.updated_at = utcnow()
        await self.repository.update_project(project)
        await self._notify(task)
        return task, True

    def _validate_attachments(self, envelope: TaskEnvelope) -> None:
        local_root = (self.settings.artifact_root / "uploads").resolve()
        filenames: set[str] = set()
        for attachment in envelope.attachments:
            if attachment.filename in filenames:
                raise ValueError("attachment filenames must be unique within a task")
            filenames.add(attachment.filename)
            if attachment.quarantined or not attachment.checksum_sha256:
                raise ValueError("attachments must be scanned and checksummed before task intake")
            expected_tag = attachment_tag(
                self.settings,
                envelope.principal_id,
                attachment.uri,
                attachment.checksum_sha256,
                attachment.generation,
            )
            if not hmac.compare_digest(attachment.authorization_tag, expected_tag):
                raise ValueError("attachment authorization is invalid for this principal")
            if attachment.uri.startswith("gs://"):
                prefix = f"gs://{self.settings.gcs_bucket}/inputs/"
                if not self.settings.gcs_bucket or not attachment.uri.startswith(prefix):
                    raise ValueError("GCS attachment is outside the managed input bucket")
                if not attachment.generation:
                    raise ValueError("GCS attachment must pin an immutable generation")
            elif attachment.uri.startswith("file://") and self.settings.env in {"local", "test"}:
                candidate = Path(attachment.uri.removeprefix("file://")).resolve()
                if not candidate.is_relative_to(local_root):
                    raise ValueError("local attachment is outside the managed upload directory")
            else:
                raise ValueError("unsupported or untrusted attachment URI")

    @staticmethod
    def _missing_execution_inputs(task: TaskRecord) -> list[str]:
        available = {item.uri for item in task.envelope.attachments}
        available.update(
            item.uri
            for item in task.dataset_inspections
            if not item.inspection_error and item.generation
        )
        required = {
            value
            for value in (task.contract.immutable_inputs if task.contract else [])
            if value.startswith("gs://") or value.startswith("file://")
        }
        missing = sorted(required - available)
        script_reads_h5ad = bool(
            re.search(
                r"input_dir\s*\.\s*(?:r?glob)\s*\(\s*['\"][^'\"]*\.h5ad['\"]",
                task.generated_script,
                re.IGNORECASE,
            )
        )
        if script_reads_h5ad and not available and not missing:
            missing.append("an attached or generation-pinned .h5ad dataset")
        return missing

    async def require_access(self, task_id: str, principal: str) -> TaskRecord:
        task = await self.repository.get_task(task_id)
        if task is None:
            raise TaskNotFound(task_id)
        project = await self.repository.get_project(task.envelope.project_id)
        if project is None:
            raise ProjectNotFound(task.envelope.project_id)
        if not project.can_access(principal):
            raise ProjectAccessDenied(task.envelope.project_id)
        return task

    async def process(self, task_id: str) -> TaskRecord:
        # Cloud Tasks and the periodic reconciler can run on different API instances.
        # Serialize the whole planning continuation, not just calls into development.
        lease = await self.repository.begin_event(f"process:{task_id}", lease_seconds=1200)
        if lease.status != "claimed":
            current = await self.repository.get_task(task_id)
            if current is None:
                raise TaskNotFound(task_id)
            return current
        try:
            return await self._process(task_id)
        finally:
            await self.repository.release_event(f"process:{task_id}", lease.token)

    async def _process(self, task_id: str) -> TaskRecord:
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = await self.repository.get_task(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task.status == TaskStatus.NEEDS_INPUT and any(
                stage.kind == StageKind.MASTER_PLAN and stage.status == StageStatus.WAITING
                for stage in task.stage_summaries
            ):
                return task
            if task.builder_protocol_version == 2 and task.development:
                if task.status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                    return task
                if any(a.status == ApprovalStatus.PENDING for a in task.approvals):
                    return task
                # An amendment retains the workspace but invalidates its planning authority.
                # Finish the planner graph before rebinding/resuming that workspace.
                if (
                    task.development.interaction_revision == task.interaction_revision
                    and task.contract
                    and task.master_plan
                    and task.decision_set
                    and task.tool_plan
                    and not task.pending_tool_builds
                    and task.resource_plan
                    and any(
                        s.kind == StageKind.FIGURE_PLAN and s.status == StageStatus.COMPLETED
                        for s in task.stage_summaries
                    )
                ):
                    return await self._advance_development(task)
            if task.status == TaskStatus.SUBMITTING:
                task = await self._execute_or_complete(task)
                await self._notify(task)
                return task
            if task.status in {
                TaskStatus.CANCELLED,
                TaskStatus.QUEUED,
                TaskStatus.RUNNING,
                TaskStatus.SUCCEEDED,
            }:
                return task
            project = await self.repository.get_project(task.envelope.project_id)
            if project is None:
                raise ProjectNotFound(task.envelope.project_id)
            if task.structured_request is None:
                structure_request = getattr(self.harness, "structure_user_request", None)
                task.structured_request = (
                    await structure_request(task.envelope)
                    if structure_request is not None and task.plan_handoff is None
                    else StructuredUserRequest(
                        objective=task.envelope.prompt,
                        original_message=task.envelope.prompt,
                    )
                )
                task.updated_at = utcnow()
                task = await self.repository.update_task(task)
                await self._assert_interaction_current(task)
            if task.planner_version >= 2:
                try:
                    return await self._process_v2(task, project)
                except TaskInteractionSuperseded:
                    current = await self.repository.get_task(task.id)
                    return current or task
                except AmendmentNeedsInput as exc:
                    current = await self.repository.get_task(task.id)
                    if current is not None and (
                        current.interaction_revision != task.interaction_revision
                        or current.status == TaskStatus.CANCELLED
                    ):
                        return current
                    task.status = TaskStatus.NEEDS_INPUT
                    task.error = str(exc)
                    for stage in task.stage_summaries:
                        if stage.status == StageStatus.RUNNING:
                            stage.status = StageStatus.WAITING
                    task.updated_at = utcnow()
                    task = await self.repository.update_task(task)
                    await self._notify(task)
                    return task
                except ExecutionSubmissionPending:
                    raise
                except Exception as exc:
                    active_stage = next(
                        (
                            item
                            for item in reversed(task.stage_summaries)
                            if item.status == StageStatus.RUNNING
                        ),
                        None,
                    )
                    if active_stage is not None:
                        active_stage.status = StageStatus.FAILED
                        active_stage.error = f"{type(exc).__name__}: {exc}"
                        active_stage.completed_at = utcnow()
                        active_stage.metrics["outcome"] = (
                            "timeout" if isinstance(exc, TimeoutError) else "failed"
                        )
                    task.status = TaskStatus.FAILED
                    task.error = (
                        AMENDMENT_FAILURE_MESSAGE if isinstance(exc, AmendmentRepairExhausted)
                        else f"{type(exc).__name__}: {exc}"
                    )
                    task.updated_at = utcnow()
                    task = await self.repository.update_task(task)
                    await self._notify(task)
                    return task
            try:
                prior_approvals = list(task.approvals)
                task.status = TaskStatus.PLANNING
                task.updated_at = utcnow()
                await self.repository.update_task(task)
                await self._notify_progress(task)
                if self.dataset_inspector is not None and not task.dataset_inspections:
                    task.dataset_inspections = await asyncio.to_thread(
                        self.dataset_inspector.inspect_prompt, task.envelope.prompt
                    )
                    await self.repository.update_task(task)
                approved_decisions = [
                    approval
                    for approval in task.approvals
                    if approval.kind in {ApprovalKind.SCIENTIFIC_STEP, ApprovalKind.HYPERPARAMETER}
                    and approval.status == ApprovalStatus.APPROVED
                ]
                provisional = (
                    task.contract
                    if task.contract
                    and (approved_decisions or task.contract.authorized_scientific_actions)
                    else None
                )
                if provisional is None:
                    provisional = TaskContract(
                        objective=task.envelope.prompt,
                        immutable_inputs=[item.uri for item in task.envelope.attachments],
                        prohibited_extensions=[
                            "Any scientific action not explicitly authorized by the current request"
                        ],
                    )
                planning_envelope = task.envelope
                explicit_decisions = [
                    str(approval.payload.get("decision", "")).strip()
                    for approval in approved_decisions
                    if str(approval.payload.get("decision", "")).strip()
                ]
                if task.contract:
                    explicit_decisions.extend(task.contract.authorized_scientific_actions)
                explicit_decisions = list(dict.fromkeys(explicit_decisions))
                if explicit_decisions:
                    decisions = "\n".join(f"- {decision}" for decision in explicit_decisions)
                    planning_envelope = task.envelope.model_copy(
                        update={
                            "prompt": (
                                f"{task.envelope.prompt}\n\n"
                                f"Explicit current-task decisions:\n{decisions}"
                            )
                        }
                    )
                plan = None
                generated_approvals: list[ApprovalRequest] = []
                for planning_attempt in range(2):
                    planning_task = task.model_copy(update={"envelope": planning_envelope})
                    planning_context = await self.context_builder.build(
                        project=project,
                        task=planning_task,
                        contract=provisional,
                    )
                    plan = await self.harness.plan(
                        task_id=task.id,
                        envelope=planning_envelope,
                        context=planning_context,
                    )
                    await self._assert_interaction_current(task)
                    plan.resource_plan = self.policy.canonicalize_resource_plan(plan.resource_plan)
                    generated_approvals = self.policy.approvals_for(
                        task.id, plan.contract, plan.resource_plan, plan.tool_plan
                    )
                    planning_issues = self._planning_issues(generated_approvals)
                    if not planning_issues:
                        break
                    if planning_attempt == 0:
                        issue_text = "\n".join(f"- {issue}" for issue in planning_issues)
                        planning_envelope = planning_envelope.model_copy(
                            update={
                                "prompt": (
                                    f"{planning_envelope.prompt}\n\n"
                                    "PLANNING VALIDATION ERROR (repair internally; do not ask the "
                                    "user to approve this):\n"
                                    f"{issue_text}\n"
                                    "Every ID in approved_tool_ids must have exactly one complete "
                                    "ToolInvocation. Add the missing review, or remove the tool ID "
                                    "if the generated script does not use it."
                                )
                            }
                        )
                        provisional = plan.contract
                        continue
                    task.contract = plan.contract
                    task.resource_plan = plan.resource_plan
                    task.tool_plan = plan.tool_plan
                    task.generated_script = plan.script
                    task.plan_response = plan.response
                    task.approvals = self._carry_forward_compute_approvals(
                        prior_approvals,
                        [
                            approval
                            for approval in generated_approvals
                            if approval.kind != ApprovalKind.HYPERPARAMETER
                        ],
                    )
                    task.status = TaskStatus.NEEDS_INPUT
                    task.error = (
                        "Planner could not produce a complete tool review after one automatic "
                        f"repair: {'; '.join(planning_issues)}"
                    )
                    task.updated_at = utcnow()
                    task = await self.repository.update_task(task)
                    await self._notify(task)
                    return task
                if plan is None:
                    raise RuntimeError("planner did not return a plan")
                task.contract = plan.contract
                task.resource_plan = plan.resource_plan
                task.tool_plan = plan.tool_plan
                task.generated_script = plan.script
                task.plan_response = plan.response
                task.context_bundle = await self.context_builder.build(
                    project=project, task=task, contract=plan.contract
                )
                for proposal in plan.tool_plan.proposals:
                    proposal.status = ToolStatus.PROPOSED
                    await self.repository.put_tool(proposal)
                    if proposal.id not in plan.tool_plan.proposed_tool_ids:
                        plan.tool_plan.proposed_tool_ids.append(proposal.id)
                for tool_id in plan.tool_plan.proposed_tool_ids:
                    tool = await self.repository.get_tool(tool_id)
                    if tool is None:
                        raise ValueError(f"tool proposal {tool_id} lacks exact registry metadata")
                    if tool.status == ToolStatus.CATALOGED:
                        tool.status = ToolStatus.PROPOSED
                        tool.updated_at = utcnow()
                        await self.repository.put_tool(tool)
                task.approvals = self._carry_forward_compute_approvals(
                    prior_approvals, generated_approvals
                )
                task.error = None
                for candidate in plan.memory_candidates:
                    candidate = candidate.model_copy(update={
                        "id": new_id("mem"), "owner_id": None, "version": 1,
                        "embedding": [], "status": MemoryStatus.CANDIDATE,
                        "supersedes_id": None, "created_at": utcnow(), "updated_at": utcnow(),
                        "last_used_at": None,
                    })
                    candidate.project_id = project.id
                    candidate.source_task_id = task.id
                    with suppress(MemoryPolicyViolation):
                        await self.memory_manager.promote(candidate)
                if any(approval.status == ApprovalStatus.PENDING for approval in task.approvals):
                    task.status = TaskStatus.NEEDS_APPROVAL
                    task.updated_at = utcnow()
                    task.paused_state_uri = await self.snapshots.save(task)
                    task = await self.repository.update_task(task)
                    await self._notify(task)
                    return task
                task = await self._execute_or_complete(task)
                await self._notify(task)
                return task
            except ExecutionSubmissionPending:
                raise
            except Exception as exc:
                task.status = TaskStatus.FAILED
                task.error = f"{type(exc).__name__}: {exc}"
                task.updated_at = utcnow()
                task = await self.repository.update_task(task)
                await self._notify(task)
                return task

    async def _process_v2(self, task: TaskRecord, project: Project) -> TaskRecord:
        """Resume the immutable planner-v2 graph from the first incomplete stage."""
        task.status = TaskStatus.PLANNING
        task.error = None
        task.updated_at = utcnow()
        await self.repository.update_task(task)

        dataset_hash = self._stable_hash(
            {
                "prompt": task.envelope.prompt,
                "attachments": [item.model_dump(mode="json") for item in task.envelope.attachments],
            }
        )
        if not self._stage_complete(task, StageKind.DATASET_INSPECTION, dataset_hash):
            stage = await self._start_stage(task, StageKind.DATASET_INSPECTION, dataset_hash)
            if self.dataset_inspector is not None and not task.dataset_inspections:
                task.dataset_inspections = await asyncio.to_thread(
                    self.dataset_inspector.inspect_prompt, task.envelope.prompt
                )
            await self._complete_stage(
                task,
                stage,
                task.dataset_inspections,
                {"dataset_count": len(task.dataset_inspections), "model_calls": 0},
            )

        provisional = task.contract or (
            task.plan_handoff.contract if task.plan_handoff else None
        ) or TaskContract(
            objective=task.envelope.prompt,
            immutable_inputs=[item.uri for item in task.envelope.attachments],
            prohibited_extensions=[
                "Any scientific action not explicitly authorized by the current request"
            ],
        )
        context = await self.context_builder.build(project=project, task=task, contract=provisional)
        master_hash = self._stable_hash(
            {
                "planner_version": task.planner_version,
                "request": task.envelope.prompt,
                "attachments": [item.model_dump(mode="json") for item in task.envelope.attachments],
                "datasets": [item.model_dump(mode="json") for item in task.dataset_inspections],
                "context_manifest": [
                    item.model_dump(mode="json")
                    for item in context.manifest
                    if item.kind != "task_contract"
                ],
            }
        )
        if task.master_plan is None:
            stage = await self._start_stage(
                task,
                StageKind.MASTER_PLAN,
                master_hash,
                model=self.settings.master_planner_model,
                reasoning=self.settings.openai_reasoning_effort,
            )
            plan, metrics = await self.harness.create_master_plan(
                task_id=task.id, envelope=task.envelope, context=context
            )
            plan.task_id = task.id
            plan.input_hash = master_hash
            plan.model = self.settings.master_planner_model
            plan = self._enforce_parameter_policy(plan)
            task.master_plan = plan
            task.master_plan_id = plan.id
            task.contract = plan.contract
            task.plan_response = plan.response
            task.context_bundle = await self.context_builder.build(
                project=project, task=task, contract=plan.contract
            )
            await self._complete_stage(task, stage, plan, metrics, output_ref=plan.id)
        elif task.master_plan.input_hash != master_hash:
            raise ValueError(
                "Task inputs changed after the master plan was created; create a new plan revision."
            )

        master = task.master_plan
        if master is None:
            raise RuntimeError("master plan was not persisted")
        if task.context_updates and not task.followup_response:
            task.followup_response = await self.conversation_agent.render_replan(task)
            await self._assert_interaction_current(task)
            task = await self.repository.update_task(task)
            await self._notify(task, event_key=f"replan:{task.interaction_revision}:{master.id}")
        if task.decision_set is None:
            approvals = self.policy.approvals_for_master_plan(task.id, master)
            if approvals:
                task.approvals = self._merge_approvals(task.approvals, approvals)
                await self._set_waiting_stage(
                    task,
                    StageKind.USER_DECISIONS,
                    self._stable_hash({"master_plan": master.id, "revision": master.revision}),
                )
                task.status = TaskStatus.NEEDS_APPROVAL
                task.paused_state_uri = await self.snapshots.save(task)
                task.updated_at = utcnow()
                task = await self.repository.update_task(task)
                await self._notify(task)
                return task
            task.decision_set = DecisionSet(
                task_id=task.id,
                master_plan_id=master.id,
                master_plan_revision=master.revision,
                values={item.id: item.proposed_value for item in master.parameters},
                approved_by="system:no-approval-required",
                approved_at=utcnow(),
            )
            task.decision_set_id = task.decision_set.id
            stage = await self._start_stage(
                task,
                StageKind.USER_DECISIONS,
                self._stable_hash(task.decision_set.model_dump(mode="json")),
            )
            await self._complete_stage(task, stage, task.decision_set, {"model_calls": 0})

        decisions = task.decision_set
        if decisions is None:
            raise RuntimeError("decision set was not persisted")
        downstream_hash = self._stable_hash(
            {
                "master_plan": master.model_dump(mode="json"),
                "decision_set": decisions.model_dump(mode="json"),
            }
        )

        registry = await self.repository.list_tools()
        tool_plan, unknown, _unavailable = resolve_registered_tools(master, decisions, registry)
        tool_hash = self._stable_hash(
            {
                "downstream": downstream_hash,
                "registry": [(item.id, item.version, item.status) for item in registry],
            }
        )
        if unknown:
            stage = await self._start_stage(
                task,
                StageKind.TOOL_RESOLUTION,
                tool_hash,
                model=self.settings.master_planner_model,
                reasoning=self.settings.openai_reasoning_effort,
            )
            scouted, metrics = await self.harness.scout_tools(
                task_id=task.id, envelope=task.envelope, master_plan=master
            )
            for proposal in scouted.proposals:
                proposal.status = ToolStatus.PROPOSED
                await self.repository.put_tool(proposal)
                if proposal.id not in scouted.proposed_tool_ids:
                    scouted.proposed_tool_ids.append(proposal.id)
            tool_plan.proposals.extend(scouted.proposals)
            tool_plan.proposed_tool_ids.extend(scouted.proposed_tool_ids)
            tool_plan.invocations.extend(scouted.invocations)
            await self._complete_stage(task, stage, tool_plan, metrics)
            unresolved = [
                item
                for item in unknown
                if not any(
                    item.lower() in {proposal.id.lower(), proposal.name.lower()}
                    for proposal in scouted.proposals
                )
            ]
            if unresolved:
                task.tool_plan = tool_plan
                task.status = TaskStatus.FAILED
                task.error = (
                    "Technical dependency resolution found no usable version for: "
                    + ", ".join(unresolved)
                )
                task = await self.repository.update_task(task)
                await self._notify(task)
                return task
        elif not self._stage_complete(task, StageKind.TOOL_RESOLUTION, tool_hash):
            stage = await self._start_stage(task, StageKind.TOOL_RESOLUTION, tool_hash)
            await self._complete_stage(task, stage, tool_plan, {"model_calls": 0})

        # Exact package/version provisioning is technical work, not a scientific decision.
        # Availability is still verified by the controlled build path before execution.
        task.tool_plan = tool_plan
        task.pending_tool_builds = []
        for tool_id in tool_plan.proposed_tool_ids:
            tool = await self.repository.get_tool(tool_id)
            if tool is None:
                raise RuntimeError(f"Technical dependency resolution failed for {tool_id}")
            if tool.status == ToolStatus.AVAILABLE:
                continue
            if tool.status == ToolStatus.FAILED:
                raise RuntimeError(f"Package build failed for {tool.name} {tool.version}")
            if tool.status != ToolStatus.BUILDING:
                tool.status = ToolStatus.APPROVED
                await self.repository.put_tool(tool)
                if not await self.tool_builds.start(tool):
                    raise RuntimeError(
                        f"Cannot prepare {tool.name} {tool.version}: package build infrastructure "
                        "is not configured. This is a technical failure, not a scientific decision."
                    )
            task.pending_tool_builds.append(tool.id)
        if task.pending_tool_builds:
            task.status = TaskStatus.PLANNING
            task.current_stage = StageKind.TOOL_RESOLUTION
            task.error = None
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task

        resolved_records = [
            item
            for item in registry
            if item.id in set(tool_plan.approved_tool_ids + tool_plan.proposed_tool_ids)
        ]
        resource_hash = self._stable_hash(
            {
                "downstream": downstream_hash,
                "datasets": [item.model_dump(mode="json") for item in task.dataset_inspections],
                "tools": [(item.id, item.version) for item in resolved_records],
            }
        )
        if not self._stage_complete(task, StageKind.RESOURCE_PLAN, resource_hash):
            stage = await self._start_stage(task, StageKind.RESOURCE_PLAN, resource_hash)
            resources = deterministic_resource_plan(
                task.dataset_inspections, resolved_records, master
            )
            task.resource_plan = self.policy.canonicalize_resource_plan(resources)
            if task.builder_protocol_version == 2:
                hours = task.resource_plan.timeout_hours
                multiplier = 2 + (
                    self.settings.development_seconds
                    + self.settings.development_max_operations * 300
                ) / (hours * 3600)
                task.resource_plan.estimated_cost_usd *= multiplier
                task.resource_plan.rationale += (
                    " Includes two full attempts, development checks, and worker startup allowance."
                )
            await self._complete_stage(task, stage, task.resource_plan, {"model_calls": 0})
        if task.resource_plan is None:
            raise RuntimeError("resource plan was not persisted")
        if task.tool_plan is None:
            raise RuntimeError("tool plan was not persisted")

        figure_hash = self._stable_hash(
            {
                "downstream": downstream_hash,
                "intents": [item.model_dump(mode="json") for item in master.figure_intents],
            }
        )
        if not self._stage_complete(task, StageKind.FIGURE_PLAN, figure_hash):
            stage = await self._start_stage(
                task,
                StageKind.FIGURE_PLAN,
                figure_hash,
                model=self.settings.worker_model,
                reasoning=self.settings.worker_reasoning_effort,
            )
            figure_plan, metrics = await self.harness.create_figure_plan(
                task_id=task.id,
                envelope=task.envelope,
                master_plan=master,
                decisions=decisions,
                context=task.context_bundle or context,
            )
            task.figure_specs = figure_plan.figures
            await self._complete_stage(task, stage, figure_plan, metrics)

        if task.builder_protocol_version == 2 and master.contract.requires_execution:
            compute = self.policy.compute_approval_for(task.id, task.resource_plan)
            if compute is not None:
                carried = self._carry_forward_compute_approvals(task.approvals, [compute])
                task.approvals = self._merge_approvals(task.approvals, carried)
                if any(item.status == ApprovalStatus.PENDING for item in task.approvals):
                    task.status = TaskStatus.NEEDS_APPROVAL
                    task = await self.repository.update_task(task)
                    await self._notify(task)
                    return task
            return await self._advance_development(task)

        assert task.tool_plan is not None and task.resource_plan is not None
        code_hash = self._stable_hash(
            {
                "downstream": downstream_hash,
                "tools": task.tool_plan.model_dump(mode="json"),
                "resources": task.resource_plan.model_dump(mode="json"),
                "figures": [item.model_dump(mode="json") for item in task.figure_specs],
            }
        )
        if master.contract.requires_execution and not self._stage_complete(
            task, StageKind.CODE_GENERATION, code_hash
        ):
            stage = await self._start_stage(
                task,
                StageKind.CODE_GENERATION,
                code_hash,
                model=self.settings.worker_model,
                reasoning=self.settings.worker_reasoning_effort,
            )
            figure_plan = FigurePlan(figures=task.figure_specs)
            build, metrics = await self.harness.build_code(
                task_id=task.id,
                envelope=task.envelope,
                master_plan=master,
                decisions=decisions,
                tool_plan=task.tool_plan,
                resource_plan=task.resource_plan,
                figure_plan=figure_plan,
                dataset_inspections=[
                    item.model_dump(mode="json") for item in task.dataset_inspections
                ],
            )
            errors: list[str] = []
            for attempt in range(3):
                try:
                    validate_script(build.script, require_preflight=True)
                    self._resolve_runtime_invocations(build.runtime_tool_ids, registry)
                    break
                except ScriptRejected as exc:
                    errors.append(str(exc))
                    if attempt == 2:
                        await self._fail_stage(task, stage, "; ".join(errors))
                        raise
                    build, retry_metrics = await self.harness.build_code(
                        task_id=task.id,
                        envelope=task.envelope,
                        master_plan=master,
                        decisions=decisions,
                        tool_plan=task.tool_plan,
                        resource_plan=task.resource_plan,
                        figure_plan=figure_plan,
                        dataset_inspections=[
                            item.model_dump(mode="json") for item in task.dataset_inspections
                        ],
                        repair_error=str(exc),
                        existing_script=build.script,
                        authoritative_fallback=attempt == 1,
                    )
                    metrics.update(
                        {
                            f"repair_{attempt + 1}_{key}": value
                            for key, value in retry_metrics.items()
                        }
                    )
            task.generated_script = build.script
            task.runtime_tool_ids = build.runtime_tool_ids
            task.plan_response = build.response or master.response
            metrics["attempts"] = len(errors) + 1
            await self._complete_stage(task, stage, build, metrics)

        if task.runtime_tool_ids:
            for invocation in self._resolve_runtime_invocations(task.runtime_tool_ids, registry):
                if invocation.tool_id not in task.tool_plan.approved_tool_ids:
                    task.tool_plan.approved_tool_ids.append(invocation.tool_id)
                if not any(
                    item.tool_id == invocation.tool_id for item in task.tool_plan.invocations
                ):
                    task.tool_plan.invocations.append(invocation)

        if master.contract.requires_execution:
            validation_hash = self._stable_hash(task.generated_script)
            if not self._stage_complete(task, StageKind.VALIDATION, validation_hash):
                stage = await self._start_stage(task, StageKind.VALIDATION, validation_hash)
                validate_script(task.generated_script, require_preflight=True)
                await self._complete_stage(
                    task,
                    stage,
                    {"valid": True, "real_data_preflight_required": True},
                    {"model_calls": 0, "outcome": "static_checks_passed"},
                )

        compute = self.policy.compute_approval_for(task.id, task.resource_plan)
        if compute is not None:
            compute_approvals = self._carry_forward_compute_approvals(task.approvals, [compute])
            task.approvals = self._merge_approvals(task.approvals, compute_approvals)
            if any(item.status == ApprovalStatus.PENDING for item in task.approvals):
                task.status = TaskStatus.NEEDS_APPROVAL
                task.paused_state_uri = await self.snapshots.save(task)
                task.updated_at = utcnow()
                task = await self.repository.update_task(task)
                await self._notify(task)
                return task

        task = await self._execute_or_complete(task)
        await self._notify(task)
        return task

    async def _advance_development(
        self, task: TaskRecord, result: TaskResult | None = None
    ) -> TaskRecord:
        try:
            task = await self.development_service.advance(task, result)
        except DevelopmentConflict:
            if result is not None:
                # A concurrent review owns the lease. Let Pub/Sub redeliver; acknowledging here
                # would discard completion until the five-minute reconciliation sweep.
                raise
            current = await self.repository.get_task(task.id)
            return current or task
        if task.status == TaskStatus.SUCCEEDED:
            await self.memory_manager.promote_task_result(task)
        await self._notify(task)
        return task

    @staticmethod
    def _stable_hash(value: object) -> str:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode()).hexdigest()

    @staticmethod
    def _enforce_parameter_policy(plan: MasterPlan) -> MasterPlan:
        parameters = []
        for parameter in plan.parameters:
            if (
                parameter.decision_scope != DecisionScope.RESEARCH_DESIGN
                or parameter.classification
                in {
                    ParameterClass.PRESENTATION,
                    ParameterClass.TECHNICAL,
                }
            ):
                parameter = parameter.model_copy(update={"requires_approval": False})
            parameters.append(parameter)
        return plan.model_copy(update={"parameters": parameters})

    @staticmethod
    def _resolve_runtime_invocations(
        runtime_tool_ids: list[str], registry: list[ToolRecord]
    ) -> list[ToolInvocation]:
        by_id = {item.id.lower(): item for item in registry}
        by_name = {item.name.lower(): item for item in registry}
        invocations: list[ToolInvocation] = []
        for requested in dict.fromkeys(runtime_tool_ids):
            record = by_id.get(requested.lower()) or by_name.get(requested.lower())
            if record is None:
                raise ScriptRejected(
                    f"code builder selected an unregistered runtime tool: {requested}"
                )
            if record.status != ToolStatus.AVAILABLE:
                raise ScriptRejected(
                    f"code builder selected an unavailable runtime tool: {record.id}"
                )
            invocations.append(
                ToolInvocation(
                    tool_id=record.id,
                    tool_name=record.name,
                    version=record.version,
                    commit_sha=record.commit_sha,
                    entrypoint=record.install_recipe,
                    review_complete=True,
                    review_evidence=[record.provenance, record.source_url],
                    no_tunable_hyperparameters_reason=(
                        "Routine implementation library; primary-procedure parameters are "
                        "recorded in the task's internal execution metadata."
                    ),
                )
            )
        return invocations

    @staticmethod
    def _stage_complete(task: TaskRecord, kind: StageKind, input_hash: str) -> bool:
        return any(
            stage.kind == kind
            and stage.status == StageStatus.COMPLETED
            and stage.input_hash == input_hash
            for stage in task.stage_summaries
        )

    async def _start_stage(
        self,
        task: TaskRecord,
        kind: StageKind,
        input_hash: str,
        *,
        model: str = "deterministic",
        reasoning: str = "none",
    ) -> TaskStage:
        existing = next(
            (
                item
                for item in reversed(task.stage_summaries)
                if item.kind == kind
                and item.input_hash == input_hash
                and item.status in {StageStatus.PENDING, StageStatus.RUNNING, StageStatus.WAITING}
            ),
            None,
        )
        if existing is not None:
            existing.status = StageStatus.RUNNING
            existing.attempts += 1
            existing.started_at = existing.started_at or utcnow()
            existing.error = ""
            stage = existing
        else:
            version = 1 + max(
                (item.version for item in task.stage_summaries if item.kind == kind),
                default=0,
            )
            dependency_map = {
                StageKind.MASTER_PLAN: [StageKind.DATASET_INSPECTION],
                StageKind.USER_DECISIONS: [StageKind.MASTER_PLAN],
                StageKind.TOOL_RESOLUTION: [StageKind.USER_DECISIONS],
                StageKind.RESOURCE_PLAN: [StageKind.TOOL_RESOLUTION],
                StageKind.FIGURE_PLAN: [StageKind.USER_DECISIONS],
                StageKind.CODE_GENERATION: [
                    StageKind.TOOL_RESOLUTION,
                    StageKind.RESOURCE_PLAN,
                    StageKind.FIGURE_PLAN,
                ],
                StageKind.VALIDATION: [StageKind.CODE_GENERATION],
                StageKind.EXECUTION: [StageKind.VALIDATION],
            }
            started_at = utcnow()
            stage = TaskStage(
                kind=kind,
                status=StageStatus.RUNNING,
                version=version,
                depends_on=dependency_map.get(kind, []),
                input_hash=input_hash,
                model=model,
                reasoning_effort=reasoning,
                attempts=1,
                started_at=started_at,
            )
            previous_completed = [
                item.completed_at for item in task.stage_summaries if item.completed_at is not None
            ]
            queue_origin = max(previous_completed) if previous_completed else task.created_at
            stage.metrics["queue_delay_ms"] = max(
                0, round((started_at - queue_origin).total_seconds() * 1000)
            )
            task.stage_summaries.append(stage)
        task.current_stage = kind
        task.updated_at = utcnow()
        saved = await self.repository.update_task(task)
        if saved.interaction_revision != task.interaction_revision:
            raise TaskInteractionSuperseded(task.id)
        await self._notify_progress(task)
        return stage

    async def _complete_stage(
        self,
        task: TaskRecord,
        stage: TaskStage,
        output: object,
        metrics: dict[str, str | int | float | bool],
        *,
        output_ref: str = "",
    ) -> None:
        stage.status = StageStatus.COMPLETED
        stage.output_hash = self._stable_hash(output)
        stage.output_ref = output_ref
        stage.metrics.update(metrics)
        stage.completed_at = utcnow()
        if stage.started_at:
            stage.metrics["duration_ms"] = round(
                (stage.completed_at - stage.started_at).total_seconds() * 1000
            )
        task.current_stage = stage.kind
        task.updated_at = utcnow()
        saved = await self.repository.update_task(task)
        if saved.interaction_revision != task.interaction_revision:
            raise TaskInteractionSuperseded(task.id)

    async def _fail_stage(self, task: TaskRecord, stage: TaskStage, error: str) -> None:
        stage.status = StageStatus.FAILED
        stage.error = error
        stage.completed_at = utcnow()
        task.error = error
        task.updated_at = utcnow()
        await self.repository.update_task(task)

    async def _set_waiting_stage(self, task: TaskRecord, kind: StageKind, input_hash: str) -> None:
        stage = next(
            (
                item
                for item in reversed(task.stage_summaries)
                if item.kind == kind and item.input_hash == input_hash
            ),
            None,
        )
        if stage is None:
            stage = TaskStage(
                kind=kind,
                status=StageStatus.WAITING,
                input_hash=input_hash,
                depends_on=[StageKind.MASTER_PLAN],
                attempts=0,
            )
            task.stage_summaries.append(stage)
        else:
            stage.status = StageStatus.WAITING
        task.current_stage = kind
        await self.repository.update_task(task)

    @staticmethod
    def _merge_approvals(
        prior: list[ApprovalRequest], generated: list[ApprovalRequest]
    ) -> list[ApprovalRequest]:
        merged = list(prior)
        for candidate in generated:
            duplicate = next(
                (
                    item
                    for item in merged
                    if item.kind == candidate.kind and item.payload == candidate.payload
                ),
                None,
            )
            if duplicate is None:
                merged.append(candidate)
        return merged

    @staticmethod
    def _planning_issues(approvals: list[ApprovalRequest]) -> list[str]:
        issues: list[str] = []
        for approval in approvals:
            if approval.kind != ApprovalKind.HYPERPARAMETER:
                continue
            raw = approval.payload.get("planning_issues", [])
            if isinstance(raw, list):
                issues.extend(str(item) for item in raw if str(item).strip())
        return list(dict.fromkeys(issues))

    @staticmethod
    def _carry_forward_compute_approvals(
        prior: list[ApprovalRequest], generated: list[ApprovalRequest]
    ) -> list[ApprovalRequest]:
        """Keep an approval only when the repaired compute payload is byte-for-byte equivalent."""
        carried: list[ApprovalRequest] = []
        for approval in generated:
            if approval.kind != ApprovalKind.COMPUTE:
                carried.append(approval)
                continue
            match = next(
                (
                    old
                    for old in prior
                    if old.kind == ApprovalKind.COMPUTE
                    and old.status == ApprovalStatus.APPROVED
                    and {k: v for k, v in old.payload.items() if k != "rationale"}
                    == {k: v for k, v in approval.payload.items() if k != "rationale"}
                ),
                None,
            )
            carried.append(match if match is not None else approval)
        return carried

    async def decide_approval(
        self,
        task_id: str,
        approval_id: str,
        principal: str,
        decision: ApprovalDecision,
        *,
        defer_replan: bool = False,
    ) -> TaskRecord:
        task = await self.require_access(task_id, principal)
        approval = next((item for item in task.approvals if item.id == approval_id), None)
        if approval is None:
            raise LookupError(approval_id)
        if approval.status != ApprovalStatus.PENDING:
            return task
        v2_decision = task.planner_version >= 2 and approval.kind in {
            ApprovalKind.SCIENTIFIC_STEP,
            ApprovalKind.HYPERPARAMETER,
        }
        if decision.approved and v2_decision:
            if not decision.response.strip():
                raise ValueError("an approval requires explicit values for the proposed settings")
            if task.master_plan is None or task.contract is None:
                raise ValueError("master plan is missing")
            if approval.payload.get("master_plan_id") != task.master_plan.id or int(
                approval.payload.get("master_plan_revision", 0)
            ) != int(task.master_plan.revision):
                raise ValueError("this approval belongs to a stale master-plan revision")
            try:
                parsed = json.loads(decision.response)
            except json.JSONDecodeError as exc:
                raise ValueError("settings must be submitted as structured values") from exc
            values = parsed.get("values", parsed) if isinstance(parsed, dict) else None
            if not isinstance(values, dict):
                raise ValueError("settings payload must contain a values object")
            expected = {str(item) for item in approval.payload.get("parameter_ids", [])}
            supplied = {str(item) for item in values}
            if expected != supplied:
                missing = sorted(expected - supplied)
                extra = sorted(supplied - expected)
                raise ValueError(
                    f"settings do not match this plan; missing={missing}, extra={extra}"
                )
            all_values = {item.id: item.proposed_value for item in task.master_plan.parameters}
            all_values.update({str(key): value for key, value in values.items()})
            task.decision_set = DecisionSet(
                task_id=task.id,
                master_plan_id=task.master_plan.id,
                master_plan_revision=task.master_plan.revision,
                values=all_values,
                approved_by=principal,
                approved_at=utcnow(),
            )
            task.decision_set_id = task.decision_set.id
            normalized = self._normalize_structured_decision(
                approval.payload, decision.response.strip()
            )
            task.contract.authorized_scientific_actions.append(normalized)
            approval.payload["decision"] = normalized
            waiting = next(
                (
                    item
                    for item in reversed(task.stage_summaries)
                    if item.kind == StageKind.USER_DECISIONS and item.status == StageStatus.WAITING
                ),
                None,
            )
            if waiting is not None:
                waiting.status = StageStatus.COMPLETED
                waiting.output_hash = self._stable_hash(task.decision_set)
                waiting.output_ref = task.decision_set.id
                waiting.completed_at = utcnow()
                waiting.metrics = {"model_calls": 0, "approved_parameters": len(values)}
        elif decision.approved and approval.kind == ApprovalKind.SCIENTIFIC_STEP:
            if not decision.response.strip():
                raise ValueError("a scientific approval requires an explicit decision response")
            if task.contract is None:
                raise ValueError("task contract is missing")
            normalized = self._normalize_structured_decision(
                approval.payload, decision.response.strip()
            )
            task.contract.authorized_scientific_actions.append(normalized)
            task.contract.missing_scientific_decisions.clear()
            task.contract.proposed_scientific_decisions.clear()
            approval.payload["decision"] = normalized
        if decision.approved and approval.kind == ApprovalKind.HYPERPARAMETER:
            if not decision.response.strip():
                raise ValueError("a hyperparameter approval requires an explicit decision response")
            if task.contract is None:
                raise ValueError("task contract is missing")
            normalized = self._normalize_structured_decision(
                approval.payload, decision.response.strip()
            )
            approval.payload["decision"] = normalized
            task.contract.authorized_scientific_actions.append(
                f"Tool hyperparameters: {normalized}"
            )
        if decision.approved and approval.kind == ApprovalKind.TOOL_ADD:
            for tool_id in approval.payload.get("tool_ids", []):
                tool = await self.repository.get_tool(str(tool_id))
                if tool is None or tool.status != ToolStatus.PROPOSED:
                    raise ValueError(f"tool proposal {tool_id} is missing or not pending")
                tool.status = ToolStatus.APPROVED
                tool.updated_at = utcnow()
                await self.repository.put_tool(tool)
                await self.tool_builds.start(tool)
        approval.status = ApprovalStatus.APPROVED if decision.approved else ApprovalStatus.REJECTED
        approval.decided_at = utcnow()
        approval.decided_by = principal
        decision_text = f"{'Approved' if decision.approved else 'Declined'}: {approval.title}."
        if decision.response.strip():
            decision_text += " " + self._normalize_structured_decision(
                approval.payload, decision.response.strip()
            )
        task.conversation.append(
            ConversationMessage(
                event_key=f"decision:{approval.id}",
                text=decision_text,
                kind="decision",
                attempt=task.attempts,
            )
        )
        task.updated_at = utcnow()
        if not decision.approved:
            task.status = TaskStatus.CANCELLED
            task.error = f"Approval {approval.id} was rejected."
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task
        pending = [item for item in task.approvals if item.status == ApprovalStatus.PENDING]
        if pending:
            task.status = TaskStatus.NEEDS_APPROVAL
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task
        await self.repository.update_task(task)
        if approval.kind == ApprovalKind.TOOL_ADD:
            task.status = TaskStatus.NEEDS_INPUT
            task.error = (
                "The exact tool version was approved. Execution remains paused until its "
                "Cloud Build scan and smoke test publish an available image."
            )
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task
        if approval.kind == ApprovalKind.COMPUTE and defer_replan:
            task.status = TaskStatus.RECEIVED
            task.error = None
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task
        if task.builder_protocol_version == 2 and approval.kind == ApprovalKind.COMPUTE:
            task.status = TaskStatus.RECEIVED
            task.error = None
            await self.repository.update_task(task)
            return await self.process(task.id)
        if any(
            item.kind in {ApprovalKind.SCIENTIFIC_STEP, ApprovalKind.HYPERPARAMETER}
            for item in task.approvals
        ):
            task.status = TaskStatus.RECEIVED
            task = await self.repository.update_task(task)
            if defer_replan:
                await self._notify(task)
                return task
            return await self.process(task.id)
        task = await self._execute_or_complete(task)
        await self._notify(task)
        return task

    @staticmethod
    def _normalize_structured_decision(payload: dict[str, object], response: str) -> str:
        """Turn the compact editor's JSON into stable, readable authorization text."""
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            return response
        if not isinstance(parsed, dict):
            return response
        raw_fields = payload.get("fields", payload.get("parameters", []))
        fields = raw_fields if isinstance(raw_fields, list) else []
        labels = {
            str(field.get("id", "")): str(field.get("label", field.get("name", "")))
            for field in fields
            if isinstance(field, dict)
        }
        values = parsed.get("values", parsed)
        if not isinstance(values, dict):
            return response
        lines = [f"{labels.get(str(key), str(key))}: {value}" for key, value in values.items()]
        return "; ".join(lines) if lines else response

    async def _execute_or_complete(self, task: TaskRecord) -> TaskRecord:
        await self._assert_interaction_current(task)
        if task.contract is None or task.resource_plan is None or task.tool_plan is None:
            raise ValueError("planned task is incomplete")
        missing_inputs = self._missing_execution_inputs(task)
        if task.contract.requires_execution and missing_inputs:
            task.status = TaskStatus.NEEDS_INPUT
            task.error = "Required execution input is unavailable: " + ", ".join(missing_inputs)
            task.updated_at = utcnow()
            task = await self.repository.update_task(task)
            await self._notify(task)
            return task
        if task.contract is None or task.resource_plan is None or task.tool_plan is None:
            raise RuntimeError("validated task plan was unexpectedly cleared")
        batch_submission = self.settings.execution_backend == "gcp_batch"
        resuming_submission = (
            batch_submission
            and task.status == TaskStatus.SUBMITTING
            and task.attempts > 0
            and not task.execution_job_name
        )
        execution_hash = self._stable_hash(
            {
                "script": task.generated_script,
                "resource_plan": task.resource_plan.model_dump(mode="json"),
                "tool_plan": task.tool_plan.model_dump(mode="json"),
                "dataset_inputs": [
                    item.model_dump(mode="json") for item in task.dataset_inspections
                ],
            }
        )
        execution_stage: TaskStage | None = None
        if task.planner_version >= 2:
            if resuming_submission:
                execution_stage = next(
                    (
                        item
                        for item in reversed(task.stage_summaries)
                        if item.kind == StageKind.EXECUTION
                        and item.status == StageStatus.RUNNING
                        and item.input_hash == execution_hash
                    ),
                    None,
                )
            if execution_stage is None:
                execution_stage = await self._start_stage(
                    task,
                    StageKind.EXECUTION,
                    execution_hash,
                )
        if task.contract.requires_execution:
            if not resuming_submission and task.attempts >= self.MAX_EXECUTION_ATTEMPTS:
                task.status = TaskStatus.FAILED
                task.error = self._public_execution_failure(task.attempts)
                task.updated_at = utcnow()
                return await self.repository.update_task(task)
            if not task.generated_script.strip():
                task.status = TaskStatus.NEEDS_INPUT
                task.error = "The plan requires execution but did not contain an executable script."
                task.updated_at = utcnow()
                return await self.repository.update_task(task)
            if not resuming_submission:
                task.status = TaskStatus.SUBMITTING if batch_submission else TaskStatus.RUNNING
                task.attempts += 1
                task.submission_attempts = 0
                task.error = None
                task.updated_at = utcnow()
                await self.repository.update_task(task)
                if not batch_submission:
                    await self._notify_progress(task)
            bundle = ExecutionBundle(
                task_id=task.id,
                script=task.generated_script,
                contract=task.contract,
                resource_plan=task.resource_plan,
                tool_plan=task.tool_plan,
                parameter_manifest=(
                    effective_parameter_manifest(task.master_plan, task.decision_set)
                    if task.master_plan is not None and task.decision_set is not None
                    else []
                ),
                figure_specs=task.figure_specs,
                context_manifest=(task.context_bundle.manifest if task.context_bundle else []),
                input_attachments=task.envelope.attachments,
                dataset_inputs=task.dataset_inspections,
                attempt=task.attempts,
            )
            execute_parameters = inspect.signature(self.executor.execute).parameters
            try:
                if "progress_callback" in execute_parameters:
                    result = await self.executor.execute(
                        bundle,
                        progress_callback=lambda progress: self._record_progress(task, progress),
                    )
                else:
                    # Preserve compatibility with custom backends implementing the original API.
                    result = await self.executor.execute(bundle)
            except Exception as exc:
                if not batch_submission:
                    raise
                task.submission_attempts += 1
                task.status = TaskStatus.SUBMITTING
                task.execution_job_name = None
                task.error = "Compute submission is temporarily unavailable; retrying safely."
                task.updated_at = utcnow()
                if execution_stage is not None:
                    execution_stage.metrics["submission_error"] = type(exc).__name__
                    execution_stage.metrics["submission_attempts"] = task.submission_attempts
                if task.submission_attempts >= self.MAX_SUBMISSION_ATTEMPTS:
                    task.status = TaskStatus.FAILED
                    task.error = (
                        "The analysis could not be submitted to compute after "
                        f"{task.submission_attempts} attempts."
                    )
                    task.current_stage = StageKind.EXECUTION
                    if execution_stage is not None:
                        execution_stage.status = StageStatus.FAILED
                        execution_stage.error = f"{type(exc).__name__}: {exc}"
                        execution_stage.completed_at = utcnow()
                        execution_stage.metrics["outcome"] = "submission_failed"
                    return await self.repository.update_task(task)
                await self.repository.update_task(task)
                raise ExecutionSubmissionPending(task.id) from exc
            result.effective_parameters = (
                effective_parameter_manifest(task.master_plan, task.decision_set)
                if task.master_plan is not None and task.decision_set is not None
                else []
            )
            result.figure_specs = task.figure_specs
            task.result = result
            task.submission_attempts = 0
            task.error = None
            if batch_submission and result.exit_code is None:
                task.status = TaskStatus.QUEUED
                task.execution_job_name = result.output_text
                task.paused_state_uri = await self.snapshots.save(task)
            elif result.exit_code == 0:
                task.status = TaskStatus.SUCCEEDED
            else:
                if execution_stage is not None:
                    self._mark_execution_stage_failed(task, execution_stage, result)
                    execution_stage = None
                if await self._repair_generated_code(task, result):
                    return await self._execute_or_complete(task)
                task.status = TaskStatus.FAILED
                task.error = self._public_execution_failure(task.attempts)
                task.current_stage = StageKind.EXECUTION
        else:
            task.result = TaskResult(
                summary=task.plan_response or "Request completed without code execution.",
                output_text=task.plan_response,
                exit_code=0,
            )
            task.status = TaskStatus.SUCCEEDED
        if execution_stage is not None and task.status != TaskStatus.QUEUED:
            await self._complete_stage(
                task,
                execution_stage,
                task.result or {"status": task.status.value},
                {"model_calls": 0, "outcome": task.status.value},
            )
        task.updated_at = utcnow()
        task = await self.repository.update_task(task)
        if task.status == TaskStatus.SUCCEEDED:
            await self.memory_manager.promote_task_result(task)
            if await self.compactor.should_compact(task.envelope.project_id):
                job = asyncio.create_task(
                    self._compact_project_session(
                        task.envelope.project_id, session_key(task.envelope)
                    )
                )
                self._maintenance.add(job)
                job.add_done_callback(self._maintenance.discard)
        return task

    async def _repair_generated_code(self, task: TaskRecord, result: TaskResult) -> bool:
        """Revise failed code while preserving the approved scientific plan."""
        if task.attempts >= self.MAX_EXECUTION_ATTEMPTS:
            return False
        build_code = getattr(self.harness, "build_code", None)
        if build_code is None or task.contract is None:
            return False
        if task.resource_plan is None or task.tool_plan is None:
            return False

        diagnostic = self._execution_failure_detail(result)
        task.result = result
        task.status = TaskStatus.PLANNING
        task.error = None
        task.execution_job_name = None
        task.paused_state_uri = None
        task.progress_updates.append(
            TaskProgress(
                attempt=task.attempts,
                status=ProgressStatus.INFO,
                message=(
                    f"Run {task.attempts} hit a technical issue. Revising the code and "
                    "retrying automatically."
                ),
            )
        )
        task.progress_updates = task.progress_updates[-500:]
        task.updated_at = utcnow()
        await self.repository.update_task(task)
        await self._notify_progress(task)

        master = task.master_plan or MasterPlan(
            task_id=task.id,
            contract=task.contract,
            primary_tool_ids=list(task.tool_plan.approved_tool_ids),
            response=task.plan_response,
        )
        decisions = task.decision_set or DecisionSet(
            task_id=task.id,
            master_plan_id=master.id,
            master_plan_revision=master.revision,
        )
        figure_plan = FigurePlan(figures=task.figure_specs)
        original_script = task.generated_script
        repair_hash = self._stable_hash(
            {
                "failed_script": original_script,
                "execution_diagnostic": diagnostic,
                "execution_attempt": task.attempts,
            }
        )
        stage = await self._start_stage(
            task,
            StageKind.CODE_GENERATION,
            repair_hash,
            model=self.settings.worker_model,
            reasoning=self.settings.worker_reasoning_effort,
        )
        repair_errors: list[str] = []
        registry = await self.repository.list_tools()
        for repair_attempt in range(2):
            repair_instruction = diagnostic
            if repair_errors:
                repair_instruction += (
                    "\n\nThe prior repair response was invalid: " + repair_errors[-1]
                )
            try:
                build, metrics = await build_code(
                    task_id=task.id,
                    envelope=task.envelope,
                    master_plan=master,
                    decisions=decisions,
                    tool_plan=task.tool_plan,
                    resource_plan=task.resource_plan,
                    figure_plan=figure_plan,
                    dataset_inspections=[
                        item.model_dump(mode="json") for item in task.dataset_inspections
                    ],
                    repair_error=repair_instruction,
                    existing_script=original_script,
                    authoritative_fallback=repair_attempt == 1,
                )
                validate_script(build.script, require_preflight=True)
                invocations = self._resolve_runtime_invocations(build.runtime_tool_ids, registry)
                if build.script.strip() == original_script.strip():
                    raise ScriptRejected("the proposed repair did not revise the failing code")
                task.generated_script = build.script
                task.runtime_tool_ids = build.runtime_tool_ids
                for invocation in invocations:
                    if invocation.tool_id not in task.tool_plan.approved_tool_ids:
                        task.tool_plan.approved_tool_ids.append(invocation.tool_id)
                    if not any(
                        item.tool_id == invocation.tool_id for item in task.tool_plan.invocations
                    ):
                        task.tool_plan.invocations.append(invocation)
                metrics["execution_attempt_repaired"] = task.attempts
                metrics["repair_generation_attempts"] = repair_attempt + 1
                await self._complete_stage(task, stage, build, metrics)
                validation_stage = await self._start_stage(
                    task,
                    StageKind.VALIDATION,
                    self._stable_hash(task.generated_script),
                )
                await self._complete_stage(
                    task,
                    validation_stage,
                    {"valid": True},
                    {"model_calls": 0, "outcome": "passed_after_repair"},
                )
                task.result = None
                task.error = None
                task.updated_at = utcnow()
                await self.repository.update_task(task)
                return True
            except Exception as exc:
                repair_errors.append(f"{type(exc).__name__}: {exc}")

        await self._fail_stage(task, stage, "; ".join(repair_errors))
        return False

    @classmethod
    def _execution_failure_detail(cls, result: TaskResult) -> str:
        detail = result.stderr or result.output_text or result.summary
        return detail[-cls.EXECUTION_DIAGNOSTIC_LIMIT :]

    @staticmethod
    def _public_execution_failure(attempts: int) -> str:
        return (
            f"The analysis encountered a technical execution error after {attempts} "
            "attempts. The generated code could not be repaired automatically."
        )

    def _mark_execution_stage_failed(
        self, task: TaskRecord, stage: TaskStage, result: TaskResult
    ) -> None:
        stage.status = StageStatus.FAILED
        stage.error = self._execution_failure_detail(result)
        stage.output_hash = self._stable_hash(result)
        stage.completed_at = utcnow()
        stage.metrics.update({"model_calls": 0, "outcome": TaskStatus.FAILED.value})

    async def _compact_project_session(self, project_id: str, conversation_id: str) -> None:
        await self.compactor.compact(project_id)
        with suppress(Exception):
            await self.harness.compact_session(conversation_id)

    async def _notify(self, task: TaskRecord, *, event_key: str | None = None) -> None:
        event_key = event_key or self._notification_event_key(task)
        # Persist the public wording before external delivery. Every surface consumes it.
        existing = next((m for m in task.conversation if m.event_key == event_key), None)
        if existing is None:
            kind = communication_kind(task, event_key, utcnow())
            if kind is not None:
                view = task.model_copy(
                    update={
                        "followup_response": task.followup_response if kind == "replan" else "",
                    }
                )
                text = (
                    task.followup_response
                    if kind == "replan"
                    else await self.conversation_agent.render(view)
                )
                previous = next(
                    (m for m in reversed(task.conversation) if m.role == "assistant"), None
                )
                if previous is None or previous.text != text or kind != "progress":
                    task.conversation.append(
                        ConversationMessage(
                            event_key=event_key,
                            text=text,
                            kind=kind,
                            attempt=task.attempts,
                            phase=communication_phase(task),
                        )
                    )
                    saved = await self.repository.update_task(task)
                    task.conversation = saved.conversation
                    if (
                        saved.interaction_revision != task.interaction_revision
                        or saved.development_version != task.development_version
                    ):
                        return
        if self.notifier is None:
            return
        try:
            # Flush earlier saved turns after a failed delivery, in original order.
            await self.notifier.notify(task)
        except Exception:
            return
        if event_key not in task.notified_event_keys:
            task.notified_event_keys.append(event_key)
            task.notified_event_keys = task.notified_event_keys[-200:]
        latest = next(
            (item for item in reversed(task.progress_updates) if item.attempt == task.attempts),
            None,
        )
        if latest is not None:
            task.last_notified_progress_id = latest.id
        task.updated_at = utcnow()
        await self.repository.update_task(task)

    def _notification_event_key(self, task: TaskRecord) -> str:
        """Return a stable identity for one public lifecycle notification."""
        if task.status == TaskStatus.NEEDS_APPROVAL:
            pending = sorted(
                item.id for item in task.approvals if item.status == ApprovalStatus.PENDING
            )
            return f"approval:{self._stable_hash(pending)}"
        if task.status in {
            TaskStatus.NEEDS_INPUT,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            payload = {
                "status": task.status.value,
                "error": task.error,
                "result": task.result,
            }
            return f"terminal:{self._stable_hash(payload)}"
        latest = next(
            (item for item in reversed(task.progress_updates) if item.attempt == task.attempts),
            None,
        )
        if latest is not None:
            return f"progress:{latest.id}"
        if task.status == TaskStatus.QUEUED:
            return f"queued:{task.attempts}"
        if task.status == TaskStatus.SUBMITTING:
            return f"submitting:{task.attempts}"
        if task.current_stage is not None:
            stage = next(
                (
                    item
                    for item in reversed(task.stage_summaries)
                    if item.kind == task.current_stage
                ),
                None,
            )
            if stage is not None:
                return f"stage:{stage.id}"
        return (
            f"status:{task.status.value}:interaction:{task.interaction_revision}:"
            f"attempt:{task.attempts}"
        )

    async def _notify_progress(self, task: TaskRecord) -> None:
        """Publish lifecycle changes to every delivery route attached to the task."""
        await self._notify(task)

    async def _record_progress(self, task: TaskRecord, progress: TaskProgress) -> None:
        """Persist and immediately deliver one validated execution milestone."""
        if progress.attempt != task.attempts or task.status not in {
            TaskStatus.RUNNING,
            TaskStatus.QUEUED,
        }:
            return
        if progress.trusted:
            task.status = TaskStatus.RUNNING
        # Estimate only the current measured phase, never invent a whole-analysis ETA.
        if progress.current is not None and progress.total is not None:
            previous = next(
                (
                    p
                    for p in reversed(task.progress_updates)
                    if p.attempt == progress.attempt
                    and p.item == progress.item
                    and p.total == progress.total
                    and p.current is not None
                    and p.current < progress.current
                ),
                None,
            )
            if previous is not None and previous.current is not None:
                seconds = (progress.created_at - previous.created_at).total_seconds()
                if seconds > 0 and progress.current <= progress.total:
                    remaining = (
                        seconds
                        / (progress.current - previous.current)
                        * (progress.total - progress.current)
                    )
                    progress.eta_low_seconds, progress.eta_high_seconds = (
                        remaining * 0.5,
                        remaining * 2,
                    )
                    progress.eta_basis = "Current phase throughput; later phases excluded"
        task.progress_updates.append(progress)
        # Keep the task document bounded while retaining enough detail for long queues.
        task.progress_updates = task.progress_updates[-500:]
        task.updated_at = utcnow()
        await self.repository.update_task(task)
        await self._notify_progress(task)

    async def record_batch_progress(
        self, task_id: str, attempt: int, progress: TaskProgress
    ) -> TaskRecord:
        """Accept a progress milestone published by an active immutable Batch attempt."""
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = await self.repository.get_task(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if (
                attempt != progress.attempt
                or task.attempts != attempt
                or task.status not in {TaskStatus.QUEUED, TaskStatus.RUNNING}
            ):
                return task
            if task.development and task.development.preparation_version == 2:
                active = next(
                    (o for o in reversed(task.development.operations) if o.status != "completed"),
                    None,
                )
                if (
                    active is None
                    or (progress.operation_id and progress.operation_id != active.id)
                    or (
                        task.development.worker_session_id
                        and progress.worker_session_id != task.development.worker_session_id
                    )
                ):
                    return task
            await self._record_progress(task, progress)
            return task

    async def cancel(self, task_id: str, principal: str) -> TaskRecord:
        task = await self.require_access(task_id, principal)
        if task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return task
        task.interaction_revision += 1
        task.cancel_requested_at = utcnow()
        task.status = TaskStatus.CANCELLED
        task.error = None
        task.updated_at = utcnow()
        task = await self.repository.update_task(task)
        cancel_execution = getattr(self.executor, "cancel", None)
        if cancel_execution is not None:
            with suppress(Exception):
                await cancel_execution(task.id, task.execution_job_name)
        await self._notify(task)
        return task

    async def add_context(
        self,
        task_id: str,
        principal: str,
        message: str,
        *,
        source: TaskSource,
        external_event_id: str,
    ) -> tuple[TaskRecord, bool]:
        """Attach a follow-up and restart planning before immutable execution begins."""
        normalized = message.strip()
        if not normalized:
            raise ValueError("follow-up context cannot be blank")
        task = await self.require_access(task_id, principal)
        if any(item.external_event_id == external_event_id for item in task.context_updates) or any(
            item.event_key == external_event_id for item in task.conversation
        ):
            return task, False
        if task.status in {TaskStatus.SUBMITTING, TaskStatus.QUEUED, TaskStatus.RUNNING}:
            reason = "Execution has already started. Cancel this task, then send a revised request."
        elif task.status in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            reason = "This task is finished. Send a new message to continue the conversation."
        else:
            reason = ""
        if reason:
            task.conversation.extend(
                [
                    ConversationMessage(
                        event_key=external_event_id,
                        role="user",
                        text=normalized,
                        kind="followup",
                        source=source,
                    ),
                    ConversationMessage(
                        event_key=f"response:{external_event_id}",
                        text=reason,
                        kind="input",
                    ),
                ]
            )
            saved = await self.repository.update_task(task)
            if self.notifier is not None:
                with suppress(Exception):
                    await self.notifier.notify(saved)
            raise TaskInteractionConflict(reason)
        if not await self.repository.mark_event(external_event_id):
            return await self.require_access(task_id, principal), False

        # Save the authority and the question before invalidating derived work. A second
        # follow-up arriving before replanning extends this same handoff instead of erasing it.
        if task.master_plan is not None or task.plan_handoff is None:
            task.plan_handoff = PlanHandoff(
                master_plan=task.master_plan,
                decision_set=task.decision_set,
                contract=task.contract,
                pending_question=task.error or "",
                conversation=task.conversation[-12:],
            ).model_copy(deep=True)
        elif task.status == TaskStatus.NEEDS_INPUT:
            task.plan_handoff.pending_question = task.error or ""
            task.plan_handoff.conversation = [
                item.model_copy(deep=True) for item in task.conversation[-12:]
            ]
        task.conversation.append(
            ConversationMessage(
                event_key=external_event_id,
                role="user",
                text=normalized,
                kind="followup",
                source=source,
            )
        )
        task.context_updates.append(
            TaskContextUpdate(
                message=normalized,
                principal_id=principal,
                source=source,
                external_event_id=external_event_id,
            )
        )
        task.plan_handoff.followups.append(task.context_updates[-1].model_copy(deep=True))
        if task.builder_protocol_version == 2 and task.development:
            task.development.state = "needs_input"
        task.interaction_revision += 1
        task.envelope = task.envelope.model_copy(
            update={
                "prompt": (
                    f"{task.envelope.prompt}\n\n"
                    "Additional user context (newer instructions take precedence):\n"
                    f"{normalized}"
                )
            }
        )
        # Invalidate derived execution state; the saved handoff remains the amendment baseline.
        task.structured_request = None
        task.status = TaskStatus.RECEIVED
        task.contract = None
        task.resource_plan = None
        task.tool_plan = None
        task.pending_tool_builds = []
        task.generated_script = ""
        task.runtime_tool_ids = []
        task.dataset_inspections = []
        task.context_bundle = None
        task.result = None
        task.submission_attempts = 0
        task.approvals = [
            item
            for item in task.approvals
            if item.kind == ApprovalKind.COMPUTE and item.status == ApprovalStatus.APPROVED
        ]
        task.error = None
        task.execution_job_name = None
        task.plan_response = ""
        task.followup_response = ""
        task.progress_updates = []
        task.paused_state_uri = None
        task.master_plan_id = None
        task.decision_set_id = None
        task.current_stage = None
        task.stage_summaries = []
        task.master_plan = None
        task.decision_set = None
        task.figure_specs = []
        task.updated_at = utcnow()
        saved = await self.repository.update_task(task)
        added = any(item.external_event_id == external_event_id for item in saved.context_updates)
        await self._notify(saved)
        return saved, added

    async def _assert_interaction_current(self, task: TaskRecord) -> None:
        current = await self.repository.get_task(task.id)
        if current is None:
            raise TaskNotFound(task.id)
        if (
            current.interaction_revision != task.interaction_revision
            or current.status == TaskStatus.CANCELLED
        ):
            raise TaskInteractionSuperseded(task.id)

    async def complete_batch(self, task_id: str, attempt: int, result: TaskResult) -> TaskRecord:
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = await self.repository.get_task(task_id)
            if task is None:
                raise TaskNotFound(task_id)
            if task.builder_protocol_version == 2 and task.development:
                if task.status in {TaskStatus.CANCELLED, TaskStatus.SUCCEEDED, TaskStatus.FAILED}:
                    return task
                if task.attempts != attempt:
                    return task
                active = next(
                    (
                        op
                        for op in reversed(task.development.operations)
                        if op.status != "completed"
                    ),
                    None,
                )
                if (
                    task.development.interaction_revision != task.interaction_revision
                    or active is None
                    or active.id != result.operation_id
                ):
                    return task
                return await self._advance_development(task, result)
            if (
                task.status == TaskStatus.SUBMITTING
                and task.attempts == attempt + 1
                and not task.execution_job_name
            ):
                task = await self._execute_or_complete(task)
                await self._notify(task)
                return task
            if task.attempts != attempt or task.status != TaskStatus.QUEUED:
                return task
            task.result = result
            if task.master_plan is not None and task.decision_set is not None:
                task.result.effective_parameters = effective_parameter_manifest(
                    task.master_plan, task.decision_set
                )
                task.result.figure_specs = task.figure_specs
            task.paused_state_uri = None
            task.execution_job_name = None
            stage: TaskStage | None = None
            if task.planner_version >= 2:
                stage = next(
                    (
                        item
                        for item in reversed(task.stage_summaries)
                        if item.kind == StageKind.EXECUTION and item.status == StageStatus.RUNNING
                    ),
                    None,
                )
            if result.exit_code == 0:
                task.status = TaskStatus.SUCCEEDED
                task.error = None
                if stage is not None:
                    stage.status = StageStatus.COMPLETED
                    stage.output_hash = self._stable_hash(result)
                    stage.completed_at = utcnow()
                    stage.error = ""
                    stage.metrics.update({"model_calls": 0, "outcome": task.status.value})
            else:
                task.status = TaskStatus.FAILED
                if stage is not None:
                    self._mark_execution_stage_failed(task, stage, result)
                if await self._repair_generated_code(task, result):
                    task = await self._execute_or_complete(task)
                    await self._notify(task)
                    return task
                task.error = self._public_execution_failure(task.attempts)
                task.current_stage = StageKind.EXECUTION
            task.updated_at = utcnow()
            task = await self.repository.update_task(task)
            if task.status == TaskStatus.SUCCEEDED:
                await self.memory_manager.promote_task_result(task)
            await self._notify(task)
            return task

    async def reconcile_batch_jobs(self) -> dict[str, int]:
        """Resolve terminal Batch jobs when the worker completion event never arrives."""
        counts = {"checked": 0, "recovered": 0, "succeeded": 0, "failed": 0, "errors": 0}
        if not isinstance(self.executor, BatchStatusBackend):
            return counts
        for project in await self.repository.list_all_projects():
            for task in await self.repository.list_tasks(project.id, limit=100):
                if task.pending_tool_builds:
                    tools = [await self.repository.get_tool(t) for t in task.pending_tool_builds]
                    if any(t and t.status == ToolStatus.BUILDING for t in tools):
                        continue
                    await self.process(task.id)
                    counts["recovered"] += 1
                    continue
                if (
                    task.builder_protocol_version == 2
                    and task.development
                    and task.status == TaskStatus.PLANNING
                ):
                    await self.process(task.id)
                    counts["recovered"] += 1
                    continue
                if task.status not in {
                    TaskStatus.SUBMITTING,
                    TaskStatus.QUEUED,
                    TaskStatus.RUNNING,
                }:
                    continue
                if task.attempts < 1:
                    continue
                job_name = task.execution_job_name or (
                    task.result.output_text if task.result is not None else ""
                )
                if not job_name:
                    counts["checked"] += 1
                    if task.status == TaskStatus.QUEUED:
                        # Repair legacy false-QUEUED records created before submission succeeded.
                        task.status = TaskStatus.SUBMITTING
                        task.result = None
                        task.error = None
                        task.updated_at = utcnow()
                        await self.repository.update_task(task)
                    try:
                        resumed = (
                            await self.process(task.id)
                            if task.builder_protocol_version == 2
                            else await self._execute_or_complete(task)
                        )
                        if resumed.status == TaskStatus.QUEUED and resumed.execution_job_name:
                            counts["recovered"] += 1
                            await self._notify(resumed)
                        elif resumed.status == TaskStatus.FAILED:
                            counts["failed"] += 1
                            await self._notify(resumed)
                    except ExecutionSubmissionPending:
                        counts["errors"] += 1
                    continue
                counts["checked"] += 1
                try:
                    status = await self.executor.get_job_status(job_name)
                    if task.development and task.development.preparation_version == 2:
                        try:
                            recovered = await self.executor.load_job_result(task.id, task.attempts)
                        except Exception:
                            recovered = None
                        if recovered is not None:
                            await self.complete_batch(task.id, task.attempts, recovered)
                            counts["recovered"] += 1
                            continue
                    if status.state == "SUCCEEDED" and not (
                        task.development and task.development.preparation_version == 2
                    ):
                        result = await self.executor.load_job_result(task.id, task.attempts)
                        await self.complete_batch(task.id, task.attempts, result)
                        counts["succeeded"] += 1
                    elif status.state in {"FAILED", "SUCCEEDED", "DELETION_IN_PROGRESS"}:
                        detail = status.detail or "Google Batch job failed before completion."
                        if task.builder_protocol_version == 2 and task.development:
                            try:
                                recovered_result = await self.executor.load_job_result(
                                    task.id, task.attempts
                                )
                            except Exception:
                                from mica.models import FailureReport

                                operation = task.development.operations[-1]
                                recovered_result = TaskResult(
                                    operation_id=operation.id,
                                    source_hash=operation.source_hash,
                                    summary="Worker failed before durable publication.",
                                    stderr=detail,
                                    exit_code=1,
                                    publication_committed=True,
                                    failure=FailureReport(
                                        phase=operation.phase,
                                        category="infrastructure",
                                        message=detail,
                                    ),
                                )
                            await self.complete_batch(task.id, task.attempts, recovered_result)
                            counts["failed"] += 1
                            continue
                        completed = await self.complete_batch(
                            task.id,
                            task.attempts,
                            TaskResult(
                                summary="Google Batch could not complete the execution job.",
                                stderr=detail,
                                exit_code=1,
                            ),
                        )
                        if completed.status == TaskStatus.FAILED:
                            counts["failed"] += 1
                except Exception:
                    counts["errors"] += 1
        return counts

    async def close_idle_projects(self) -> int:
        count = 0
        cutoff = utcnow() - timedelta(days=self.settings.project_idle_days)
        for project in await self.repository.list_all_projects():
            if project.last_active_at >= cutoff:
                continue
            await self.compactor.compact(project.id, force=True)
            tasks = await self.repository.list_tasks(project.id, limit=100)
            for conversation_id in {session_key(task.envelope) for task in tasks}:
                await self.repository.replace_session_items(conversation_id, [])
            await self.memory_manager.inactivate_stale(project.id)
            count += 1
        return count
