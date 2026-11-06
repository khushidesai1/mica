"""Add-only project IAM grants through a dedicated, role-limited identity."""

from __future__ import annotations

import copy
import json
import re
from typing import TYPE_CHECKING, Any

from googleapiclient.errors import HttpError

from .cloud_access_config import grant_role
from .identity import recipient_email

if TYPE_CHECKING:
    from .config import Settings


class GrantFailed(ValueError):
    """A definite provider refusal safe to show without exposing its response body."""


def client(settings: Settings) -> Any:
    import google.auth
    import google_auth_httplib2
    import httplib2
    from google.auth import impersonated_credentials
    from googleapiclient.discovery import build

    if not settings.access_service_account:
        raise GrantFailed("Cloud access is not configured yet. Please contact the administrator.")
    source, _ = google.auth.default()  # type: ignore[no-untyped-call]
    credentials = impersonated_credentials.Credentials(  # type: ignore[no-untyped-call]
        source_credentials=source,
        target_principal=settings.access_service_account,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
        lifetime=300,
    )
    http = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=15))
    return build("cloudresourcemanager", "v1", http=http, cache_discovery=False)


def failure(exc: HttpError) -> GrantFailed:
    detail = exc.content.decode(errors="replace").lower()
    if any(
        s in detail
        for s in ("allowedpolicymember", "domain restricted", "organization policy", "org policy")
    ):
        return GrantFailed(
            "The organization's sharing policy blocks this account. No access was granted."
        )
    if any(
        s in detail
        for s in ("does not exist", "not a valid member", "invalid member", "unknown user")
    ):
        return GrantFailed(
            "No Google account was found for that email. Check the address and try again."
        )
    if exc.resp.status in {401, 403}:
        return GrantFailed(
            "Mica lacks permission to grant this access. Please contact the administrator."
        )
    return GrantFailed("Google Cloud rejected this access grant. Please contact the administrator.")


def inputs(project: str, email: str, roles: tuple[str, ...]) -> tuple[str, set[str]]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project):
        raise ValueError("Invalid target project.")
    if not roles or len(roles) > 10:
        raise ValueError("Choose one to ten configured roles.")
    return "user:" + recipient_email(email), {grant_role(role) for role in roles}


def read(api: Any, project: str) -> dict[str, Any]:
    value = (
        api.projects()
        .getIamPolicy(resource=project, body={"options": {"requestedPolicyVersion": 3}})
        .execute()
    )
    if not isinstance(value, dict) or not value.get("etag"):
        raise GrantFailed("Google Cloud returned no policy version; no access was changed.")
    return value


def missing(policy: dict[str, Any], member: str, roles: set[str]) -> set[str]:
    return roles - {
        b["role"]
        for b in policy.get("bindings", [])
        if not b.get("condition") and member in b.get("members", [])
    }


def missing_roles(
    project: str, email: str, roles: tuple[str, ...], *, settings: Settings
) -> set[str]:
    member, requested = inputs(project, email, roles)
    try:
        return missing(read(client(settings), project), member, requested)
    except HttpError as exc:
        if exc.resp.status >= 500 or exc.resp.status == 429:
            raise
        raise failure(exc) from None


def grant(project: str, email: str, roles: tuple[str, ...], *, settings: Settings) -> None:
    member, requested = inputs(project, email, roles)
    api = client(settings)
    for attempt in range(3):
        try:
            policy = read(api, project)
            needed = missing(policy, member, requested)
            if not needed:
                return
            proposed = copy.deepcopy(policy)
            proposed["version"] = 3
            bindings = proposed.setdefault("bindings", [])
            for role in sorted(needed):
                binding = next(
                    (b for b in bindings if b["role"] == role and not b.get("condition")), None
                )
                if binding is None:
                    bindings.append({"role": role, "members": [member]})
                else:
                    binding.setdefault("members", []).append(member)
            api.projects().setIamPolicy(resource=project, body={"policy": proposed}).execute()
            return
        except HttpError as exc:
            detail = json.dumps(str(exc)).lower()
            conflict = exc.resp.status == 409 or (exc.resp.status == 412 and "etag" in detail)
            if conflict and attempt < 2:
                continue
            if conflict:
                raise GrantFailed(
                    "The access policy changed repeatedly. Please submit a new request."
                ) from None
            if exc.resp.status >= 500 or exc.resp.status == 429:
                raise  # Safe to retry this add-only operation after an ambiguous write.
            raise failure(exc) from None
