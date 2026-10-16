"""Stable HTTP error mapping for service-layer exceptions."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from mica.services.projects import ProjectAccessDenied, ProjectNotFound
from mica.services.tasks import TaskNotFound


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProjectNotFound)
    @app.exception_handler(TaskNotFound)
    async def not_found(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ProjectAccessDenied)
    async def forbidden(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})
