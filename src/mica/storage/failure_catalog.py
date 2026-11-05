"""Fixed operational causes; every new source declares its default impact."""

from typing import Literal, NamedTuple

Priority = Literal["high", "medium", "low"]


class CauseInfo(NamedTuple):
    kind: str
    explanation: str
    priority: Priority


CATALOG: dict[str, CauseInfo] = {
    "github_catalog_failed": CauseInfo(
        "catalog", "The scheduled GitHub tool-catalog refresh failed.", "low"
    ),
    "infra_http_error": CauseInfo(
        "infrastructure", "Cloud Run recorded a failed HTTP request.", "low"
    ),
    "infra_platform_error": CauseInfo(
        "infrastructure", "Cloud Run reported a platform error.", "low"
    ),
    "routing_exhausted": CauseInfo(
        "routing", "Request routing exhausted its automatic retries.", "high"
    ),
    "conversation_failed": CauseInfo(
        "turn", "The conversation could not complete an internal operation.", "high"
    ),
    "access_unverifiable": CauseInfo(
        "access", "Project access could not be verified within the grace period.", "medium"
    ),
    "bucket_unavailable": CauseInfo(
        "access", "The service could not verify access to project storage.", "medium"
    ),
    "compute_failed": CauseInfo(
        "compute", "The worker stopped without completing the request.", "medium"
    ),
    "compute_control_failed": CauseInfo(
        "compute", "The service could not submit or inspect its worker.", "medium"
    ),
    "compute_deadline": CauseInfo(
        "compute", "The request exhausted its computation time allowance.", "medium"
    ),
    "runtime_install_failed": CauseInfo(
        "runtime",
        "A requested Python package runtime could not be installed; the worker can continue.",
        "medium",
    ),
    "reference_repo_failed": CauseInfo(
        "reference",
        "A project reference repository could not be loaded; the request continues without it.",
        "medium",
    ),
    "memory_editor_failed": CauseInfo(
        "memory", "Project memory editing failed; its task may retry.", "low"
    ),
    "memory_compaction_retry": CauseInfo(
        "memory", "Project memory compaction failed and needs a retry.", "low"
    ),
    "memory_compaction_exhausted": CauseInfo(
        "memory", "Project memory compaction exhausted its safe attempts.", "low"
    ),
    "email_delivery_failed": CauseInfo(
        "delivery", "An email response could not be delivered.", "high"
    ),
    "email_delivery_unconfirmed": CauseInfo(
        "delivery", "An email send is unconfirmed and needs outbox review.", "medium"
    ),
    "slack_delivery_failed": CauseInfo(
        "delivery", "A Slack response could not be delivered.", "high"
    ),
    "atomx_inventory_failed": CauseInfo("transfer", "The AtoMx inventory worker failed.", "medium"),
    "atomx_pull_failed": CauseInfo("transfer", "The AtoMx transfer worker failed.", "medium"),
    "atomx_auth_failed": CauseInfo(
        "transfer", "The saved AtoMx connection failed authentication.", "medium"
    ),
    "atomx_control_failed": CauseInfo(
        "transfer", "The AtoMx controller could not advance a transfer.", "medium"
    ),
    "atomx_delivery_failed": CauseInfo(
        "delivery", "A private AtoMx Slack response could not be delivered.", "high"
    ),
    "smoke_test": CauseInfo("test", "Synthetic Mica incident; no user job failed.", "low"),
    "request_blocked": CauseInfo(
        "request", "Mica answered but could not complete the requested work.", "medium"
    ),
    "request_stalled": CauseInfo(
        "request", "This request stopped advancing; its polling chain may have stopped.", "high"
    ),
    "requests_failing": CauseInfo(
        "service", "Several requests encountered the same kind of failure recently.", "high"
    ),
}
