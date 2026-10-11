from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .transfer_config import TransferProject

CHECKPOINT_PREFIX = "tasks/slack-analysis"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", populate_by_name=True
    )
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), alias="ANTHROPIC_API_KEY")
    slack_bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("SLACK_BOT_TOKEN", "PISTACHIO_SLACK_BOT_TOKEN"),
    )
    slack_app_token: SecretStr = Field(default=SecretStr(""), alias="SLACK_APP_TOKEN")
    model: str = "claude-opus-5-5"
    loading_emoji: str = ":hourglass_flowing_sand:"  # AGENT_LOADING_EMOJI; custom emoji supported.
    memory_repo: str = ""
    memory_branch: str = "main"
    memory_token_secret: str = ""  # Control-plane-only Secret Manager version.
    memory_index_chars: int = Field(default=4000, ge=1, le=16000)
    memory_file_kb: int = Field(default=16, ge=1, le=64)
    memory_max_files: int = Field(default=200, ge=1, le=1000)
    memory_total_kb: int = Field(default=1024, ge=1, le=16384)
    memory_router_chars: int = Field(default=12000, ge=1, le=64000)
    memory_auto_edit: bool = True
    memory_model: str = ""
    fallback_model: str = "claude-opus-5"
    bucket: str = Field(
        default="", validation_alias=AliasChoices("AGENT_BUCKET", "PISTACHIO_GCS_BUCKET")
    )
    data_buckets: str = ""
    data_project: str = ""
    data_mount_root: Path = Path("/mnt/gcs")
    work_root: Path = Path("/srv/pistachio/work")
    state_db: Path = Path("/srv/pistachio/state.db")
    sci_python: Path = Path("/opt/sci/bin/python")
    tools_dir: Path = Path("/opt/pistachio/tools")
    runtimes_enabled: bool = False
    runtime_root: Path = Path("/srv/pistachio/runtimes")
    runtime_min_age_days: int = Field(default=7, ge=0)
    runtime_max_gb: float = Field(default=8, gt=0)
    runtime_max_new_per_turn: int = Field(default=3, ge=1, le=10)
    runtime_timeout_seconds: int = Field(default=900, ge=1, le=900)
    runtime_max_local_gb: float = Field(default=60, gt=0)
    runtime_denylist: str = ""
    return_scripts_default: bool = False
    attachment_max_files: int = Field(default=10, ge=1, le=20)
    attachment_max_mb: int = Field(default=100, ge=1, le=500)
    attachment_max_message_mb: int = Field(default=250, ge=1, le=1000)
    max_turns: int = Field(default=40, ge=1)
    max_budget_usd_per_turn: float = Field(default=5, gt=0)
    max_concurrent_runs: int = Field(default=3, ge=1)
    launch_heavy_tier: Literal["standard", "large", "xlarge"] = "large"
    max_tier: Literal["standard", "large", "xlarge"] = "xlarge"
    max_concurrent_large_runs: int = Field(default=1, ge=1, le=10)
    partial_checkpoint_seconds: int = Field(default=900, ge=60, le=3600)
    compute_checkpoint_max_gb: int = Field(default=100, ge=1, le=500)
    long_run_threshold_seconds: int = Field(default=600, ge=60)
    long_run_update_seconds: int = Field(default=7200, ge=60)
    # Finite multi-day limit; GCP Batch's general maximum is 14 days.
    max_run_seconds: int = Field(default=7 * 24 * 3600, ge=600, le=14 * 24 * 3600)
    # Channel mode denies admission until a designated admin enables a channel.
    access_mode: Literal["channel", "mapped", "workspace"] = "channel"
    access_admin_ids: str = ""  # Explicit team:user IDs; onboarding authority only.
    write_admin_ids: str = ""  # Separate team:user IDs authorized to manage output grants.
    write_allowed_prefixes: str = ""  # Comma-separated gs://bucket/folder/ ceilings.
    write_service_account: str = ""  # Dedicated create/get-only identity; control plane only.
    write_max_mb: int = Field(default=512, ge=1, le=10000)
    access_cache_seconds: int = Field(default=30, ge=0, le=60)
    allowed_user_ids: str = ""  # Legacy mapped mode only.
    # Separate staging-only approval for Docker's proc masking exception.
    batch_proc_mount_opt_in: bool = False
    storage: Literal["gcs", "local"] = "gcs"
    local_storage_root: Path = Path(".local-storage")
    transfers_enabled: bool = False
    transfer_image: str = ""
    transfer_service_account: str = ""
    transfer_projects: dict[str, TransferProject] = Field(default_factory=dict)

    def csv(self, field: str) -> set[str]:
        return {x.strip() for x in getattr(self, field).split(",") if x.strip()}

    def require_credentials(self, *, slack: bool = False) -> None:
        names = ["anthropic_api_key"] + (["slack_bot_token", "slack_app_token"] if slack else [])
        for name in names:
            if not getattr(self, name).get_secret_value():
                raise ValueError(f"Set {name.upper()} in the environment or .env")
        if slack and (not self.data_buckets or (self.storage == "gcs" and not self.bucket)):
            raise ValueError("Set AGENT_DATA_BUCKETS and AGENT_BUCKET (or PISTACHIO_GCS_BUCKET)")
