"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings with secure, low-cost defaults."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PISTACHIO_",
        case_sensitive=False,
        extra="ignore",
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    store_backend: Literal["memory", "firestore"] = "memory"
    execution_backend: Literal["local", "docker", "gcp_batch"] = "local"
    auth_mode: Literal["dev", "iap", "oidc"] = "dev"
    allowed_emails: tuple[str, ...] = ()
    oidc_audience: str = ""

    openai_model: str = "gpt-5.6-sol"
    openai_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = "medium"
    planner_version: int = Field(default=2, ge=1, le=2)
    builder_protocol_version: Literal[1, 2] = 1
    development_model: str = "gpt-5.6-sol"
    development_preparation_enabled: bool = True
    development_latency_enabled: bool = False
    worker_sessions_enabled: bool = False
    worker_session_idle_seconds: int = Field(default=600, ge=30, le=600)
    worker_input_cache: Path | None = None
    development_initial_reasoning_effort: Literal["medium", "high", "xhigh"] = "high"
    development_initial_timeout_seconds: float = Field(default=300, ge=150, le=300)
    development_max_turns: int = Field(default=40, ge=1, le=100)
    development_max_operations: int = Field(default=12, ge=1, le=50)
    development_max_revisions: int = Field(default=20, ge=1, le=40)
    development_seconds: float = Field(default=1800, gt=0)
    development_wall_seconds: float = Field(default=7200, gt=0)
    development_command_seconds: float = Field(default=300, gt=0, le=1800)
    development_artifact_bytes: int = Field(default=100_000_000, gt=0)
    # Set only after Docker/Batch sandbox conformance has passed for the deployed image.
    development_enabled: bool = False
    worker_sandbox: Literal["docker", "bubblewrap", "test"] = "docker"
    master_planner_model: str = "gpt-5.6-sol"
    worker_model: str = "gpt-6-luna"
    worker_fallback_model: str = "gpt-5.6-sol"
    worker_reasoning_effort: Literal["none", "low", "medium"] = "low"
    master_planner_timeout_seconds: float = Field(default=180.0, ge=30.0, le=300.0)
    worker_timeout_seconds: float = Field(default=150.0, ge=30.0, le=300.0)
    embedding_model: str = "text-embedding-3-small"
    disable_agents_tracing: bool = True

    artifact_root: Path = Path(".pistachio/artifacts")
    docker_artifact_root: Path | None = None
    artifact_retention_days: int = 30
    memory_inactive_days: int = 90
    project_idle_days: int = 14
    compaction_task_threshold: int = 5
    compaction_token_threshold: int = 60_000
    context_token_budget: int = 40_000
    memory_token_budget: int = 8_000
    memory_result_limit: int = 8

    compute_approval_usd: float = 20.0
    compute_cost_contingency: float = 0.25
    max_concurrent_jobs: int = 3

    gcp_project: str = ""
    gcp_region: str = "us-central1"
    gcs_bucket: str = ""
    gcs_quarantine_bucket: str = ""
    batch_service_account: str = ""
    batch_image: str = ""
    # Operator-pinned previous workers retained while their existing sessions drain.
    batch_compatible_images: tuple[str, ...] = ()
    # Experimental staging-only permission for an unprivileged nested namespace worker.
    # Removes outer seccomp/AppArmor profiles; requires explicit operator approval.
    batch_nested_sandbox_opt_in: bool = False
    batch_network: str = "default"
    batch_subnetwork: str = "default"
    control_plane_url: str = ""
    cloud_tasks_queue: str = "pistachio-tasks"
    cloud_tasks_service_account: str = ""
    completion_topic: str = ""

    slack_bot_token: str = ""
    # Both Slack backends share the headless control plane.
    slack_agent_backend: Literal["existing", "claude_batch"] = "existing"
    slack_agent_image: str = ""
    anthropic_api_key: SecretStr = Field(
        default=SecretStr(""), validation_alias="ANTHROPIC_API_KEY"
    )
    slack_signing_secret: str = ""
    slack_user_map: dict[str, str] = Field(default_factory=dict)
    gmail_account: str = ""
    gmail_pubsub_topic: str = ""
    signed_link_secret: str = ""
    github_org: str = "azizilab"
    github_app_token: str = ""
    tool_build_trigger: str = ""
    tool_image_repository: str = ""

    dev_user_email: str = "developer@example.org"
    upload_limit_bytes: int = 100 * 1024 * 1024

    @field_validator("builder_protocol_version", mode="before")
    @classmethod
    def parse_builder_protocol(cls, value: object) -> object:
        return int(value) if isinstance(value, str) and value in {"1", "2"} else value

    @field_validator("allowed_emails", mode="before")
    @classmethod
    def split_emails(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip().lower() for part in value.split(",") if part.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(part).strip().lower() for part in value if str(part).strip())
        return value

    @model_validator(mode="after")
    def validate_auth_environment(self) -> Settings:
        if self.auth_mode == "dev" and self.env not in {"local", "test"}:
            raise ValueError("dev authentication is permitted only in local/test environments")
        return self

    @field_validator("artifact_root", "docker_artifact_root", mode="before")
    @classmethod
    def expand_path(cls, value: object) -> object:
        return Path(str(value)).expanduser() if value not in {None, ""} else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
