"""Verify the durable commit before accepting cloud operation completion."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlparse

from mica.execution.workspace import safe_relative
from mica.models import TaskResult


def verify_cloud_commit(
    bucket: Any, task_id: str, attempt: int, result: TaskResult, payload: str | None = None
) -> dict[str, Any]:
    prefix = f"tasks/{task_id}/attempt-{attempt}/"
    commit = json.loads(bucket.blob(prefix + "publication-commit.json").download_as_text())
    if commit["operation_id"] != result.operation_id:
        raise ValueError("publication operation identity mismatch")
    if payload is None:
        payload = bucket.blob(prefix + "worker-result.json").download_as_text()
    if hashlib.sha256(payload.encode()).hexdigest() != commit["checksums"]["worker-result.json"]:
        raise ValueError("published result checksum mismatch")
    if TaskResult.model_validate_json(payload) != result:
        raise ValueError("completion payload does not match publication")
    for artifact in result.artifacts + result.audit_artifacts:
        parsed = urlparse(artifact.uri)
        key = parsed.path.lstrip("/")
        if parsed.scheme != "gs" or parsed.netloc != bucket.name or not key.startswith(prefix):
            raise ValueError("published artifact is outside its operation")
        relative = safe_relative(key.removeprefix(prefix))
        if commit["checksums"].get(relative) != artifact.checksum_sha256:
            raise ValueError("artifact is missing from the durable commit")

    return dict(commit)
