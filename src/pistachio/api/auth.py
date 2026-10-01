"""Authentication dependencies and Google OAuth browser flow."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import cast
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

from pistachio.config import Settings

router = APIRouter(prefix="/auth", tags=["authentication"])


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


def _sign(settings: Settings, payload: dict[str, object]) -> str:
    encoded = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    signature = hmac.new(
        settings.signed_link_secret.encode(), encoded.encode(), hashlib.sha256
    ).hexdigest()
    return f"{encoded}.{signature}"


def _unsign(settings: Settings, value: str) -> dict[str, object] | None:
    if not settings.signed_link_secret:
        return None
    try:
        encoded, signature = value.rsplit(".", 1)
        expected = hmac.new(
            settings.signed_link_secret.encode(), encoded.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        payload = cast(dict[str, object], json.loads(base64.urlsafe_b64decode(encoded + "===")))
        if int(str(payload.get("exp", 0))) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def get_principal(request: Request) -> Principal:
    settings: Settings = request.app.state.services.settings
    if settings.auth_mode == "dev":
        return _allow(settings, settings.dev_user_email)
    if settings.auth_mode == "iap":
        return _allow(settings, request.headers.get("x-goog-authenticated-user-email", ""))
    session = _unsign(settings, request.cookies.get("pistachio_session", ""))
    if session:
        return _allow(settings, str(session.get("email", "")))
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


@router.get("/login")
async def login(request: Request) -> RedirectResponse:
    settings: Settings = request.app.state.services.settings
    if not settings.oauth_client_id or not settings.signed_link_secret:
        raise HTTPException(status_code=503, detail="Google OAuth is not configured")
    requested_return = request.query_params.get("next", "/")
    return_to = (
        requested_return
        if requested_return.startswith("/") and not requested_return.startswith("//")
        else "/"
    )
    state = _sign(
        settings,
        {
            "nonce": secrets.token_urlsafe(24),
            "exp": int(time.time()) + 600,
            "return_to": return_to,
        },
    )
    redirect_uri = f"{settings.control_plane_url.rstrip('/')}/auth/callback"
    query = urlencode(
        {
            "client_id": settings.oauth_client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "online",
            "prompt": "select_account",
        }
    )
    response = RedirectResponse(f"https://accounts.google.com/o/oauth2/v2/auth?{query}")
    response.set_cookie(
        "pistachio_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=settings.env != "local",
        samesite="lax",
    )
    return response


@router.get("/callback")
async def callback(request: Request, code: str, state: str) -> RedirectResponse:
    settings: Settings = request.app.state.services.settings
    cookie_state = request.cookies.get("pistachio_oauth_state", "")
    state_payload = _unsign(settings, state)
    if not hmac.compare_digest(state, cookie_state) or not state_payload:
        raise HTTPException(status_code=400, detail="invalid OAuth state")
    redirect_uri = f"{settings.control_plane_url.rstrip('/')}/auth/callback"
    async with httpx.AsyncClient(timeout=20) as client:
        token_response = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings.oauth_client_id,
                "client_secret": settings.oauth_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_response.raise_for_status()
    claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
        token_response.json()["id_token"],
        GoogleRequest(),  # type: ignore[no-untyped-call]
        audience=settings.oauth_client_id,
    )
    principal = _allow(settings, str(claims.get("email", "")))
    session_seconds = settings.web_session_days * 24 * 3600
    session = _sign(
        settings,
        {"email": principal.id, "exp": int(time.time()) + session_seconds},
    )
    return_to = str(state_payload.get("return_to", "/"))
    if not return_to.startswith("/") or return_to.startswith("//"):
        return_to = "/"
    response = RedirectResponse(return_to, status_code=303)
    response.delete_cookie("pistachio_oauth_state")
    response.set_cookie(
        "pistachio_session",
        session,
        max_age=session_seconds,
        httponly=True,
        secure=settings.env != "local",
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie("pistachio_session")
    return response
