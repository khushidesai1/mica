"""Private integration form primitives shared by providers."""

from __future__ import annotations

import json
import secrets
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def response(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=HEADERS)


def origin(request: Request, control_url: str) -> None:
    parts = urlsplit(control_url)
    if request.headers.get("origin") != f"{parts.scheme}://{parts.netloc}":
        raise ValueError("Open the private form on Mica's configured address.")


async def body(request: Request, maximum: int = 8192) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > maximum:
            raise ValueError("Form submission is too large.")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Invalid form submission.")
    return value


def page_response(source: str) -> Response:
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(
        source.replace("NONCE", nonce),
        headers={
            **HEADERS,
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
        },
    )
