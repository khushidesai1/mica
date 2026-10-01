"""Minimal authenticated browser interface."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from pistachio.api.auth import get_principal
from pistachio.api.dependencies import Services

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/assets/pista_logo.svg", include_in_schema=False)
async def logo() -> FileResponse:
    return FileResponse(
        Path(__file__).parent / "static" / "pista_logo.svg",
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/")
async def home(request: Request) -> Response:
    services: Services = request.app.state.services
    try:
        principal = await get_principal(request)
    except HTTPException as exc:
        if exc.status_code == 401 and services.settings.auth_mode == "oidc":
            return_to = request.url.path
            if request.url.query:
                return_to += f"?{request.url.query}"
            return RedirectResponse(f"/auth/login?{urlencode({'next': return_to})}")
        raise
    projects = await services.repository.list_projects(principal.id)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"principal": principal.id, "projects": projects},
    )
