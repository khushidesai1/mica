"""Durable, budgeted analysis development; one model continuation per dispatch."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pistachio.agents.development import DEVELOPMENT_INSTRUCTIONS, REVIEW_INSTRUCTIONS
from pistachio.agents.harness import AgentHarness
from pistachio.config import Settings
from pistachio.execution.backends import BatchStatusBackend, ExecutionBackend, GcpBatchExecutor
from pistachio.execution.workspace import WorkspaceStore, digest, safe_relative, validate_sources
from pistachio.models import (
    ApprovalStatus,
    ArtifactRef,
    CandidateReview,
    DecisionScope,
    DevelopmentAction,
    DevelopmentOperation,
    DevelopmentSession,
    ExecutionBundle,
    SourceFile,
    StageKind,
    TaskProgress,
    TaskRecord,
    TaskResult,
    TaskStatus,
    utcnow,
)
from pistachio.services.acceptance import checklist, compact_observation, helper_contracts
from pistachio.services.planning import effective_parameter_manifest
from pistachio.storage.base import Repository


class DevelopmentConflict(RuntimeError):
    pass


class DevelopmentService:
    @staticmethod
    def needs_research_input(review: CandidateReview) -> bool:
        return (
            review.scientific_amendment_required
            and review.decision_scope == DecisionScope.RESEARCH_DESIGN
            and bool(review.scientific_impact.strip())
        )

    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        harness: AgentHarness,
        executor: ExecutionBackend,
        progress_callback: Callable[[TaskRecord, TaskProgress], Awaitable[None]] | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.harness = harness
        self.executor = executor
        self.progress_callback = progress_callback

    async def save(self, task: TaskRecord) -> TaskRecord:
        task.updated_at = utcnow()
        saved = await self.repository.compare_and_swap_task(task, task.development_version)
        if saved is None:
            raise DevelopmentConflict("development continuation was superseded")
        task.development_version = saved.development_version
        if (
            task.development
            and task.development.worker_session_id
            and isinstance(self.executor, GcpBatchExecutor)
            and task.status
            in {
                TaskStatus.SUCCEEDED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
                TaskStatus.NEEDS_INPUT,
            }
        ):
            await self.executor.stop_session(task.id, task.development.worker_session_id)
        return task

    @staticmethod
    def authority(task: TaskRecord) -> str:
        return digest(
            {
                "contract": task.contract.model_dump(mode="json") if task.contract else None,
                "master": task.master_plan.model_dump(mode="json") if task.master_plan else None,
                "decisions": task.decision_set.model_dump(mode="json")
                if task.decision_set
                else None,
                "inputs": [x.model_dump(mode="json") for x in task.envelope.attachments],
                "datasets": [x.model_dump(mode="json") for x in task.dataset_inspections],
                "resources": task.resource_plan.model_dump(mode="json")
                if task.resource_plan
                else None,
            }
        )

    async def advance(self, task: TaskRecord, completion: TaskResult | None = None) -> TaskRecord:
        # Lease covers the longest permitted synchronous local operation. Cloud operations return
        # queued immediately, then resume through a fresh leased continuation.
        duration = int((task.resource_plan.timeout_hours if task.resource_plan else 2) * 3600 + 600)
        if isinstance(self.executor, GcpBatchExecutor):
            duration = int(
                max(
                    self.settings.development_initial_timeout_seconds,
                    self.settings.worker_timeout_seconds,
                )
                + 180
            )
        lease = await self.repository.begin_event(f"development:{task.id}", lease_seconds=duration)
        if lease.status != "claimed":
            raise DevelopmentConflict("another development continuation holds the lease")
        try:
            current = await self.repository.get_task(task.id)
            if current is None or current.interaction_revision != task.interaction_revision:
                raise DevelopmentConflict("task interaction changed")
            if current.development_version != task.development_version:
                raise DevelopmentConflict("task development changed")
            return await self._advance(task, completion)
        except DevelopmentConflict:
            raise
        except Exception as exc:
            if task.development:
                task.development.state = "failed"
                task.development.feedback = f"{type(exc).__name__}: {exc}"
            task.status = TaskStatus.FAILED
            task.error = "Development could not continue: " + str(exc)
            return await self.save(task)
        finally:
            await self.repository.release_event(f"development:{task.id}", lease.token)

    async def _advance(self, task: TaskRecord, completion: TaskResult | None) -> TaskRecord:
        if not self.settings.development_enabled:
            raise ValueError(
                "development protocol is disabled; enable only after sandbox conformance"
            )
        if (
            not task.contract
            or not task.master_plan
            or not task.decision_set
            or not task.resource_plan
            or not task.tool_plan
            or task.pending_tool_builds
            or any(a.status == ApprovalStatus.PENDING for a in task.approvals)
        ):
            # Reconciliation and delayed completions can arrive during replanning. This is
            # a scheduling conflict, never a reason to overwrite the task with FAILED.
            raise DevelopmentConflict("development is waiting for planning or approvals")
        if task.resource_plan.gpu_count:
            raise ValueError(
                "protocol 2 GPU isolation/replay has not passed conformance; use protocol 1"
            )
        store = WorkspaceStore(self.settings, task.id)
        if task.development is None:
            task.development = DevelopmentSession(
                preparation_version=(
                    2
                    if self.settings.development_latency_enabled
                    else 1
                    if self.settings.development_preparation_enabled
                    else 0
                ),
                worker_reuse_enabled=self.settings.worker_sessions_enabled,
                acceptance_checklist=checklist(task)
                if self.settings.development_latency_enabled
                else [],
                interaction_revision=task.interaction_revision,
                authority_hash=self.authority(task),
                runtime_image=self.settings.batch_image,
                developer_model=self.settings.development_model,
                prompt_hash=digest([DEVELOPMENT_INSTRUCTIONS, REVIEW_INSTRUCTIONS]),
            )
            task.development.source_hash = digest({})
            task.development.source_ref = await store.put({})
            task.status = TaskStatus.PLANNING
            await self.save(task)
        session = task.development
        if session.interaction_revision != task.interaction_revision and session.state in {
            "needs_input",
            "failed",
        }:
            # Replan after an explicit user amendment. Keep already spent budgets and source;
            # conservatively invalidate verification/checkpoints until the new plan is reviewed.
            session.authority_hash = self.authority(task)
            session.interaction_revision = task.interaction_revision
            session.state = "developing"
            session.review_hash = session.verified_hash = session.output_contract_hash = ""
            session.output_specs = []
            session.checkpoint_specs = []
            session.preparation_hash = ""
            session.implementation_plan = None
            session.candidate_review = None
            session.candidate_review_source = ""
            if session.preparation_version == 2:
                session.acceptance_checklist = checklist(task)
            session.feedback = (
                "User amended the task. Reconcile existing code with the new authority."
            )
            session.started_at = utcnow()  # paused user wait does not consume active wall budget
            await self.save(task)
        if session.authority_hash != self.authority(task):
            raise ValueError(
                "scientific authority changed; start an explicitly amended development revision"
            )
        if session.state in {"complete", "failed", "needs_input"}:
            return task
        if task.status == TaskStatus.CANCELLED:
            return task
        active = next((op for op in reversed(session.operations) if op.status != "completed"), None)
        if completion is not None:
            if active is None or completion.operation_id != active.id:
                return task  # duplicate or stale completion
            await self.accept(task, active, completion, store)
            return await self.save(task)
        if active is not None:
            if active.status == "reserved":
                return await self.dispatch(task, active, store)
            return task
        development_elapsed = (utcnow() - session.started_at).total_seconds()
        if session.preparation_version == 2:
            development_elapsed -= sum(
                (o.completed_at - o.reserved_at).total_seconds()
                for o in session.operations
                if o.phase == "run" and o.completed_at
            )
        if development_elapsed > self.settings.development_wall_seconds:
            raise ValueError("development wall-clock budget exhausted; evidence retained")
        if session.state == "reviewing_results":
            return await self.review_results(task, store)
        if session.state == "verified":
            return await self.reserve(task, "run", store)
        if not session.operations:
            return await self.reserve(task, "probe", store)
        if session.preparation_version == 2 and session.code_revisions:
            last = session.operations[-1]
            if last.source_hash != session.source_hash or last.phase == "probe":
                return await self.reserve(task, "verify", store)
        if session.model_turns >= self.settings.development_max_turns:
            raise ValueError("development model-turn budget exhausted; evidence retained")
        files = await store.get(session.source_ref)
        payload = await self.payload(task, files, store)
        session.model_turns += 1  # reserve before the external model call
        await self.save(task)
        action = await self.harness.development_action(task.id, task.envelope, payload)
        metrics = getattr(self.harness, "development_metrics", {}).get(task.id, {})
        session.model_input_tokens += int(metrics.get("input_tokens", 0))
        session.model_output_tokens += int(metrics.get("output_tokens", 0))
        action_ref = await store.put(
            {
                "action": action.model_dump(mode="json"),
                "metrics": metrics,
                "prompt_hash": digest([DEVELOPMENT_INSTRUCTIONS, REVIEW_INSTRUCTIONS]),
                "model": self.settings.development_model,
            }
        )
        session.action_refs.append(action_ref)
        # Keep request evidence even when validation rejects a malformed tool request.
        session.feedback = "Last tool request: " + action_ref
        try:
            return await self.apply_action(task, action, files, store)
        except (ValueError, SyntaxError) as exc:
            session.feedback = (
                f"Tool request rejected: {exc}. Revise the request using current evidence."
            )
            task.status = TaskStatus.PLANNING
            return await self.save(task)

    async def payload(
        self, task: TaskRecord, files: dict[str, str], store: WorkspaceStore
    ) -> dict[str, Any]:
        session = task.development
        assert session and task.master_plan and task.decision_set
        observations = []
        selected = session.operations[-4:]
        probe = next(
            (
                op
                for op in reversed(session.operations)
                if op.phase == "probe" and op.exit_code == 0 and op.result_ref
            ),
            None,
        )
        if probe is not None and probe not in selected:
            selected = [probe, *selected]
        for op in selected:
            if op.result_ref:
                raw = await store.get(op.result_ref)
                observations.append(
                    {
                        "operation_id": op.id,
                        "source_hash": op.source_hash,
                        "matches_current_source": op.source_hash == session.source_hash,
                        "phase": op.phase,
                        "exit_code": op.exit_code,
                        "summary": raw["summary"],
                        "failure": raw.get("failure"),
                        "stdout": raw.get("stdout", "")[:32000]
                        if op.phase == "probe"
                        else raw.get("stdout", "")[-16000:],
                        "stderr": raw.get("stderr", "")[-16000:],
                        "audit_artifacts": [a["name"] for a in raw.get("audit_artifacts", [])],
                    }
                )
        if session.preparation_version == 2:
            observations = [
                {
                    **{
                        k: item[k]
                        for k in (
                            "operation_id",
                            "source_hash",
                            "matches_current_source",
                            "phase",
                            "exit_code",
                            "summary",
                            "audit_artifacts",
                        )
                    },
                    **compact_observation(await store.get(op.result_ref), op.phase),
                }
                for item, op in zip(
                    observations, [o for o in selected if o.result_ref], strict=True
                )
            ]
        previous_review = None
        changed_paths = list(files)
        if session.preparation_version == 2 and session.review_refs:
            prior = await store.get(session.review_refs[-1])
            if prior.get("authority_hash") == session.authority_hash:
                previous_review = {"source_hash": prior["source_hash"], "review": prior["review"]}
                previous_files = await store.get(store.prefix + prior["source_hash"] + ".json")
                changed_paths = sorted(
                    k
                    for k in set(previous_files) | set(files)
                    if previous_files.get(k) != files.get(k)
                )
        images: list[str] = []
        if session.preview_artifact:
            images, _ = await self.artifact_evidence([session.preview_artifact])
        return {
            "image_previews": images,
            "workflow_version": session.preparation_version,
            "previous_review": previous_review,
            "changed_paths": changed_paths,
            "trusted_helper_contracts": helper_contracts()
            if session.preparation_version == 2
            else {},
            "acceptance_checklist": session.acceptance_checklist,
            "preparation_required": session.preparation_version == 1,
            "implementation_plan": session.implementation_plan.model_dump(mode="json")
            if session.implementation_plan
            else None,
            "preparation_approved": bool(session.preparation_hash),
            "initial_effort": session.preparation_version >= 1
            and (not session.preparation_hash or session.code_revisions == 0),
            "master_plan": task.master_plan.model_dump(mode="json"),
            "decisions": task.decision_set.model_dump(mode="json"),
            "figures": [f.model_dump(mode="json") for f in task.figure_specs],
            "effective_parameters": [
                p.model_dump(mode="json")
                for p in effective_parameter_manifest(task.master_plan, task.decision_set)
            ],
            "runtime_image": session.runtime_image,
            "resource_plan": task.resource_plan.model_dump(mode="json")
            if task.resource_plan
            else None,
            "source_hash": session.source_hash,
            "files": files,
            "design": session.design,
            "feedback": session.feedback,
            "output_specs": [x.model_dump(mode="json") for x in session.output_specs],
            "checkpoint_specs": [x.model_dump(mode="json") for x in session.checkpoint_specs],
            "observations": observations,
            "remaining_turns": self.settings.development_max_turns - session.model_turns,
            "remaining_revisions": self.settings.development_max_revisions - session.code_revisions,
            "remaining_operations": (
                self.settings.development_max_operations - len(session.operations)
            ),
            "remaining_development_seconds": (
                self.settings.development_seconds - session.reserved_seconds
            ),
        }

    async def apply_action(
        self,
        task: TaskRecord,
        action: DevelopmentAction,
        files: dict[str, str],
        store: WorkspaceStore,
    ) -> TaskRecord:
        session = task.development
        assert session and task.contract
        if action.action == "write_candidate":
            if session.preparation_version != 2:
                raise ValueError("write_candidate requires workflow two")
            await self.prepare(task, action, files, store)
            action = action.model_copy(update={"action": "edit_files"})
        if action.action == "plan_implementation":
            return await self.prepare(task, action, files, store)
        if action.action in {"inspect_runtime", "inspect_inputs"}:
            if (
                session.operations
                and session.operations[-1].phase == "probe"
                and (
                    session.operations[-1].status == "completed"
                    and session.operations[-1].exit_code == 0
                )
            ):
                session.feedback = (
                    "The fixed runtime/input probe already succeeded; see observations. "
                    "Implement the analysis. For more details, write a bounded test or inspection "
                    "and run_check; repeating the probe adds no data."
                )
                task.status = TaskStatus.PLANNING
                return await self.save(task)
            return await self.reserve(task, "probe", store)
        if action.action == "edit_files":
            self.require_preparation(task)
            if action.base_revision != session.source_hash:
                raise ValueError("stale source revision")
            if session.code_revisions >= self.settings.development_max_revisions:
                raise RuntimeError(
                    "source revision budget exhausted; retained code needs technical repair"
                )
            revised = dict(files)
            for path in action.remove_paths:
                revised.pop(safe_relative(path), None)
            for path, content in validate_sources(action.files).items():
                revised[path] = content
            validate_sources([SourceFile(path=k, content=v) for k, v in revised.items()])
            if revised == files:
                raise ValueError("edit did not change the candidate")
            if session.preparation_version >= 1 and session.code_revisions == 0:
                assert session.implementation_plan
                if not set(session.implementation_plan.module_paths) <= revised.keys():
                    raise ValueError(
                        "first implementation must include every planned module and test"
                    )
                if action.output_specs != session.implementation_plan.output_specs:
                    raise ValueError("first implementation must use the reviewed output specs")
                for path, content in revised.items():
                    if path.endswith(".py"):
                        compile(content, path, "exec")
            if action.output_specs:
                if (
                    session.output_contract_hash
                    and digest([x.model_dump(mode="json") for x in action.output_specs])
                    != session.output_contract_hash
                ):
                    raise ValueError("reviewed output contract is immutable")
                requested = set(task.contract.requested_outputs)
                if {x.requested_output for x in action.output_specs} != requested:
                    raise ValueError("output specs must cover exactly the requested deliverables")
                paths = [safe_relative(x.path) for x in action.output_specs]
                if len(set(paths)) != len(paths) or any(x == "reproduction.zip" for x in paths):
                    raise ValueError("duplicate or reserved delivery path")
            if action.checkpoint_specs:
                if (
                    session.output_contract_hash
                    and action.checkpoint_specs != session.checkpoint_specs
                ):
                    raise ValueError("reviewed checkpoint contract is immutable")
                for spec in action.checkpoint_specs:
                    for path in spec.source_paths:
                        if safe_relative(path) not in revised:
                            raise ValueError("checkpoint producer must name a source file")
                if len({x.id for x in action.checkpoint_specs}) != len(action.checkpoint_specs):
                    raise ValueError("duplicate checkpoint id")
            if not (action.output_specs or session.output_specs):
                raise ValueError("an executable candidate requires explicit requested output specs")
            if action.output_specs:
                session.output_specs = action.output_specs
            if action.checkpoint_specs:
                session.checkpoint_specs = action.checkpoint_specs
            if (
                session.first_candidate_source_hash == session.source_hash
                and session.first_check_passed is None
            ):
                session.first_candidate_outcome = "revised_before_validation"
                session.first_check_passed = False
            session.source_hash = digest(revised)
            session.source_ref = await store.put(revised)
            session.code_revisions += 1
            if not session.first_candidate_source_hash:
                session.first_candidate_source_hash = session.source_hash
            session.design = action.design or session.design
            session.review_hash = session.verified_hash = ""
            session.preview_artifact = None
            session.candidate_review = None
            session.candidate_review_source = ""
            session.feedback = action.reason
            task.generated_script = revised.get("analysis.py", "")
            task.current_stage = StageKind.CODE_GENERATION
        elif action.action == "inspect_artifact":
            if not session.operations[-1].result_ref:
                raise ValueError("no candidate artifacts available")
            result = TaskResult.model_validate(await store.get(session.operations[-1].result_ref))
            selected = next(
                (a for a in result.audit_artifacts if a.name == action.observation_path), None
            )
            if selected is None:
                raise ValueError("artifact is not in the latest operation manifest")
            session.preview_artifact = selected
            _, artifact_text = await self.artifact_evidence([selected])
            session.feedback = str(artifact_text)[:16000]
        elif action.action == "read_observation":
            if not session.operations[-1].result_ref:
                raise ValueError("no observation available")
            raw = await store.get(session.operations[-1].result_ref)
            if action.observation_path in {"stdout", "stderr"}:
                text = str(raw.get(action.observation_path, ""))
            else:
                from pistachio.services.artifacts import prepare_individual_artifact

                artifact = next(
                    (
                        x
                        for x in raw.get("audit_artifacts", [])
                        if x["name"] == action.observation_path
                    ),
                    None,
                )
                if artifact is None:
                    raise ValueError("observation path is not in the latest operation manifest")
                prepared = await prepare_individual_artifact(
                    ArtifactRef.model_validate(artifact), self.settings
                )
                try:
                    text = await asyncio.to_thread(prepared.files[0].path.read_text)
                finally:
                    prepared.cleanup()
            session.feedback = text[action.observation_offset : action.observation_offset + 16000]
        elif (
            action.action in {"run_check", "submit_candidate"} and session.preparation_version == 2
        ):
            if session.operations and session.operations[-1].source_hash == session.source_hash:
                raise ValueError(
                    "Current candidate was already validated; repair concrete findings"
                )
            return await self.reserve(task, "verify", store)
        elif action.action == "run_check":
            if not files:
                raise ValueError("write a candidate before running checks")
            self.require_preparation(task)
            if session.preparation_version == 1 and session.review_hash != session.source_hash:
                if not await self.review(task, await self.payload(task, files, store), store):
                    return await self.save(task)
                session.review_hash = session.source_hash
            return await self.reserve(task, "test", store)
        elif action.action == "submit_candidate":
            if not files or not session.output_specs:
                raise ValueError("candidate and output contract required")
            self.require_preparation(task)
            if session.review_hash != session.source_hash:
                if not await self.review(task, await self.payload(task, files, store), store):
                    return await self.save(task)
                session.review_hash = session.source_hash
            session.output_contract_hash = digest(
                [x.model_dump(mode="json") for x in session.output_specs]
            )
            return await self.reserve(task, "verify", store)
        elif action.action == "propose_amendment":
            if (
                action.amendment_kind != "scientific"
                or action.decision_scope != DecisionScope.RESEARCH_DESIGN
                or not action.scientific_impact.strip()
            ):
                session.feedback = (
                    "Handle this as a technical implementation issue without asking the user: "
                    + action.reason
                    + ". Use the authorized method's standard package workflow and defaults, "
                    "repair and run checks. Only an unavoidable research-design choice "
                    "warrants a scientific amendment."
                )
                task.status = TaskStatus.PLANNING
                task.error = None
                return await self.save(task)
            session.state = "needs_input"
            task.status = TaskStatus.NEEDS_INPUT
            task.error = action.scientific_impact
            session.feedback = task.error
            return await self.save(task)
        task.status = TaskStatus.PLANNING
        return await self.save(task)

    @staticmethod
    def preparation_identity(task: TaskRecord) -> str:
        session = task.development
        assert session and session.implementation_plan
        return digest(
            [
                session.authority_hash,
                session.runtime_image,
                session.implementation_plan.model_dump(mode="json"),
            ]
        )

    def require_preparation(self, task: TaskRecord) -> None:
        session = task.development
        assert session
        if session.preparation_version >= 1 and (
            not session.implementation_plan
            or session.preparation_hash != self.preparation_identity(task)
        ):
            raise ValueError(
                "a reviewed implementation plan is required before coding or execution"
            )

    async def prepare(
        self,
        task: TaskRecord,
        action: DevelopmentAction,
        files: dict[str, str],
        store: WorkspaceStore,
    ) -> TaskRecord:
        session, plan = task.development, action.implementation_plan
        assert session and task.contract and task.master_plan and task.decision_set
        assert task.resource_plan
        if plan is None:
            raise ValueError("plan_implementation requires a structured implementation plan")
        probe = next(
            (
                op
                for op in session.operations
                if op.id == plan.probe_operation_id
                and op.phase == "probe"
                and op.status == "completed"
                and op.exit_code == 0
                and op.result_ref
            ),
            None,
        )
        if probe is None:
            raise ValueError("implementation plan must cite a successful runtime/input probe")
        evidence = TaskResult.model_validate(await store.get(probe.result_ref))
        if (
            not evidence.verification
            or not evidence.verification.passed
            or evidence.verification.runtime_image != session.runtime_image
        ):
            raise ValueError("implementation plan requires probe evidence for the pinned runtime")
        if plan.blockers:
            raise ValueError("resolve implementation blockers before requesting design approval")
        expected = {p.id for p in effective_parameter_manifest(task.master_plan, task.decision_set)}
        bound = [p.parameter_id for p in plan.parameter_bindings]
        if set(bound) != expected or len(bound) != len(set(bound)):
            raise ValueError("implementation plan must bind every effective parameter exactly once")
        if plan.peak_memory_gib > task.resource_plan.memory_gib:
            raise ValueError("implementation memory estimate exceeds the approved worker")
        if {x.requested_output for x in plan.output_specs} != set(task.contract.requested_outputs):
            raise ValueError("implementation plan must cover exactly the requested deliverables")
        output_paths = [safe_relative(x.path) for x in plan.output_specs]
        if len(set(output_paths)) != len(output_paths) or "reproduction.zip" in output_paths:
            raise ValueError("duplicate or reserved output paths")
        if session.output_contract_hash and plan.output_specs != session.output_specs:
            raise ValueError("reviewed output contract is immutable")
        paths = [safe_relative(x) for x in plan.module_paths]
        if "analysis.py" not in paths or not any(x.startswith("tests/test") for x in paths):
            raise ValueError("implementation plan must include the entrypoint and production tests")
        payload = await self.payload(task, files, store)
        payload.update(
            review_phase="design",
            initial_effort=True,
            implementation_plan=plan.model_dump(mode="json"),
        )
        if session.preparation_version == 2 or await self.review(task, payload, store):
            session.implementation_plan = plan
            session.preparation_hash = self.preparation_identity(task)
            session.review_hash = session.verified_hash = ""
            session.feedback = (
                "Implementation plan checked. Write the complete implementation and tests "
                "in one coherent edit, covering every planned boundary and API adaptation."
            )
        return await self.save(task)

    async def review(
        self, task: TaskRecord, payload: dict[str, Any], store: WorkspaceStore
    ) -> bool:
        session = task.development
        assert session
        if session.model_turns >= self.settings.development_max_turns:
            raise ValueError("review exceeds model-turn budget")
        session.model_turns += 1
        await self.save(task)
        review = await self.harness.review_candidate(task.id, task.envelope, payload)
        metrics = getattr(self.harness, "development_metrics", {}).get(task.id, {})
        session.model_input_tokens += int(metrics.get("input_tokens", 0))
        session.model_output_tokens += int(metrics.get("output_tokens", 0))
        session.review_refs.append(
            await store.put(
                {
                    "review": review.model_dump(mode="json"),
                    "metrics": metrics,
                    "phase": payload.get("review_phase", "code"),
                    "source_hash": session.source_hash,
                    "authority_hash": session.authority_hash,
                    "runtime_image": session.runtime_image,
                    "implementation_plan": payload.get("implementation_plan"),
                    "prompt_hash": digest(REVIEW_INSTRUCTIONS),
                    "model": self.settings.development_model,
                }
            )
        )
        task.status = TaskStatus.PLANNING
        if review.approved and not review.scientific_amendment_required:
            return True
        session.feedback = "Review: " + "; ".join(review.issues)
        if self.needs_research_input(review):
            session.state = "needs_input"
            task.status = TaskStatus.NEEDS_INPUT
            task.error = review.scientific_impact
        else:
            session.feedback += (
                " Repair all identified technical issues together using the authorized method "
                "and standard package defaults. Do not ask the user about library mechanics."
            )
        return False

    async def reserve(self, task: TaskRecord, phase: str, store: WorkspaceStore) -> TaskRecord:
        session = task.development
        assert session and task.contract and task.resource_plan and task.tool_plan
        if phase != "probe":
            self.require_preparation(task)
            if (
                session.preparation_version == 1 or phase == "run"
            ) and session.review_hash != session.source_hash:
                raise ValueError("current code must pass review before worker execution")
        assert task.master_plan and task.decision_set
        if len(session.operations) >= self.settings.development_max_operations:
            raise ValueError("executable development operation budget exhausted")
        if phase == "run" and session.full_runs >= 2:
            raise ValueError("full analysis attempt budget exhausted")
        timeout = (
            task.resource_plan.timeout_hours * 3600
            if phase == "run"
            else self.settings.development_command_seconds
        )
        if phase != "run":
            used = sum(op.reserved_seconds for op in session.operations if op.phase != "run")
            if used + timeout > self.settings.development_seconds:
                raise ValueError("development compute budget exhausted")
        # Conservatively reserve the entire operation timeout plus worker startup/staging allowance.
        envelope_seconds = (
            2 * task.resource_plan.timeout_hours * 3600
            + self.settings.development_seconds
            + self.settings.development_max_operations * 300
        )
        rate = task.resource_plan.estimated_cost_usd / envelope_seconds
        cost = (timeout + 300) * rate
        if session.preparation_version == 2 and session.worker_reuse_enabled:
            elapsed = session.worker_seconds_consumed
            if session.worker_session_started_at:
                elapsed += (utcnow() - session.worker_session_started_at).total_seconds()
            session.reserved_cost_usd = elapsed * rate
        if session.reserved_cost_usd + cost > task.resource_plan.estimated_cost_usd + 1e-9:
            raise ValueError("cumulative approved compute estimate exhausted")
        sequence = task.attempts + 1
        op_id = digest([task.id, task.interaction_revision, phase, session.source_hash, sequence])
        op = DevelopmentOperation(
            id=op_id,
            phase=phase,
            source_hash=session.source_hash,
            attempt=sequence,
            reserved_seconds=timeout,
            reserved_cost_usd=cost,
        )
        verification = None
        if phase == "run":
            previous = next(
                x for x in reversed(session.operations) if x.phase == "verify" and x.exit_code == 0
            )
            verification = TaskResult.model_validate(
                await store.get(previous.result_ref)
            ).verification
        files = await store.get(session.source_ref)
        cached: list[ArtifactRef] = []
        if phase == "run":
            for previous_op in session.operations:
                if previous_op.phase == "run" and previous_op.result_ref:
                    previous_result = TaskResult.model_validate(
                        await store.get(previous_op.result_ref)
                    )
                    cached.extend(
                        a
                        for a in previous_result.audit_artifacts
                        if a.name.startswith("checkpoints/")
                    )
        bundle = ExecutionBundle(
            builder_protocol_version=2,
            workflow_version=session.preparation_version,
            queued_at=utcnow(),
            developer_model=session.developer_model,
            prompt_hash=session.prompt_hash,
            task_id=task.id,
            script=files.get("analysis.py", ""),
            operation_id=op_id,
            operation_phase=phase,
            source_hash=session.source_hash,
            source_files=[SourceFile(path=k, content=v) for k, v in sorted(files.items())],
            output_specs=session.output_specs,
            checkpoint_specs=session.checkpoint_specs,
            checkpoint_inputs=cached,
            runtime_image=session.runtime_image,
            command_timeout_seconds=timeout,
            verification=verification,
            interaction_revision=task.interaction_revision,
            contract=task.contract,
            resource_plan=task.resource_plan,
            tool_plan=task.tool_plan,
            parameter_manifest=effective_parameter_manifest(task.master_plan, task.decision_set),
            figure_specs=task.figure_specs,
            input_attachments=task.envelope.attachments,
            dataset_inputs=task.dataset_inspections,
            attempt=sequence,
        )
        op.bundle_ref = await store.put(bundle.model_dump(mode="json"))
        session.operations.append(op)
        session.reserved_seconds += timeout
        session.reserved_cost_usd += cost
        if phase == "run":
            session.full_runs += 1
        task.attempts = sequence  # transport sequence; full_runs is the scientific attempt counter
        task.status = TaskStatus.SUBMITTING
        task.current_stage = StageKind.EXECUTION if phase == "run" else StageKind.VALIDATION
        await self.save(task)
        return await self.dispatch(task, op, store)

    async def dispatch(
        self, task: TaskRecord, op: DevelopmentOperation, store: WorkspaceStore
    ) -> TaskRecord:
        bundle = ExecutionBundle.model_validate(await store.get(op.bundle_ref))
        op.submission_attempts += 1
        await self.save(task)
        callback = None
        if self.progress_callback is not None:

            async def forward(progress: TaskProgress) -> None:
                assert self.progress_callback is not None
                await self.progress_callback(task, progress)

            callback = forward
        if not isinstance(self.executor, BatchStatusBackend):
            task.status = TaskStatus.RUNNING
            await self.save(task)
        worker_session = await self.ensure_worker_session(task, op)

        async def execute_operation() -> TaskResult:
            if isinstance(self.executor, GcpBatchExecutor):
                return await self.executor.execute(
                    bundle, retry_index=op.infrastructure_retries, worker_session=worker_session
                )
            return await self.executor.execute(bundle, progress_callback=callback)

        session = task.development
        assert session
        reviewing = (
            session.preparation_version == 2
            and op.phase == "verify"
            and session.candidate_review_source != op.source_hash
        )
        if reviewing:
            if session.model_turns >= self.settings.development_max_turns:
                raise ValueError("review exceeds model-turn budget")
            session.model_turns += 1
            await self.save(task)
        try:
            if reviewing:
                payload = await self.payload(task, await store.get(session.source_ref), store)
                payload.update(
                    review_phase="candidate",
                    execution_evidence_required=False,
                    initial_effort=session.code_revisions == 1,
                )
                executed, reviewed = await asyncio.gather(
                    execute_operation(),
                    self.harness.review_candidate(task.id, task.envelope, payload),
                    return_exceptions=True,
                )
                if isinstance(executed, BaseException):
                    raise executed
                result = executed
                if isinstance(reviewed, BaseException):
                    # Retain execution; retry the review on completion, never relaunch the job.
                    session.feedback = "Candidate review interrupted; execution evidence retained."
                else:
                    await self.record_candidate_review(task, reviewed, store)
            elif isinstance(self.executor, GcpBatchExecutor):
                result = await self.executor.execute(
                    bundle, retry_index=op.infrastructure_retries, worker_session=worker_session
                )
            else:
                result = await self.executor.execute(bundle, progress_callback=callback)
        except Exception as exc:
            if op.submission_attempts >= 3:
                raise RuntimeError("operation submission failed after three calls") from exc
            task.status = TaskStatus.SUBMITTING
            task.error = (
                "Operation submission is temporarily unavailable; retrying the same revision."
            )
            return await self.save(task)
        if isinstance(self.executor, BatchStatusBackend) and result.exit_code is None:
            op.status = "queued"
            task.execution_job_name = result.output_text
            if worker_session:
                session.worker_session_job = result.output_text
            task.status = TaskStatus.QUEUED
        else:
            await self.accept(task, op, result, store)
        return await self.save(task)

    async def accept(
        self, task: TaskRecord, op: DevelopmentOperation, result: TaskResult, store: WorkspaceStore
    ) -> None:
        session = task.development
        assert session
        if result.operation_id != op.id or result.source_hash != op.source_hash:
            raise ValueError("operation result identity mismatch")
        if not result.publication_committed:
            raise ValueError("operation evidence has not been durably published")
        if op.result_ref:
            op.prior_result_refs.append(op.result_ref)
        op.result_ref = await store.put(result.model_dump(mode="json"))
        op.status = "completed"
        op.completed_at = utcnow()
        op.exit_code = result.exit_code
        op.summary = result.summary
        task.execution_job_name = None
        task.status = TaskStatus.PLANNING
        task.error = None
        session.feedback = result.summary
        if session.preparation_version == 2 and op.phase == "verify":
            if session.candidate_review_source != op.source_hash:
                if session.model_turns >= self.settings.development_max_turns:
                    raise ValueError("review budget exhausted; validation evidence retained")
                session.model_turns += 1
                await self.save(task)
                payload = await self.payload(task, await store.get(session.source_ref), store)
                payload.update(
                    review_phase="candidate",
                    execution_evidence_required=False,
                    initial_effort=session.code_revisions == 1,
                )
                reviewed = await self.harness.review_candidate(task.id, task.envelope, payload)
                await self.record_candidate_review(task, reviewed, store)
            review = session.candidate_review
            assert review
            session.feedback = "Consolidated validation: " + result.summary
            if review.issues:
                session.feedback += "\nReview findings: " + "; ".join(review.issues)
            if not review.approved or review.scientific_amendment_required:
                session.state = "developing"
                session.verified_hash = ""
                if op.source_hash == session.first_candidate_source_hash:
                    session.first_check_passed = False
                    session.first_candidate_outcome = "review_rejected"
                if self.needs_research_input(review):
                    session.state = "needs_input"
                    task.status = TaskStatus.NEEDS_INPUT
                    task.error = review.scientific_impact
                # Infrastructure failures still need their ordinary retry handling.
                if not result.failure or result.failure.category != "infrastructure":
                    return
        if result.failure and result.failure.category == "infrastructure":
            if op.infrastructure_retries >= 2:
                session.state = "failed"
                task.status = TaskStatus.FAILED
                task.error = "Worker infrastructure failed after three attempts; code was retained."
            elif (
                task.resource_plan
                and session.reserved_cost_usd + op.reserved_cost_usd
                <= task.resource_plan.estimated_cost_usd
            ):
                op.infrastructure_retries += 1
                op.submission_attempts = 0
                op.status = "reserved"
                session.reserved_cost_usd += op.reserved_cost_usd
                session.reserved_seconds += op.reserved_seconds
                task.status = TaskStatus.SUBMITTING
                task.error = "Retrying the same immutable operation after an infrastructure fault."
            else:
                session.state = "needs_input"
                task.status = TaskStatus.NEEDS_INPUT
                task.error = "Infrastructure retry would exceed the approved compute envelope."
            return
        if (
            session.first_check_passed is None
            and op.phase in {"test", "verify"}
            and op.source_hash == session.first_candidate_source_hash
        ):
            session.first_check_passed = result.exit_code == 0
            session.first_candidate_outcome = "passed" if result.exit_code == 0 else "tests_failed"
        if result.exit_code != 0:
            session.state = "developing"
            session.verified_hash = ""
            # Retain failed candidate artifacts internally, never put them on the delivery path.
            task.result = None
            if session.full_runs >= 2:
                session.state = "failed"
                task.status = TaskStatus.FAILED
                task.error = result.summary
        elif op.phase == "verify":
            if not result.verification or not result.verification.passed:
                raise ValueError("verification operation returned no passing report")
            session.verified_hash = op.source_hash
            session.output_contract_hash = digest(
                [x.model_dump(mode="json") for x in session.output_specs]
            )
            session.state = "verified"
        elif op.phase == "run":
            if (
                session.verified_hash != op.source_hash
                or not result.verification
                or not result.verification.passed
                or not result.artifacts
            ):
                raise ValueError("completion gate rejected unverified result")
            session.state = "reviewing_results"
            session.pending_result_ref = op.result_ref
            task.result = None
            task.status = TaskStatus.PLANNING

    async def ensure_worker_session(
        self, task: TaskRecord, op: DevelopmentOperation
    ) -> dict[str, Any] | None:
        session = task.development
        assert session and task.resource_plan
        if (
            session.preparation_version != 2
            or not session.worker_reuse_enabled
            or not isinstance(self.executor, GcpBatchExecutor)
        ):
            return None
        now = utcnow()
        alive = bool(
            session.worker_session_id
            and session.worker_session_deadline
            and session.worker_session_deadline > now
        )
        if alive and session.worker_session_job:
            from google.api_core.exceptions import NotFound

            try:
                state = await self.executor.get_job_status(session.worker_session_job)
                alive = state.state in {"QUEUED", "SCHEDULED", "RUNNING"}
            except NotFound:
                alive = False
        if not alive:
            if session.worker_session_started_at:
                # Conservatively include startup and idle time, never just process CPU time.
                session.worker_seconds_consumed += (
                    now - session.worker_session_started_at
                ).total_seconds()
            envelope = (
                2 * task.resource_plan.timeout_hours * 3600
                + self.settings.development_seconds
                + self.settings.development_max_operations * 300
            )
            remaining = envelope - session.worker_seconds_consumed
            if remaining < op.reserved_seconds + 300:
                raise ValueError("task worker wall-time budget exhausted")
            session.worker_session_id = digest([task.id, op.id, op.infrastructure_retries])[:32]
            session.worker_session_started_at = now
            session.worker_session_deadline = now + timedelta(seconds=remaining)
            session.worker_session_job = ""
            await self.save(task)
        assert session.worker_session_started_at and session.worker_session_deadline
        return {
            "session_id": session.worker_session_id,
            "epoch": int(session.worker_session_started_at.timestamp() * 1000000),
            "deadline": session.worker_session_deadline.isoformat(),
        }

    async def record_candidate_review(
        self, task: TaskRecord, review: CandidateReview, store: WorkspaceStore
    ) -> None:
        session = task.development
        assert session
        metrics = getattr(self.harness, "development_metrics", {}).get(task.id, {})
        session.model_input_tokens += int(metrics.get("input_tokens", 0))
        session.model_output_tokens += int(metrics.get("output_tokens", 0))
        session.review_refs.append(
            await store.put(
                {
                    "review": review.model_dump(mode="json"),
                    "metrics": metrics,
                    "phase": "candidate",
                    "source_hash": session.source_hash,
                    "acceptance_checklist": session.acceptance_checklist,
                    "authority_hash": session.authority_hash,
                    "runtime_image": session.runtime_image,
                }
            )
        )
        session.candidate_review = review
        session.candidate_review_source = session.source_hash
        if review.approved and not review.scientific_amendment_required:
            session.review_hash = session.source_hash

    async def artifact_evidence(
        self, artifacts: list[ArtifactRef]
    ) -> tuple[list[str], list[dict[str, str]]]:
        from pistachio.services.artifacts import prepare_individual_artifact

        images: list[str] = []
        text: list[dict[str, str]] = []
        total_image_bytes = 0
        for artifact in artifacts:
            if artifact.name.endswith(".zip"):
                continue
            if artifact.size_bytes > 4_000_000:
                text.append(
                    {"name": artifact.name, "note": "Too large for bounded model inspection."}
                )
                continue
            prepared = await prepare_individual_artifact(artifact, self.settings)
            try:
                path = prepared.files[0].path
                if (
                    artifact.media_type == "image/png"
                    and total_image_bytes + artifact.size_bytes <= 8_000_000
                ):
                    content = path.read_bytes()
                    images.append("data:image/png;base64," + base64.b64encode(content).decode())
                    total_image_bytes += len(content)
                    text.append({"name": artifact.name, "note": f"Image {len(images)} attached."})
                elif artifact.name.endswith((".json", ".csv", ".tsv", ".txt", ".log")):
                    text.append({"name": artifact.name, "content": path.read_text()[:6000]})
                else:
                    text.append(
                        {"name": artifact.name, "note": "Metadata only; no visual inspection."}
                    )
            finally:
                prepared.cleanup()
        return images, text

    async def review_results(self, task: TaskRecord, store: WorkspaceStore) -> TaskRecord:
        session = task.development
        assert session
        if session.model_turns >= self.settings.development_max_turns:
            raise ValueError("result review exceeds the model budget; results remain unverified")
        result = TaskResult.model_validate(await store.get(session.pending_result_ref))
        images, evidence = await self.artifact_evidence(result.artifacts)
        payload = await self.payload(task, await store.get(session.source_ref), store)
        payload["image_previews"] = images
        payload["actual_results"] = evidence
        payload["verification"] = (
            result.verification.model_dump(mode="json") if result.verification else None
        )
        session.model_turns += 1
        await self.save(task)
        review = await self.harness.review_results(task.id, task.envelope, payload)
        metrics = getattr(self.harness, "development_metrics", {}).get(task.id, {})
        session.model_input_tokens += int(metrics.get("input_tokens", 0))
        session.model_output_tokens += int(metrics.get("output_tokens", 0))
        session.review_refs.append(
            await store.put(
                {
                    "result_review": review.model_dump(mode="json"),
                    "metrics": metrics,
                    "operation_id": result.operation_id,
                }
            )
        )
        if review.approved and not review.scientific_amendment_required:
            session.state = "complete"
            task.result = result
            task.status = TaskStatus.SUCCEEDED
        else:
            session.feedback = "Result review: " + "; ".join(review.issues)
            scientific = self.needs_research_input(review)
            if not scientific:
                session.feedback += (
                    " Repair internally with the authorized standard method; package defaults "
                    "do not require user input."
                )
            session.state = "needs_input" if scientific else "developing"
            task.status = TaskStatus.NEEDS_INPUT if scientific else TaskStatus.PLANNING
            if session.full_runs >= 2 and session.state == "developing":
                session.state = "failed"
                task.status = TaskStatus.FAILED
            task.error = session.feedback if task.status != TaskStatus.PLANNING else None
        return await self.save(task)
