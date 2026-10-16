"""Deterministic approval and hard-limit policies."""

from __future__ import annotations

from mica.config import Settings
from mica.models import (
    ApprovalKind,
    ApprovalRequest,
    DecisionScope,
    MasterPlan,
    ParameterClass,
    ProvisioningModel,
    ResourcePlan,
    ResourceProfile,
    TaskContract,
    ToolPlan,
)


class PolicyViolation(ValueError):
    pass


class ExecutionPolicy:
    PROFILE_LIMITS = {
        ResourceProfile.CPU_SMALL: (2, 8, 0, 50, 2.0),
        ResourceProfile.CPU_MEDIUM: (8, 32, 0, 200, 6.0),
        ResourceProfile.MEMORY_LARGE: (16, 128, 0, 500, 12.0),
        ResourceProfile.GPU_L4: (8, 32, 1, 500, 12.0),
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def canonicalize_resource_plan(self, plan: ResourcePlan) -> ResourcePlan:
        """Apply the immutable infrastructure tuple for a selected named profile."""
        cpu, memory_gib, gpu_count, disk_gib, timeout_hours = self.PROFILE_LIMITS[plan.profile]
        return plan.model_copy(
            update={
                "cpu": cpu,
                "memory_gib": memory_gib,
                "gpu_count": gpu_count,
                "gpu_type": "L4" if gpu_count else None,
                "disk_gib": disk_gib,
                "timeout_hours": timeout_hours,
            }
        )

    def validate_resource_plan(self, plan: ResourcePlan) -> None:
        if plan.cpu > 64:
            raise PolicyViolation("v1 hard limit is 64 vCPU")
        if plan.memory_gib > 256:
            raise PolicyViolation("v1 hard limit is 256 GiB RAM")
        if plan.gpu_count > 1:
            raise PolicyViolation("v1 hard limit is one GPU")
        if plan.disk_gib > 1024:
            raise PolicyViolation("v1 hard limit is 1 TiB scratch disk")
        if plan.timeout_hours > 24:
            raise PolicyViolation("v1 hard limit is 24 hours")
        expected = self.PROFILE_LIMITS[plan.profile]
        actual = (plan.cpu, plan.memory_gib, plan.gpu_count, plan.disk_gib, plan.timeout_hours)
        if actual != expected:
            raise PolicyViolation(
                f"resource profile {plan.profile.value} must use cpu/memory/gpu/disk/time "
                f"values {expected}; got {actual}"
            )
        if plan.provisioning_model == ProvisioningModel.SPOT and not plan.checkpointable:
            raise PolicyViolation("Spot provisioning is allowed only for checkpointable jobs")

    def approvals_for(
        self,
        task_id: str,
        contract: TaskContract,
        resources: ResourcePlan,
        tools: ToolPlan,
    ) -> list[ApprovalRequest]:
        self.validate_resource_plan(resources)
        approvals: list[ApprovalRequest] = []
        if contract.missing_scientific_decisions:
            fields = [
                item.model_dump(mode="json") for item in contract.proposed_scientific_decisions
            ]
            if not fields:
                fields = [
                    {
                        "id": f"decision_{index}",
                        "label": question.rstrip("?"),
                        "proposed_value": "",
                        "options": [],
                        "rationale": "A current-task decision is required.",
                        "source": "agent question",
                        "confidence": 0.0,
                    }
                    for index, question in enumerate(contract.missing_scientific_decisions, start=1)
                ]
            return [
                ApprovalRequest(
                    task_id=task_id,
                    kind=ApprovalKind.SCIENTIFIC_STEP,
                    title="Confirm analysis settings",
                    detail=f"Review {len(fields)} proposed settings, edit any value, then confirm.",
                    payload={
                        "fields": fields,
                        "decisions": contract.missing_scientific_decisions,
                    },
                )
            ]
        approvals.extend(self._hyperparameter_approvals(task_id, contract, tools))
        cost_with_contingency = resources.estimated_cost_usd * (
            1 + self.settings.compute_cost_contingency
        )
        compute_reasons: list[str] = []
        if resources.gpu_count:
            compute_reasons.append("GPU requested")
        if resources.memory_gib > 64:
            compute_reasons.append("more than 64 GiB RAM requested")
        if resources.timeout_hours > 6:
            compute_reasons.append("runtime exceeds six hours")
        if cost_with_contingency > self.settings.compute_approval_usd:
            compute_reasons.append(
                f"projected compute cost with contingency is ${cost_with_contingency:.2f}"
            )
        if compute_reasons:
            approvals.append(
                ApprovalRequest(
                    task_id=task_id,
                    kind=ApprovalKind.COMPUTE,
                    title="Compute plan needs approval",
                    detail="; ".join(compute_reasons),
                    payload=resources.model_dump(mode="json"),
                )
            )
        if tools.proposed_tool_ids:
            approvals.append(
                ApprovalRequest(
                    task_id=task_id,
                    kind=ApprovalKind.TOOL_ADD,
                    title="New tool versions need approval",
                    detail=", ".join(tools.proposed_tool_ids),
                    payload={"tool_ids": tools.proposed_tool_ids},
                )
            )
        return approvals

    def approvals_for_master_plan(self, task_id: str, plan: MasterPlan) -> list[ApprovalRequest]:
        """Create one compact approval for interpretation-changing settings only."""
        fields = [
            parameter.model_dump(mode="json")
            for parameter in plan.parameters
            if parameter.classification
            in {ParameterClass.SCIENTIFIC, ParameterClass.VISUAL_SEMANTIC}
            and parameter.requires_approval
            and not parameter.confirmed_by_user
            and parameter.decision_scope == DecisionScope.RESEARCH_DESIGN
        ]
        if not fields:
            return []
        scientific = sum(
            field["classification"] == ParameterClass.SCIENTIFIC.value for field in fields
        )
        semantic = len(fields) - scientific
        detail_parts = []
        if scientific:
            detail_parts.append(f"{scientific} scientific setting(s)")
        if semantic:
            detail_parts.append(f"{semantic} interpretation-changing figure setting(s)")
        return [
            ApprovalRequest(
                task_id=task_id,
                kind=ApprovalKind.SCIENTIFIC_STEP,
                title="Confirm analysis settings",
                detail=f"Review {' and '.join(detail_parts)}. Cosmetic settings are automatic.",
                payload={
                    "master_plan_id": plan.id,
                    "master_plan_revision": plan.revision,
                    "parameter_ids": [str(field["id"]) for field in fields],
                    "fields": fields,
                },
            )
        ]

    def compute_approval_for(self, task_id: str, resources: ResourcePlan) -> ApprovalRequest | None:
        self.validate_resource_plan(resources)
        cost_with_contingency = resources.estimated_cost_usd * (
            1 + self.settings.compute_cost_contingency
        )
        reasons: list[str] = []
        if resources.gpu_count:
            reasons.append("GPU requested")
        if resources.memory_gib > 64:
            reasons.append("more than 64 GiB RAM requested")
        if resources.timeout_hours > 6:
            reasons.append("runtime exceeds six hours")
        if cost_with_contingency > self.settings.compute_approval_usd:
            reasons.append(
                f"projected compute cost with contingency is ${cost_with_contingency:.2f}"
            )
        if not reasons:
            return None
        return ApprovalRequest(
            task_id=task_id,
            kind=ApprovalKind.COMPUTE,
            title="Compute plan needs approval",
            detail="; ".join(reasons),
            payload=resources.model_dump(mode="json"),
        )

    @staticmethod
    def _hyperparameter_approvals(
        task_id: str, contract: TaskContract, tools: ToolPlan
    ) -> list[ApprovalRequest]:
        if not contract.requires_execution:
            return []
        selected = set(tools.approved_tool_ids) | set(tools.proposed_tool_ids)
        described = {invocation.tool_id for invocation in tools.invocations}
        missing = sorted(selected - described)
        planning_issues: list[str] = [
            f"{tool_id}: the plan omitted its required hyperparameter review."
            for tool_id in missing
        ]
        user_questions: list[str] = []
        parameter_payload: list[dict[str, object]] = []
        for invocation in tools.invocations:
            if invocation.tool_id not in selected:
                raise PolicyViolation(
                    f"tool invocation {invocation.tool_id} is not present in approved_tool_ids"
                )
            if not invocation.version.strip() or not invocation.entrypoint.strip():
                raise PolicyViolation("tool invocations require an exact version and entrypoint")
            if not invocation.review_complete:
                planning_issues.append(
                    f"{invocation.tool_name} {invocation.version}: hyperparameter review is "
                    "incomplete; inspect the exact version before execution."
                )
            if not invocation.review_evidence:
                planning_issues.append(
                    f"{invocation.tool_name} {invocation.version}: provide primary documentation "
                    "or source evidence for the hyperparameter review."
                )
            if (
                invocation.review_complete
                and not invocation.hyperparameters
                and not invocation.no_tunable_hyperparameters_reason.strip()
            ):
                planning_issues.append(
                    f"{invocation.tool_name} {invocation.version}: confirm whether the exact "
                    "entrypoint has tunable hyperparameters."
                )
            for parameter in invocation.hyperparameters:
                if parameter.tunable and not parameter.scientific_impact.strip():
                    planning_issues.append(
                        f"{invocation.tool_name}.{parameter.name}: explain how this tunable "
                        "parameter can affect the result."
                    )
                if (
                    parameter.requires_user_confirmation
                    and not parameter.confirmed_by_user
                    and parameter.decision_scope == DecisionScope.RESEARCH_DESIGN
                ):
                    question = parameter.user_question.strip() or (
                        f"Choose {invocation.tool_name}.{parameter.name}; proposed value is "
                        f"{parameter.value!r} and tool default is {parameter.default_value!r}."
                    )
                    user_questions.append(question)
                    parameter_payload.append(
                        {
                            "id": f"{invocation.tool_id}.{parameter.name}",
                            "label": f"{invocation.tool_name}: {parameter.name}",
                            "tool_id": invocation.tool_id,
                            "tool_name": invocation.tool_name,
                            "version": invocation.version,
                            "name": parameter.name,
                            "proposed_value": parameter.value,
                            "default_value": parameter.default_value,
                            "scientific_impact": parameter.scientific_impact,
                        }
                    )
        if not planning_issues and not user_questions:
            return []
        return [
            ApprovalRequest(
                task_id=task_id,
                kind=ApprovalKind.HYPERPARAMETER,
                title="Tool hyperparameters need confirmation",
                detail=f"Review {len(parameter_payload)} tool settings before execution.",
                payload={
                    "parameters": parameter_payload,
                    "fields": parameter_payload,
                    "tool_ids": sorted(selected),
                    "questions": list(dict.fromkeys(planning_issues + user_questions)),
                    "planning_issues": list(dict.fromkeys(planning_issues)),
                },
            )
        ]
