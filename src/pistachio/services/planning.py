"""Deterministic downstream planning for planner version 2."""

from __future__ import annotations

import re

from pistachio.models import (
    DatasetInspection,
    DecisionScope,
    DecisionSet,
    HyperparameterSetting,
    HyperparameterSource,
    MasterPlan,
    ParameterClass,
    ParameterSpec,
    ProvisioningModel,
    ResourcePlan,
    ResourceProfile,
    ToolInvocation,
    ToolPlan,
    ToolRecord,
    ToolStatus,
)


def _normal(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _user_confirmed(spec: ParameterSpec, decisions: DecisionSet) -> bool:
    return spec.confirmed_by_user or (
        spec.decision_scope == DecisionScope.RESEARCH_DESIGN
        and spec.id in decisions.values
        and decisions.approved_by is not None
        and not decisions.approved_by.startswith("system:")
    )


def resolve_registered_tools(
    master_plan: MasterPlan,
    decisions: DecisionSet,
    records: list[ToolRecord],
) -> tuple[ToolPlan, list[str], list[ToolRecord]]:
    """Resolve exact registry entries without a model call or web search."""
    by_key: dict[str, ToolRecord] = {}
    for record in records:
        for key in {record.id, record.name, record.source_url.rsplit("/", 1)[-1]}:
            by_key[_normal(key)] = record

    resolved: list[ToolRecord] = []
    unknown: list[str] = []
    for requested in master_plan.primary_tool_ids:
        matched = by_key.get(_normal(requested))
        if matched is None:
            unknown.append(requested)
        elif matched not in resolved:
            resolved.append(matched)

    invocations: list[ToolInvocation] = []
    for record in resolved:
        parameters = []
        for spec in master_plan.parameters:
            if spec.tool_id and _normal(spec.tool_id) not in {
                _normal(record.id),
                _normal(record.name),
            }:
                continue
            if (
                spec.component_id
                not in {
                    step.id
                    for step in master_plan.steps
                    if record.id in step.primary_tool_ids or record.name in step.primary_tool_ids
                }
                and spec.tool_id == ""
            ):
                continue
            value = decisions.values.get(spec.id, spec.proposed_value)
            parameters.append(
                HyperparameterSetting(
                    name=spec.id,
                    value=value,
                    default_value=spec.default_value,
                    source=(
                        HyperparameterSource.USER_SPECIFIED
                        if _user_confirmed(spec, decisions)
                        else HyperparameterSource.TOOL_DEFAULT
                        if spec.decision_scope == DecisionScope.METHOD_DEFAULT
                        else HyperparameterSource.AGENT_RECOMMENDATION
                    ),
                    tunable=spec.classification
                    in {ParameterClass.SCIENTIFIC, ParameterClass.VISUAL_SEMANTIC},
                    scientific_impact=spec.scientific_impact,
                    requires_user_confirmation=(
                        spec.requires_approval
                        and spec.decision_scope == DecisionScope.RESEARCH_DESIGN
                    ),
                    confirmed_by_user=_user_confirmed(spec, decisions),
                    decision_scope=spec.decision_scope,
                )
            )
        invocations.append(
            ToolInvocation(
                tool_id=record.id,
                tool_name=record.name,
                version=record.version,
                commit_sha=record.commit_sha,
                entrypoint=record.install_recipe,
                hyperparameters=parameters,
                review_complete=True,
                review_evidence=[record.provenance, record.source_url],
                no_tunable_hyperparameters_reason=(
                    "No primary-procedure parameters are assigned to this tool."
                    if not parameters
                    else ""
                ),
            )
        )

    available = [item for item in resolved if item.status == ToolStatus.AVAILABLE]
    unavailable = [item for item in resolved if item.status != ToolStatus.AVAILABLE]
    return (
        ToolPlan(
            approved_tool_ids=[item.id for item in available],
            proposed_tool_ids=[item.id for item in unavailable],
            invocations=invocations,
            rationale="Exact pinned registry lookup; no web search was used.",
        ),
        unknown,
        unavailable,
    )


def deterministic_resource_plan(
    inspections: list[DatasetInspection],
    tool_records: list[ToolRecord],
    master_plan: MasterPlan,
) -> ResourcePlan:
    """Map verified dataset metadata and registry hints onto fixed profiles."""
    total_bytes = sum(max(item.size_bytes, 0) for item in inspections)
    max_obs = max((item.n_obs or 0 for item in inspections), default=0)
    hints = {
        _normal(hint.name): hint.value.lower()
        for record in tool_records
        for hint in record.resource_hints
    }
    request_text = " ".join(
        [master_plan.contract.objective, *(step.description for step in master_plan.steps)]
    ).lower()
    needs_gpu = any("gpu" in value for value in hints.values()) or "requires gpu" in request_text
    needs_large_memory = (
        total_bytes >= 8 * 1024**3
        or max_obs >= 1_000_000
        or any("128" in value for value in hints.values())
    )
    needs_medium = total_bytes >= 1024**3 or max_obs >= 100_000
    if needs_gpu:
        profile = ResourceProfile.GPU_L4
        cost = 20.0
    elif needs_large_memory:
        profile = ResourceProfile.MEMORY_LARGE
        cost = 15.0
    elif needs_medium:
        profile = ResourceProfile.CPU_MEDIUM
        cost = 5.0
    else:
        profile = ResourceProfile.CPU_SMALL
        cost = 1.0
    checkpointable = "checkpointable" in request_text
    return ResourcePlan(
        profile=profile,
        checkpointable=checkpointable,
        provisioning_model=(
            ProvisioningModel.SPOT if checkpointable else ProvisioningModel.STANDARD
        ),
        estimated_cost_usd=cost,
        rationale=(
            f"Deterministic profile from {total_bytes} input byte(s), {max_obs} maximum rows, "
            "and pinned tool resource hints."
        ),
    )


def effective_parameter_manifest(
    master_plan: MasterPlan, decisions: DecisionSet
) -> list[ParameterSpec]:
    """Return the complete effective configuration without mutating the master plan."""
    return [
        parameter.model_copy(
            update={
                "proposed_value": decisions.values.get(parameter.id, parameter.proposed_value),
                "confirmed_by_user": _user_confirmed(parameter, decisions),
            }
        )
        for parameter in master_plan.parameters
    ]
