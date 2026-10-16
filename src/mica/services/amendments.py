"""Apply bounded follow-up edits to durable task-local planning authority."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal

from mica.models import (
    AnalysisStep,
    DecisionScope,
    FigureIntent,
    MasterPlan,
    MasterPlanAmendment,
    ParameterClass,
    ParameterSpec,
    PlanHandoff,
    new_id,
    utcnow,
)
from mica.services.planning import effective_parameter_manifest


class AmendmentNeedsInput(Exception):
    """A reply cannot be bound to a specific proposal without clarification."""


AMENDMENT_FAILURE_MESSAGE = (
    "I have your reply, but I hit an internal problem updating the plan and couldn’t continue. "
    "You don’t need to reformat or repeat your choices."
)


class AmendmentRepairExhausted(Exception):
    """Internal diagnostics stay in the stage audit, never become a user formatting request."""


def is_internal_amendment_error(text: str) -> bool:
    return bool(re.search(
        r"\bevidence_refs\b|\b(?:evidence|citation)[ _-](?:ids?|refs?|references)\b|"
        r"\b(?:amendment|validation).{0,60}\b(?:schema|payload|format|json|evidence)\b|"
        r"\b(?:reformat|resubmit).{0,60}\b(?:reply|settings|json|schema)\b|"
        r"\b(?:verbatim|quot\w*).{0,70}\b(?:exchange|message|evidence)\b|"
        r"\bevidence.{0,70}\b(?:quot\w*|reference|format)\b",
        text, re.IGNORECASE,
    ))


def amendment_sources(handoff: PlanHandoff) -> dict[str, tuple[str, str]]:
    sources = {item.id: ("user", item.message) for item in handoff.followups}
    sources.update({
        item.id: ("assistant", item.text)
        for item in handoff.conversation if item.role == "assistant"
    })
    if handoff.pending_question:
        sources["pending_question"] = ("assistant", handoff.pending_question)
    if handoff.master_plan:
        sources[handoff.master_plan.id] = ("plan", handoff.master_plan.model_dump_json())
    return sources


def _quote_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(?<=\d)[,_](?=\d{3}(?:\D|$))", "", text)
    return re.sub(r"[^\w]+", " ", text).strip()


def _merge[Component: (ParameterSpec, AnalysisStep, FigureIntent)](
    existing: list[Component], updates: list[Component], removed: list[str]
) -> list[Component]:
    old_ids = {item.id for item in existing}
    update_ids = [item.id for item in updates]
    if len(set(update_ids)) != len(update_ids) or len(set(removed)) != len(removed):
        raise ValueError("amendment contains duplicate component IDs")
    if set(removed) - old_ids or set(removed) & set(update_ids):
        raise ValueError("amendment removes an unknown or simultaneously updated component")
    replacements = {item.id: item for item in updates}
    return [
        replacements.get(item.id, item).model_copy(deep=True)
        for item in existing if item.id not in removed
    ] + [item.model_copy(deep=True) for item in updates if item.id not in old_ids]


def _numbers(text: str) -> set[Decimal]:
    """Compare values across ordinary numeric notation, including English number words."""
    text = unicodedata.normalize("NFKC", text).casefold()
    text = re.sub(r"(?<=\d)[,_ ](?=\d{3}(?:\D|$))", "", text)
    scales = {"k": 1000, "thousand": 1000, "million": 1_000_000}
    numbers = {
        Decimal(number) * scales.get(suffix.strip(), 1)
        for number, suffix in re.findall(
            r"(?<![\w.])(-?\d+(?:\.\d+)?(?:e[+-]?\d+)?)"
            r"(\s*(?:k|thousand|million))?(?![\w.])", text
        )
    }
    words = dict(zip(
        ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
         "nineteen"], range(20), strict=True,
    ))
    words.update(dict(zip(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"],
        range(20, 100, 10), strict=True,
    )))
    group = total = 0
    active = False
    for word in [*re.findall(r"[a-z]+", text), "end"]:
        if word in words:
            group += words[word]
            active = True
        elif word == "hundred" and active:
            group *= 100
        elif word in {"thousand", "million"} and active:
            total += group * scales[word]
            group = 0
        elif word == "and" and active:
            continue
        else:
            if active:
                numbers.add(Decimal(total + group))
            group = total = 0
            active = False
    return numbers


def apply_plan_amendment(handoff: PlanHandoff, amendment: MasterPlanAmendment) -> MasterPlan:
    """Preserve every omitted component, including values edited in an approval form."""
    if handoff.master_plan is None:
        raise ValueError("an amendment requires a saved master plan")
    if amendment.clarification_question.strip():
        if is_internal_amendment_error(amendment.clarification_question):
            raise ValueError("Resolve internal evidence/formatting problems without user input")
        raise AmendmentNeedsInput(amendment.clarification_question.strip())
    plan = handoff.master_plan.model_copy(deep=True)
    if handoff.contract is not None:
        plan.contract = handoff.contract.model_copy(deep=True)
    if handoff.decision_set is not None:
        if (
            handoff.decision_set.master_plan_id != plan.id
            or handoff.decision_set.master_plan_revision != plan.revision
        ):
            raise ValueError("handoff decisions belong to a different master plan")
        plan.parameters = effective_parameter_manifest(plan, handoff.decision_set)
    prior = {item.id: item for item in plan.parameters}
    available = amendment_sources(handoff)
    for edit in amendment.parameters:
        spec = edit.parameter
        if edit.evidence_refs:
            if any(ref not in available for ref in edit.evidence_refs):
                raise ValueError(f"{spec.id}: use existing message IDs for evidence_refs")
            sources = [available[ref] for ref in dict.fromkeys(edit.evidence_refs)]
        else:
            # Read older outputs tolerantly; new outputs cite saved IDs instead of copying quotes.
            role = "assistant" if edit.basis == "accepted_proposal" else "user"
            quote = _quote_key(edit.evidence)
            sources = [
                (kind, text) for kind, text in available.values()
                if kind == role and quote and quote in _quote_key(text)
            ]
            if edit.basis == "accepted_proposal" and sources:
                sources.extend(("user", item.message) for item in handoff.followups)
        roles = {role for role, _ in sources}
        required = {"user", "assistant"} if edit.basis == "accepted_proposal" else {"user"}
        if edit.basis == "automatic_choice":
            required = set() if "plan" in roles else {"user"}
        if not sources or not required.issubset(roles):
            raise ValueError(f"{spec.id}: reference the relevant saved user/proposal messages")
        evidence = "\n".join(text for _, text in sources)
        previous = prior.get(spec.id)
        changed = previous is None or previous.proposed_value != spec.proposed_value
        if edit.basis == "new_proposal":
            if spec.confirmed_by_user or (
                spec.classification in {ParameterClass.SCIENTIFIC, ParameterClass.VISUAL_SEMANTIC}
                and (
                    not spec.requires_approval
                    or spec.decision_scope != DecisionScope.RESEARCH_DESIGN
                )
            ):
                raise ValueError(f"{spec.id}: a new scientific proposal requires approval")
        elif edit.basis in {"automatic_choice", "delegated_choice"}:
            if (
                edit.basis == "automatic_choice" and previous
                and previous.confirmed_by_user and changed
            ):
                raise ValueError(f"{spec.id}: automatic choices cannot override approved values")
            if (
                spec.decision_scope == DecisionScope.RESEARCH_DESIGN
                and edit.basis == "automatic_choice"
            ):
                raise ValueError(f"{spec.id}: unresolved research choices still require user input")
            spec = spec.model_copy(update={
                "requires_approval": False, "confirmed_by_user": False,
                "decision_scope": (
                    DecisionScope.IMPLEMENTATION if edit.basis == "delegated_choice"
                    else spec.decision_scope
                ),
            })
        elif changed and not _numbers(str(spec.proposed_value)).issubset(_numbers(evidence)):
            raise ValueError(f"{spec.id}: effective value does not match the quoted proposal")
        if previous and previous.confirmed_by_user and not changed:
            spec = spec.model_copy(update={"confirmed_by_user": True})
        spec = spec.model_copy(update={"source": f"Current-task {edit.basis}: {evidence}"})
        # Keep the submitted patch immutable; replacements below use copies.
        prior[spec.id] = spec

    plan.parameters = _merge(
        plan.parameters, [prior[item.parameter.id] for item in amendment.parameters],
        amendment.remove_parameter_ids,
    )
    plan.steps = _merge(plan.steps, amendment.steps, amendment.remove_step_ids)
    plan.figure_intents = _merge(
        plan.figure_intents, amendment.figure_intents, amendment.remove_figure_ids
    )
    removed = set(amendment.remove_parameter_ids)
    if any(removed.intersection(step.parameter_ids) for step in plan.steps) or any(
        removed.intersection(figure.semantic_parameter_ids) for figure in plan.figure_intents
    ):
        raise ValueError("removed parameters still have dependent steps or figures")
    if amendment.contract is not None:
        plan.contract = amendment.contract.model_copy(deep=True)
    if amendment.primary_tool_ids is not None:
        plan.primary_tool_ids = list(amendment.primary_tool_ids)
    plan.id = new_id("plan")
    plan.revision += 1
    plan.created_at = utcnow()
    plan.input_hash = ""
    plan.response = amendment.response
    plan.amendment_explanation = amendment.amendment_explanation
    return plan
