"""Narrow Cloud Run log intake; raw log content never enters the incident index."""

from __future__ import annotations

import base64
import binascii
import json
import re
from urllib.parse import urlsplit

from mica.storage.failures import Observation, digest

# These are the only handlers that deliver/persist failure summaries. Never ingest their
# own HTTP failures. Container stdout/stderr (including reporter logs) is excluded entirely.
REPORTER_PATHS = (
    "/api/v1/internal/maintenance/log-failure",
    "/api/v1/internal/maintenance/reconcile-batch",
    "/api/v1/internal/maintenance/compact-idle",
)
MAX_ENVELOPE = 512_000
# Fixed templates, not arbitrary URL segments. Unknown/new endpoints remain visible as "other".
ROUTES = {
    r"/api/v1/internal/tasks/[^/]+/process": "/api/v1/internal/tasks/{task_id}/process",
    r"/api/v1/internal/slack-analysis/process/[^/]+/[0-9]+": (
        "/api/v1/internal/slack-analysis/process/{event_id}/{attempt}"
    ),
    r"/integrations/slack/events": "/integrations/slack/events",
    r"/integrations/gmail/push": "/integrations/gmail/push",
    r"/api/v1/internal/batch/completion": "/api/v1/internal/batch/completion",
    r"/api/v1/internal/tools/catalog": "/api/v1/internal/tools/catalog",
}


def parse(body: bytes, *, project: str, service: str) -> Observation | None:
    """Return safe metadata or deliberately ignore malformed/out-of-scope entries.

    Missing insertId uses Pub/Sub's stable messageId, scoped by logName. Different
    publications without an insertId cannot safely be claimed to be the same occurrence.
    """
    if not service or len(body) > MAX_ENVELOPE:
        return None
    try:
        envelope = json.loads(body)
        message = envelope["message"]
        entry = json.loads(base64.b64decode(message["data"], validate=True))
        resource = entry["resource"]
        labels = resource["labels"]
        if (
            resource["type"] != "cloud_run_revision"
            or labels["project_id"] != project
            or labels["service_name"] != service
        ):
            return None
        log_name = entry["logName"]
        prefix = f"projects/{project}/logs/run.googleapis.com%2F"
        if log_name not in {prefix + "requests", prefix + "varlog%2Fsystem"}:
            return None
        identity = entry.get("insertId") or message.get("messageId")
        identity_kind = "insert" if entry.get("insertId") else "pubsub"
        if not isinstance(identity, str) or not identity or len(identity) > 512:
            return None
        status = 0
        route = "platform"
        cause = "infra_platform_error"
        event_id = ""
        if log_name == prefix + "requests":
            request = entry["httpRequest"]
            status = request["status"]
            if type(status) is not int or not 500 <= status < 600:
                return None
            path = urlsplit(request.get("requestUrl", "")).path
            if any(path.rstrip("/") == p for p in REPORTER_PATHS):
                return None
            route = next(
                (template for pattern, template in ROUTES.items() if re.fullmatch(pattern, path)),
                "unmatched route",
            )
            cause = (
                "github_catalog_failed"
                if route == "/api/v1/internal/tools/catalog"
                else "infra_http_error"
            )
            # This opaque internal key is useful only after a repository lookup. Never
            # preserve arbitrary path segments, URL queries, or external event IDs.
            match = re.fullmatch(
                r"/api/v1/internal/slack-analysis/process/([a-f0-9]{32})/[0-9]+", path
            )
            if match:
                event_id = match[1]
        elif entry.get("severity") not in {"ERROR", "CRITICAL", "ALERT", "EMERGENCY"}:
            return None
        # Trusted platform metadata only, with bounds even for authenticated publisher mistakes.
        revision = labels.get("revision_name", "")
        if not isinstance(revision, str) or not re.fullmatch(r"[a-z0-9-]{1,100}", revision):
            revision = ""
        return Observation(
            scope=f"cloud_run:{project}:{service}:{route}:{status}",
            kind="infrastructure",
            cause=cause,
            occurrence=digest(log_name, identity_kind, identity),
            source="cloud_run",
            service=service,
            route=route,
            http_status=status,
            revision=revision,
            event_id=event_id,
        )
    except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
        return None
