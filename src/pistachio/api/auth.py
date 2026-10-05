"""Authentication for the task API and trusted service callbacks."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from pistachio.config import Settings


@dataclass(frozen=True, slots=True)
class Principal:
    id: str


def _allow(settings: Settings, email: str) -> Principal:
    normalized = email.removeprefix("accounts.google.com:").strip().lower()
    if not normalized or "@" not in normalized:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid identity")
    if settings.allowed_emails and normalized not in settings.allowed_emails:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="user is not allowlisted")
    return Principal(normalized)


async def get_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.services.settings
    if settings.auth_mode == "dev":
        return _allow(settings, settings.dev_user_email)
    if settings.auth_mode == "iap":
        return _allow(settings, request.headers.get("x-goog-authenticated-user-email", ""))
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer ") or not settings.oidc_audience:
        raise HTTPException(status_code=401, detail="authentication required")
    try:
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            authorization.removeprefix("Bearer "),
            GoogleRequest(),  # type: ignore[no-untyped-call]
            audience=settings.oidc_audience,
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid OIDC token") from exc
    email = str(claims.get("email", "")).lower()
    if email and email == settings.cloud_tasks_service_account.lower():
        return Principal(email)
    return _allow(settings, email)
