from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    max_turns: int = Field(default=40, ge=1)
    max_budget_usd_per_turn: float = Field(default=5, gt=0)
    max_concurrent_runs: int = Field(default=3, ge=1)
    # Channel mode denies admission until a designated admin enables a channel.
    access_mode: Literal["channel", "mapped"] = "channel"
    access_admin_ids: str = ""  # Explicit team:user IDs; onboarding authority only.
    access_cache_seconds: int = Field(default=30, ge=0, le=60)
    allowed_user_ids: str = ""  # Legacy mapped mode only.
    # Separate staging-only approval for Docker's proc masking exception.
    batch_proc_mount_opt_in: bool = False
    storage: Literal["gcs", "local"] = "gcs"
    local_storage_root: Path = Path(".local-storage")

    def csv(self, field: str) -> set[str]:
        return {x.strip() for x in getattr(self, field).split(",") if x.strip()}

    def require_credentials(self, *, slack: bool = False) -> None:
        names = ["anthropic_api_key"] + (["slack_bot_token", "slack_app_token"] if slack else [])
        for name in names:
            if not getattr(self, name).get_secret_value():
                raise ValueError(f"Set {name.upper()} in the environment or .env")
        if slack and (not self.data_buckets or (self.storage == "gcs" and not self.bucket)):
            raise ValueError("Set AGENT_DATA_BUCKETS and AGENT_BUCKET (or PISTACHIO_GCS_BUCKET)")
