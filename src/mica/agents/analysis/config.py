from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .transfer_config import TransferProject

CHECKPOINT_PREFIX = "tasks/slack-analysis"
MAX_MEMORY_TOTAL_KB = 16384


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENT_", env_file=".env", extra="ignore", populate_by_name=True
    )
    anthropic_api_key: SecretStr = Field(default=SecretStr(""), alias="ANTHROPIC_API_KEY")
    slack_bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "SLACK_BOT_TOKEN", "MICA_SLACK_BOT_TOKEN", "PISTACHIO_SLACK_BOT_TOKEN"
        ),
    )
    slack_app_token: SecretStr = Field(default=SecretStr(""), alias="SLACK_APP_TOKEN")
    model: str = "claude-opus-5-5"
    conversation_effort: Literal["low", "medium", "high"] = "medium"
    conversation_max_calls: int = Field(default=6, ge=1, le=12)
    routing_repair_model: str = "claude-opus-5"  # Failure investigation assessments.
    loading_emoji: str = ":hourglass_flowing_sand:"  # AGENT_LOADING_EMOJI; custom emoji supported.
    amendments: bool = False
    failure_channel: str = Field(default="", pattern=r"^(?:[CG][A-Z0-9]+)?$")
    outcome_review_enabled: bool = True  # Post-turn enum-only completion checks.
    failure_investigation_enabled: bool = True  # Bounded read-only Claude assessments.
    failure_operator_ids: str = ""  # Explicit team:user IDs allowed to resolve incident alerts.
    reference_github_org: Literal["azizilab"] = "azizilab"
    reference_github_app_id: str = Field(default="", pattern=r"^[0-9]*$")
    reference_github_installation_id: str = Field(default="", pattern=r"^[0-9]*$")
    reference_github_key_secret: str = ""  # Control plane only; never copied to workers.
    memory_repo: str = ""
    memory_branch: str = "main"
    memory_token_secret: str = ""  # Control-plane-only Secret Manager version.
    memory_index_chars: int = Field(default=4000, ge=1, le=16000)
    memory_file_kb: int = Field(default=16, ge=1, le=64)
    memory_max_files: int = Field(default=500, ge=1, le=1000)
    memory_total_kb: int = Field(default=4096, ge=1, le=MAX_MEMORY_TOTAL_KB)
    thread_memory_enabled: bool = False  # Trusted worker flag; no GitHub credentials.
    thread_memory: Literal["dual", "github", "github_only"] = "dual"
    thread_text_kb: int = Field(default=4096, ge=64, le=16384)
    thread_max_files: int = Field(default=4000, ge=10, le=10000)
    thread_summary_kb: int = Field(default=64, ge=16, le=128)
    thread_log_kb: int = Field(default=32, ge=4, le=64)
    thread_script_kb: int = Field(default=256, ge=1, le=1024)
    thread_scripts_kb: int = Field(default=2048, ge=1, le=16384)
    thread_output_kb: int = Field(default=1024, ge=1, le=1024)
    thread_outputs_kb: int = Field(default=20480, ge=1, le=102400)
    thread_idle_days: int = Field(default=14, ge=1, le=365)
    thread_output_age_days: int = Field(default=10, ge=1, le=365)
    thread_expiry_days: int = Field(default=30, ge=1, le=365)
    memory_router_chars: int = Field(default=12000, ge=1, le=64000)
    memory_auto_edit: bool = True
    memory_compaction_enabled: bool = True
    # During coordinated rollout, keep the old layout until old API requests have drained.
    memory_archive_after: float = Field(default=0, ge=0)
    memory_compaction_chars: int = Field(default=24000, ge=2000, le=64000)
    memory_compaction_attempts: int = Field(default=2, ge=1, le=3)
    memory_compaction_calls_per_hour: int = Field(default=3, ge=1, le=6)
    memory_compaction_cooldown_seconds: int = Field(default=600, ge=60, le=3600)
    memory_model: str = "claude-opus-5"  # Requires forced tool choice support.
    fallback_model: str = "claude-opus-5"
    bucket: str = Field(
        default="",
        validation_alias=AliasChoices("AGENT_BUCKET", "MICA_GCS_BUCKET", "PISTACHIO_GCS_BUCKET"),
    )
    data_buckets: str = ""
    data_project: str = ""
    data_mount_root: Path = Path("/mnt/gcs")
    work_root: Path = Path("/srv/pistachio/work")
    state_db: Path = Path("/srv/pistachio/state.db")
    sci_python: Path = Path("/opt/sci/bin/python")
    tools_dir: Path = Path("/opt/pistachio/tools")
    plugin_dir: Path = Path("/opt/pistachio/plugin")
    runtimes_enabled: bool = False
    runtime_root: Path = Path("/srv/pistachio/runtimes")
    runtime_min_age_days: int = Field(default=7, ge=0)
    runtime_max_gb: float = Field(default=24, gt=0)
    runtime_max_scratch_gb: float = Field(default=48, gt=0)
    runtime_max_download_gb: float = Field(default=24, gt=0)
    runtime_min_free_disk_gb: float = Field(default=20, ge=0)
    runtime_max_new_per_turn: int = Field(default=3, ge=1, le=10)
    runtime_timeout_seconds: int = Field(default=2700, ge=1, le=2700)
    runtime_max_local_gb: float = Field(default=120, gt=0)
    runtime_max_source_builds: int = Field(default=5, ge=0, le=20)
    runtime_failure_ttl_hours: float = Field(default=24, gt=0)
    runtime_denylist: str = (
        "awscli,boto3,botocore,google-cloud-storage,google-cloud-secret-manager,"
        "google-cloud-compute,google-cloud-run,google-cloud-batch,google-cloud-resource-manager,"
        "google-cloud-iam,azure-identity,azure-mgmt-resource,azure-keyvault-secrets,"
        "keyring,keyrings-alt,fabric,sh,plumbum,paramiko,pysftp,asyncssh,ftputil,scapy,python-nmap"
    )
    return_scripts_default: bool = False
    attachment_max_files: int = Field(default=10, ge=1, le=20)
    attachment_max_mb: int = Field(default=100, ge=1, le=500)
    attachment_max_message_mb: int = Field(default=250, ge=1, le=1000)
    max_turns: int = Field(default=500, ge=1)
    max_budget_usd_per_turn: float = Field(default=5, gt=0)
    max_concurrent_runs: int = Field(default=3, ge=1)
    on_demand_after_hours: float = Field(default=2, gt=0, le=336)
    max_tier: Literal["standard", "large", "xlarge"] = "xlarge"
    gpu_tier_enabled: bool = False
    gpu_flexible_sizing: bool = False
    gpu_types: str = Field(
        default="t4,l4",
        pattern=r"^(?:t4|l4|a100|a100-80gb|h100)(?:,(?:t4|l4|a100|a100-80gb|h100))*$",
    )
    gpu_default_type: Literal["t4", "l4"] = "t4"
    gpu_queue_wait_seconds: int = Field(default=600, ge=60, le=3600)
    gpu_image: str = Field(default="", pattern=r"^(?:[a-z0-9./_-]+@sha256:[0-9a-f]{64})?$")
    gpu_max_seconds: int = Field(default=7 * 24 * 3600, ge=600, le=14 * 24 * 3600)
    gpu_default_seconds: int = Field(default=8 * 3600, ge=600, le=14 * 24 * 3600)
    gpu_hourly_estimate_usd: float = Field(default=2.0, gt=0, le=100)
    gpu_t4_hourly_estimate_usd: float = Field(default=1.5, gt=0, le=100)
    gpu_worker: bool = False  # Set only on the explicitly approved GPU worker.
    partial_checkpoint_seconds: int = Field(default=900, ge=60, le=3600)
    long_run_threshold_seconds: int = Field(default=600, ge=60)
    long_run_update_seconds: int = Field(default=7200, ge=60)
    # Finite multi-day limit; GCP Batch's general maximum is 14 days.
    max_run_seconds: int = Field(default=7 * 24 * 3600, ge=600, le=14 * 24 * 3600)
    # Channel mode denies admission until a designated admin enables a channel.
    access_mode: Literal["channel", "mapped", "workspace"] = "channel"
    access_admin_ids: str = ""  # Explicit team:user IDs; onboarding authority only.
    access_request_owner_id: str = Field(default="", pattern=r"^(?:T[A-Z0-9]+:[UW][A-Z0-9]+)?$")
    write_admin_ids: str = ""  # Separate team:user IDs authorized to manage output grants.
    write_allowed_prefixes: str = ""  # Comma-separated gs://bucket/folder/ ceilings.
    write_service_account: str = ""  # Dedicated output writer identity; control plane only.
    write_upload_seconds: int = Field(default=840, ge=1, le=840)
    access_cache_seconds: int = Field(default=30, ge=0, le=60)
    access_unavailable_grace_seconds: int = Field(default=1800, ge=1, le=3600)
    allowed_user_ids: str = ""  # Legacy mapped mode only.
    # Separate staging-only approval for Docker's proc masking exception.
    batch_proc_mount_opt_in: bool = False
    storage: Literal["gcs", "local"] = "gcs"
    local_storage_root: Path = Path(".local-storage")
    transfers_enabled: bool = False
    transfer_host: str = "na.export.atomx.nanostring.com"
    transfer_port: int = Field(default=22, ge=1, le=65535)
    transfer_host_key: str = ""
    transfer_max_disk_gb: int = Field(default=2048, ge=50, le=10000)
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
            raise ValueError("Set AGENT_DATA_BUCKETS and AGENT_BUCKET (or MICA_GCS_BUCKET)")
