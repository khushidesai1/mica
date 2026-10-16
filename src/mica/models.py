"""Shared domain models and API contracts."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HyperparameterValue = (
    str | int | float | bool | list[str] | list[int] | list[float] | list[bool] | None
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


_TASK_NAME_ADJECTIVES = (
    "amber",
    "brisk",
    "calm",
    "clear",
    "coral",
    "gentle",
    "golden",
    "lively",
    "lucid",
    "mellow",
    "quiet",
    "silver",
    "steady",
    "sunny",
    "vivid",
    "warm",
)
_TASK_NAME_NOUNS = (
    "cedar",
    "comet",
    "finch",
    "forest",
    "harbor",
    "heron",
    "maple",
    "meadow",
    "orchid",
    "otter",
    "river",
    "sparrow",
    "summit",
    "willow",
    "wren",
    "zephyr",
)


def friendly_task_name(task_id: str) -> str:
    """Return a stable, non-identifying display name for an internal task ID."""
    digest = hashlib.blake2s(task_id.encode(), digest_size=8).digest()
    adjective = _TASK_NAME_ADJECTIVES[digest[0] % len(_TASK_NAME_ADJECTIVES)]
    noun = _TASK_NAME_NOUNS[digest[1] % len(_TASK_NAME_NOUNS)]
    suffix = int.from_bytes(digest[2:5]) % 10_000
    return f"{adjective}-{noun}-{suffix:04d}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TaskStatus(StrEnum):
    RECEIVED = "RECEIVED"
    NEEDS_INPUT = "NEEDS_INPUT"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    PLANNING = "PLANNING"
    SUBMITTING = "SUBMITTING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ProgressStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INFO = "info"


class TaskSource(StrEnum):
    WEB = "web"
    SLACK = "slack"
    EMAIL = "email"
    INTERNAL = "internal"


class DeliveryRoute(StrictModel):
    """A user-facing channel address, independent from where a task was submitted."""

    project_id: str
    principal_id: str
    source: TaskSource
    conversation_id: str
    thread_id: str = ""
    expires_at: datetime | None = None


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    CANDIDATE = "candidate"
    SUPERSEDED = "superseded"
    PINNED = "pinned"
    INACTIVE = "inactive"


class MemoryKind(StrEnum):
    USER_PREFERENCE = "user_preference"
    USER_CORRECTION = "user_correction"
    DATASET_FACT = "dataset_fact"
    TOOL_FACT = "tool_fact"
    PROJECT_CONVENTION = "project_convention"
    RESULT_POINTER = "result_pointer"
    PENDING_ITEM = "pending_item"
    INFERRED_CANDIDATE = "inferred_candidate"


class ApprovalKind(StrEnum):
    SCIENTIFIC_STEP = "SCIENTIFIC_STEP"
    HYPERPARAMETER = "HYPERPARAMETER"
    TOOL_ADD = "TOOL_ADD"
    COMPUTE = "COMPUTE"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ResourceProfile(StrEnum):
    CPU_SMALL = "cpu-small"
    CPU_MEDIUM = "cpu-medium"
    MEMORY_LARGE = "memory-large"
    GPU_L4 = "gpu-l4"


class ProvisioningModel(StrEnum):
    STANDARD = "STANDARD"
    SPOT = "SPOT"


class ToolStatus(StrEnum):
    CATALOGED = "cataloged"
    PROPOSED = "proposed"
    APPROVED = "approved"
    BUILDING = "building"
    AVAILABLE = "available"
    FAILED = "failed"


class HyperparameterSource(StrEnum):
    USER_SPECIFIED = "user_specified"
    TOOL_DEFAULT = "tool_default"
    AGENT_RECOMMENDATION = "agent_recommendation"
    FIXED_BY_TOOL = "fixed_by_tool"


class ParameterClass(StrEnum):
    SCIENTIFIC = "SCIENTIFIC"
    VISUAL_SEMANTIC = "VISUAL_SEMANTIC"
    PRESENTATION = "PRESENTATION"
    TECHNICAL = "TECHNICAL"


class DecisionScope(StrEnum):
    IMPLEMENTATION = "implementation"
    METHOD_DEFAULT = "method_default"
    RESEARCH_DESIGN = "research_design"


class StageKind(StrEnum):
    DATASET_INSPECTION = "DATASET_INSPECTION"
    MASTER_PLAN = "MASTER_PLAN"
    USER_DECISIONS = "USER_DECISIONS"
    TOOL_RESOLUTION = "TOOL_RESOLUTION"
    RESOURCE_PLAN = "RESOURCE_PLAN"
    FIGURE_PLAN = "FIGURE_PLAN"
    CODE_GENERATION = "CODE_GENERATION"
    VALIDATION = "VALIDATION"
    EXECUTION = "EXECUTION"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INVALIDATED = "INVALIDATED"


class MemoryPolicy(StrictModel):
    compact_after_tasks: int = 5
    compact_after_tokens: int = 60_000
    idle_after_days: int = 14
    max_retrieved_items: int = 8
    max_retrieved_tokens: int = 8_000


class Project(StrictModel):
    id: str = Field(default_factory=lambda: new_id("prj"))
    slug: str
    name: str
    owner: str
    members: set[str] = Field(default_factory=set)
    status: ProjectStatus = ProjectStatus.ACTIVE
    memory_policy: MemoryPolicy = Field(default_factory=MemoryPolicy)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_active_at: datetime = Field(default_factory=utcnow)

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        clean = value.strip().lower()
        if not clean or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789-" for ch in clean):
            raise ValueError("slug must contain lowercase letters, digits, or hyphens")
        return clean

    def can_access(self, principal: str) -> bool:
        principal = principal.lower()
        return principal == self.owner.lower() or principal in {m.lower() for m in self.members}


class AttachmentRef(StrictModel):
    id: str = Field(default_factory=lambda: new_id("att"))
    filename: str
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    uri: str
    checksum_sha256: str = ""
    generation: str = ""
    quarantined: bool = True
    authorization_tag: str = ""


class UserProvidedSetting(StrictModel):
    """One value the user explicitly supplied; never an inferred recommendation."""

    label: str
    value: str
    evidence: str = ""


class StructuredUserRequest(StrictModel):
    """Loss-minimized handoff from the conversation agent to the scientific query agent."""

    objective: str
    requested_outputs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    explicit_settings: list[UserProvidedSetting] = Field(default_factory=list)
    delegated_choices: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    original_message: str


class TaskEnvelope(StrictModel):
    project_id: str
    source: TaskSource = TaskSource.WEB
    external_event_id: str = Field(default_factory=lambda: new_id("evt"))
    conversation_id: str
    thread_id: str = ""
    principal_id: str
    prompt: str = Field(min_length=1, max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list)


class TaskSubmission(StrictModel):
    project_id: str
    source: TaskSource = TaskSource.WEB
    external_event_id: str = Field(default_factory=lambda: new_id("evt"))
    conversation_id: str = Field(default_factory=lambda: new_id("conv"))
    prompt: str = Field(min_length=1, max_length=100_000)
    attachments: list[AttachmentRef] = Field(default_factory=list)


class TaskContextSubmission(StrictModel):
    """A follow-up message added while a task is still being planned."""

    message: str = Field(min_length=1, max_length=20_000)
    external_event_id: str = Field(default_factory=lambda: new_id("evt"))

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class TaskContextUpdate(StrictModel):
    id: str = Field(default_factory=lambda: new_id("context"))
    message: str = Field(min_length=1, max_length=20_000)
    principal_id: str
    source: TaskSource
    external_event_id: str
    created_at: datetime = Field(default_factory=utcnow)


class ScientificDecisionProposal(StrictModel):
    """One editable, unapproved scientific choice proposed from verified metadata."""

    id: str
    label: str
    proposed_value: str = ""
    options: list[str] = Field(default_factory=list)
    rationale: str = ""
    source: str = "agent recommendation"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class TaskContract(StrictModel):
    objective: str
    requested_outputs: list[str] = Field(default_factory=list)
    immutable_inputs: list[str] = Field(default_factory=list)
    authorized_scientific_actions: list[str] = Field(default_factory=list)
    missing_scientific_decisions: list[str] = Field(default_factory=list)
    proposed_scientific_decisions: list[ScientificDecisionProposal] = Field(default_factory=list)
    prohibited_extensions: list[str] = Field(default_factory=list)
    requires_execution: bool = False


class ParameterSpec(StrictModel):
    """One effective setting, classified by whether it requires current-task consent."""

    id: str
    label: str
    classification: ParameterClass
    component_id: str
    tool_id: str = ""
    proposed_value: HyperparameterValue = None
    default_value: HyperparameterValue = None
    options: list[str] = Field(default_factory=list)
    rationale: str = ""
    source: str = "agent recommendation"
    scientific_impact: str = ""
    depends_on: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    confirmed_by_user: bool = False
    # Omit the legacy scope to preserve persisted plan/authority hashes on upgrade.
    decision_scope: DecisionScope = Field(
        default=DecisionScope.RESEARCH_DESIGN,
        exclude_if=lambda value: value == DecisionScope.RESEARCH_DESIGN,
    )


class AnalysisStep(StrictModel):
    id: str
    title: str
    description: str
    primary_tool_ids: list[str] = Field(default_factory=list)
    parameter_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class FigureIntent(StrictModel):
    id: str
    title: str
    kind: str
    purpose: str
    semantic_parameter_ids: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class MasterPlan(StrictModel):
    id: str = Field(default_factory=lambda: new_id("plan"))
    task_id: str = ""
    revision: int = 1
    contract: TaskContract
    steps: list[AnalysisStep] = Field(default_factory=list)
    primary_tool_ids: list[str] = Field(default_factory=list)
    parameters: list[ParameterSpec] = Field(default_factory=list)
    figure_intents: list[FigureIntent] = Field(default_factory=list)
    response: str = ""
    amendment_explanation: str = ""
    input_hash: str = ""
    model: str = "gpt-5.6-sol"
    created_at: datetime = Field(default_factory=utcnow)


class DecisionSet(StrictModel):
    id: str = Field(default_factory=lambda: new_id("dec"))
    task_id: str
    master_plan_id: str
    master_plan_revision: int
    revision: int = 1
    values: dict[str, HyperparameterValue] = Field(default_factory=dict)
    approved_by: str | None = None
    approved_at: datetime | None = None


class ParameterAmendment(StrictModel):
    parameter: ParameterSpec
    # References bind provenance in code; the model does not have to copy a user's wording.
    evidence_refs: list[str] = Field(default_factory=list)
    evidence: str = ""  # Compatibility with earlier structured outputs.
    basis: Literal[
        "user_request", "accepted_proposal", "new_proposal", "delegated_choice", "automatic_choice"
    ]


class MasterPlanAmendment(StrictModel):
    """A sparse edit to a saved plan; omitted components retain their effective values."""

    parameters: list[ParameterAmendment] = Field(default_factory=list)
    steps: list[AnalysisStep] = Field(default_factory=list)
    figure_intents: list[FigureIntent] = Field(default_factory=list)
    remove_parameter_ids: list[str] = Field(default_factory=list)
    remove_step_ids: list[str] = Field(default_factory=list)
    remove_figure_ids: list[str] = Field(default_factory=list)
    contract: TaskContract | None = None
    primary_tool_ids: list[str] | None = None
    clarification_question: str = ""
    response: str
    amendment_explanation: str


class FigureSetting(StrictModel):
    """One versioned figure setting with an explicit, strict schema."""

    id: str
    value: HyperparameterValue = None
    rationale: str = ""
    source: str = "figure planner"


class FigureSpec(StrictModel):
    id: str
    title: str
    kind: str
    semantic: list[FigureSetting] = Field(default_factory=list)
    presentation: list[FigureSetting] = Field(default_factory=list)
    rationale: str = ""
    qa_checks: list[str] = Field(default_factory=list)
    version: int = 1

    @field_validator("semantic", "presentation", mode="before")
    @classmethod
    def migrate_setting_maps(cls, value: Any) -> Any:
        """Load figure specs persisted before settings became strict records."""
        if isinstance(value, dict):
            return [
                {
                    "id": setting_id,
                    "value": setting_value,
                    "source": "legacy figure plan",
                }
                for setting_id, setting_value in value.items()
            ]
        return value


class FigurePlan(StrictModel):
    figures: list[FigureSpec] = Field(default_factory=list)
    response: str = ""


class CodeBuildResult(StrictModel):
    script: str
    runtime_tool_ids: list[str] = Field(default_factory=list)
    response: str = ""


class TaskStage(StrictModel):
    id: str = Field(default_factory=lambda: new_id("stage"))
    kind: StageKind
    status: StageStatus = StageStatus.PENDING
    version: int = 1
    depends_on: list[StageKind] = Field(default_factory=list)
    input_hash: str = ""
    output_hash: str = ""
    output_ref: str = ""
    model: str = ""
    reasoning_effort: str = ""
    attempts: int = 0
    metrics: dict[str, str | int | float | bool] = Field(default_factory=dict)
    error: str = ""
    started_at: datetime | None = None
    completed_at: datetime | None = None


class DatasetField(StrictModel):
    name: str
    kind: str
    shape: list[int] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)


class DatasetInspection(StrictModel):
    uri: str
    generation: str = ""
    size_bytes: int = 0
    checksum: str = ""
    format: str = "h5ad"
    n_obs: int | None = None
    obs_fields: list[DatasetField] = Field(default_factory=list)
    obsm_fields: list[DatasetField] = Field(default_factory=list)
    suggested_sample_key: str = ""
    suggested_annotation_key: str = ""
    suggested_spatial_key: str = ""
    inspection_error: str = ""


class ResourcePlan(StrictModel):
    profile: ResourceProfile = ResourceProfile.CPU_SMALL
    cpu: int = 2
    memory_gib: int = 8
    gpu_count: int = 0
    gpu_type: str | None = None
    disk_gib: int = 50
    timeout_hours: float = 2.0
    checkpointable: bool = False
    provisioning_model: ProvisioningModel = ProvisioningModel.STANDARD
    region: str = "us-central1"
    estimated_cost_usd: float = 0.0
    rationale: str = "Small CPU task."


class HyperparameterSetting(StrictModel):
    """One output-affecting setting for an exact tool invocation."""

    name: str
    value: HyperparameterValue = None
    default_value: HyperparameterValue = None
    source: HyperparameterSource = HyperparameterSource.TOOL_DEFAULT
    tunable: bool = True
    scientific_impact: str = ""
    requires_user_confirmation: bool = False
    confirmed_by_user: bool = False
    user_question: str = ""
    decision_scope: DecisionScope = Field(
        default=DecisionScope.RESEARCH_DESIGN,
        exclude_if=lambda value: value == DecisionScope.RESEARCH_DESIGN,
    )


class ToolInvocation(StrictModel):
    """Auditable, version-specific tool call and its effective hyperparameters."""

    tool_id: str
    tool_name: str
    version: str
    commit_sha: str = ""
    entrypoint: str
    hyperparameters: list[HyperparameterSetting] = Field(default_factory=list)
    review_complete: bool = False
    review_evidence: list[str] = Field(default_factory=list)
    no_tunable_hyperparameters_reason: str = ""


class ResourceHint(StrictModel):
    name: str
    value: str


class ToolPlan(StrictModel):
    approved_tool_ids: list[str] = Field(default_factory=list)
    proposed_tool_ids: list[str] = Field(default_factory=list)
    proposals: list[ToolProposal] = Field(default_factory=list)
    invocations: list[ToolInvocation] = Field(default_factory=list)
    rationale: str = ""


class ToolRecord(StrictModel):
    id: str = Field(default_factory=lambda: new_id("tool"))
    name: str
    source_url: str
    version: str
    commit_sha: str = ""
    license: str = "unknown"
    install_recipe: str
    image_digest: str = ""
    execution_backend: Literal["analysis", "transfer_job"] = "analysis"
    build_profile: Literal["python_package", "spatial_analysis"] = "python_package"
    availability_note: str = ""
    platforms: list[str] = Field(default_factory=lambda: ["linux/amd64"])
    resource_hints: list[ResourceHint] = Field(default_factory=list)
    smoke_test: str = ""
    status: ToolStatus = ToolStatus.CATALOGED
    provenance: str = ""
    parameter_manifest: list[ParameterSpec] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @field_validator("resource_hints", mode="before")
    @classmethod
    def migrate_resource_hints(cls, value: object) -> object:
        if isinstance(value, dict):
            return [{"name": str(name), "value": str(hint)} for name, hint in value.items()]
        return value


class ToolProposal(ToolRecord):
    status: ToolStatus = ToolStatus.PROPOSED
    why_needed: str
    security_notes: str = ""


class ApprovalRequest(StrictModel):
    id: str = Field(default_factory=lambda: new_id("apr"))
    task_id: str
    kind: ApprovalKind
    title: str
    detail: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    requested_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    decided_by: str | None = None


class MemoryItem(StrictModel):
    id: str = Field(default_factory=lambda: new_id("mem"))
    project_id: str
    owner_id: str | None = None
    kind: MemoryKind
    status: MemoryStatus = MemoryStatus.ACTIVE
    content: str
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    source_task_id: str | None = None
    provenance: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    embedding: list[float] = Field(default_factory=list)
    version: int = 1
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_used_at: datetime | None = None
    supersedes_id: str | None = None

    @field_validator("tags", "entities", mode="before")
    @classmethod
    def normalize_terms(cls, value: object) -> object:
        if isinstance(value, (set, tuple, list)):
            return sorted({str(item) for item in value})
        return value


class ProjectSummary(StrictModel):
    id: str = Field(default_factory=lambda: new_id("sum"))
    project_id: str
    version: int = 1
    summary: str
    source_task_ids: list[str] = Field(default_factory=list)
    tasks_since_compaction: int = 0
    estimated_source_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class ContextManifestEntry(StrictModel):
    kind: str
    id: str
    version: int
    sha256: str
    provenance: str


class ContextBundle(StrictModel):
    schema_version: int = 1
    project_id: str
    task_id: str
    policy: str
    task_contract: TaskContract
    recent_exchanges: list[dict[str, Any]] = Field(default_factory=list)
    project_summary: ProjectSummary | None = None
    memories: list[MemoryItem] = Field(default_factory=list)
    tool_metadata: list[ToolRecord] = Field(default_factory=list)
    dataset_inspections: list[DatasetInspection] = Field(default_factory=list)
    plan_handoff: PlanHandoff | None = Field(default=None, exclude_if=lambda value: value is None)
    rendered: str
    estimated_tokens: int
    manifest: list[ContextManifestEntry] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class ArtifactRef(StrictModel):
    name: str
    uri: str
    media_type: str = "application/octet-stream"
    size_bytes: int = 0
    checksum_sha256: str = ""


class OutputSpec(StrictModel):
    """Frozen delivery requirement; generated processes cannot edit this contract."""

    path: str
    requested_output: str
    format: Literal[
        "json", "csv", "tsv", "png", "pdf", "svg", "text", "h5ad", "hdf5", "npy", "npz", "parquet"
    ]
    required_fields: list[str] = Field(default_factory=list)
    allow_empty: bool = False


class SourceFile(StrictModel):
    path: str
    content: str = Field(max_length=120_000)


class CheckpointSpec(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    source_paths: list[str] = Field(min_length=1)
    required_fields: list[str] = Field(min_length=1)


class ParameterBinding(StrictModel):
    parameter_id: str
    consumer: str = Field(min_length=1)
    api_adaptation: str = Field(min_length=1)


class ImplementationPlan(StrictModel):
    """Reviewable engineering commitments grounded in the actual worker probe."""

    probe_operation_id: str
    input_contracts: list[str] = Field(min_length=1)
    parameter_bindings: list[ParameterBinding] = Field(default_factory=list)
    library_calls: list[str] = Field(min_length=1)
    algorithm: str = Field(min_length=1, max_length=6000)
    scale_analysis: str = Field(min_length=1, max_length=4000)
    peak_memory_gib: float = Field(ge=0)
    validation_plan: list[str] = Field(min_length=1)
    module_paths: list[str] = Field(min_length=1)
    output_specs: list[OutputSpec] = Field(min_length=1)
    blockers: list[str] = Field(default_factory=list)


class DevelopmentAction(StrictModel):
    """One durable tool request, selected by the coding agent."""

    action: Literal[
        "inspect_runtime",
        "inspect_inputs",
        "plan_implementation",
        "write_candidate",
        "edit_files",
        "run_check",
        "read_observation",
        "inspect_artifact",
        "submit_candidate",
        "propose_amendment",
    ]
    base_revision: str = ""
    files: list[SourceFile] = Field(default_factory=list)
    remove_paths: list[str] = Field(default_factory=list)
    design: str = Field(default="", max_length=12_000)
    implementation_plan: ImplementationPlan | None = None
    output_specs: list[OutputSpec] = Field(default_factory=list)
    checkpoint_specs: list[CheckpointSpec] = Field(default_factory=list)
    observation_path: str = ""
    observation_offset: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=4_000)
    amendment_kind: Literal["technical", "scientific"] = "technical"
    scientific_impact: str = Field(default="", max_length=4_000)
    decision_scope: DecisionScope = DecisionScope.IMPLEMENTATION


class CandidateReview(StrictModel):
    approved: bool
    issues: list[str] = Field(default_factory=list)
    scientific_amendment_required: bool = False
    scientific_impact: str = Field(default="", max_length=4_000)
    decision_scope: DecisionScope = DecisionScope.IMPLEMENTATION


class FailureReport(StrictModel):
    phase: str
    category: Literal["code", "validation", "environment", "resource", "infrastructure"]
    message: str
    diagnostic_ref: str = ""
    fingerprint: str = ""


class VerificationReport(StrictModel):
    operation_id: str
    source_hash: str
    input_hash: str
    config_hash: str
    runtime_image: str
    checks: list[str] = Field(default_factory=list)
    passed: bool = False
    validation_suite_hash: str = ""
    replay_coverage: str = "not verified"
    limitations: list[str] = Field(default_factory=list)


class DevelopmentOperation(StrictModel):
    reserved_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    id: str
    phase: Literal["probe", "test", "verify", "run"]
    source_hash: str
    attempt: int
    bundle_ref: str = ""
    result_ref: str = ""
    status: Literal["reserved", "queued", "completed"] = "reserved"
    submission_attempts: int = 0
    infrastructure_retries: int = 0
    prior_result_refs: list[str] = Field(default_factory=list)
    reserved_seconds: float = 0
    reserved_cost_usd: float = 0
    exit_code: int | None = None
    summary: str = ""


class DevelopmentSession(StrictModel):
    version: int = 1
    # Persist the rollout choice: existing workspaces keep their original workflow.
    preparation_version: Literal[0, 1, 2] = 0
    acceptance_checklist: list[str] = Field(default_factory=list)
    candidate_review: CandidateReview | None = None
    candidate_review_source: str = ""
    first_candidate_outcome: str = "pending"
    worker_reuse_enabled: bool = False
    worker_session_id: str = ""
    worker_session_started_at: datetime | None = None
    worker_session_deadline: datetime | None = None
    worker_session_job: str = ""
    worker_seconds_consumed: float = 0
    implementation_plan: ImplementationPlan | None = None
    preparation_hash: str = ""
    first_candidate_source_hash: str = ""
    first_check_passed: bool | None = None
    interaction_revision: int
    authority_hash: str
    state: Literal[
        "developing", "verified", "reviewing_results", "needs_input", "complete", "failed"
    ] = "developing"
    pending_result_ref: str = ""
    preview_artifact: ArtifactRef | None = None
    source_hash: str = ""
    source_ref: str = ""
    design: str = ""
    output_specs: list[OutputSpec] = Field(default_factory=list)
    checkpoint_specs: list[CheckpointSpec] = Field(default_factory=list)
    operations: list[DevelopmentOperation] = Field(default_factory=list)
    model_turns: int = 0
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    action_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    developer_model: str = ""
    prompt_hash: str = ""
    code_revisions: int = 0
    full_runs: int = 0
    reserved_seconds: float = 0
    reserved_cost_usd: float = 0
    runtime_image: str = ""
    feedback: str = ""
    output_contract_hash: str = ""
    review_hash: str = ""
    verified_hash: str = ""
    started_at: datetime = Field(default_factory=utcnow)


class ExecutionBundle(StrictModel):
    workflow_version: Literal[0, 1, 2] = 0
    worker_session_id: str = ""
    worker_session_deadline: datetime | None = None
    queued_at: datetime | None = None
    builder_protocol_version: int = 1
    developer_model: str = ""
    prompt_hash: str = ""
    operation_id: str = ""
    operation_phase: Literal["probe", "test", "verify", "run"] = "run"
    source_files: list[SourceFile] = Field(default_factory=list)
    source_hash: str = ""
    output_specs: list[OutputSpec] = Field(default_factory=list)
    checkpoint_specs: list[CheckpointSpec] = Field(default_factory=list)
    runtime_image: str = ""
    checkpoint_inputs: list[ArtifactRef] = Field(default_factory=list)
    command_timeout_seconds: float = 300
    verification: VerificationReport | None = None
    interaction_revision: int = 0
    task_id: str
    script: str
    contract: TaskContract
    resource_plan: ResourcePlan
    tool_plan: ToolPlan
    parameter_manifest: list[ParameterSpec] = Field(default_factory=list)
    figure_specs: list[FigureSpec] = Field(default_factory=list)
    context_manifest: list[ContextManifestEntry] = Field(default_factory=list)
    input_attachments: list[AttachmentRef] = Field(default_factory=list)
    dataset_inputs: list[DatasetInspection] = Field(default_factory=list)
    attempt: int = 1


class TaskResult(StrictModel):
    runtime_evidence: dict[str, Any] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)
    phase_timestamps: dict[str, datetime] = Field(default_factory=dict)
    operation_id: str = ""
    source_hash: str = ""
    failure: FailureReport | None = None
    verification: VerificationReport | None = None
    publication_committed: bool = False
    audit_artifacts: list[ArtifactRef] = Field(default_factory=list)
    summary: str
    output_text: str = ""
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    tool_usage: list[ToolInvocation] = Field(default_factory=list)
    effective_parameters: list[ParameterSpec] = Field(default_factory=list)
    figure_specs: list[FigureSpec] = Field(default_factory=list)
    exit_code: int | None = None
    started_at: datetime | None = None
    completed_at: datetime = Field(default_factory=utcnow)


class TaskProgress(StrictModel):
    """A concise, user-facing milestone emitted by a running execution."""

    id: str = Field(default_factory=lambda: new_id("progress"))
    attempt: int = Field(ge=1)
    status: ProgressStatus
    message: str = Field(min_length=1, max_length=500)
    operation_id: str = ""
    worker_session_id: str = ""
    eta_low_seconds: float | None = Field(default=None, ge=0)
    eta_high_seconds: float | None = Field(default=None, ge=0)
    eta_basis: str = ""
    trusted: bool = False
    item: str = Field(default="", max_length=200)
    current: int | None = Field(default=None, ge=0)
    total: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utcnow)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_progress_message(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("progress message cannot be blank")
        return normalized

    @field_validator("item", mode="before")
    @classmethod
    def normalize_progress_item(cls, value: object) -> object:
        return " ".join(value.split()) if isinstance(value, str) else value


class ConversationMessage(StrictModel):
    """One canonical turn, rendered once and delivered unchanged on every surface."""

    id: str = Field(default_factory=lambda: new_id("message"))
    event_key: str
    role: Literal["user", "assistant"] = "assistant"
    text: str
    kind: str = "progress"
    source: TaskSource | None = None
    attempt: int = 0
    phase: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class PlanHandoff(StrictModel):
    """Task-local authority and the exact exchange to which a follow-up responds."""

    master_plan: MasterPlan | None = None
    decision_set: DecisionSet | None = None
    contract: TaskContract | None = None
    pending_question: str = ""
    conversation: list[ConversationMessage] = Field(default_factory=list)
    followups: list[TaskContextUpdate] = Field(default_factory=list)


class TaskRecord(StrictModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    public_name: str = ""
    envelope: TaskEnvelope
    structured_request: StructuredUserRequest | None = None
    status: TaskStatus = TaskStatus.RECEIVED
    contract: TaskContract | None = None
    resource_plan: ResourcePlan | None = None
    tool_plan: ToolPlan | None = None
    pending_tool_builds: list[str] = Field(default_factory=list)
    generated_script: str = ""
    runtime_tool_ids: list[str] = Field(default_factory=list)
    dataset_inspections: list[DatasetInspection] = Field(default_factory=list)
    context_bundle: ContextBundle | None = None
    result: TaskResult | None = None
    context_updates: list[TaskContextUpdate] = Field(default_factory=list)
    plan_handoff: PlanHandoff | None = None
    interaction_revision: int = Field(default=0, ge=0)
    cancel_requested_at: datetime | None = None
    progress_updates: list[TaskProgress] = Field(default_factory=list)
    conversation: list[ConversationMessage] = Field(default_factory=list)
    notified_event_keys: list[str] = Field(default_factory=list)
    # Retained for documents written before semantic notification deduplication.
    last_notified_progress_id: str | None = None
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    error: str | None = None
    attempts: int = 0
    submission_attempts: int = Field(default=0, ge=0)
    execution_job_name: str | None = None
    plan_response: str = ""
    # Conversation-agent explanation of the latest amendment, shared by web and channels.
    followup_response: str = ""
    paused_state_uri: str | None = None
    planner_version: int = 1
    builder_protocol_version: int = 1
    development: DevelopmentSession | None = None
    development_version: int = 0
    master_plan_id: str | None = None
    decision_set_id: str | None = None
    current_stage: StageKind | None = None
    stage_summaries: list[TaskStage] = Field(default_factory=list)
    master_plan: MasterPlan | None = None
    decision_set: DecisionSet | None = None
    figure_specs: list[FigureSpec] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def populate_public_name(self) -> TaskRecord:
        # Existing Firestore records predate public names. Deriving from the random UUID keeps
        # those names stable across reads without changing the internal routing key.
        if not self.public_name:
            self.public_name = friendly_task_name(self.id)
        return self


class TaskListItem(StrictModel):
    id: str
    public_name: str
    project_id: str
    prompt: str
    source: TaskSource
    status: TaskStatus
    current_stage: StageKind | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class AgentTaskPlan(StrictModel):
    contract: TaskContract
    resource_plan: ResourcePlan = Field(default_factory=ResourcePlan)
    tool_plan: ToolPlan = Field(default_factory=ToolPlan)
    script: str = ""
    response: str
    memory_candidates: list[MemoryItem] = Field(default_factory=list)


class ApprovalDecision(StrictModel):
    approved: bool
    response: str = ""


class ProjectCreate(StrictModel):
    slug: str
    name: str
    members: set[str] = Field(default_factory=set)


class ProjectPatch(StrictModel):
    name: str | None = None
    members: set[str] | None = None
    memory_policy: MemoryPolicy | None = None


class MemoryPatch(StrictModel):
    content: str | None = None
    status: MemoryStatus | None = None
    tags: list[str] | None = None
    entities: list[str] | None = None
