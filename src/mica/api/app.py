"""FastAPI application factory for the Mica control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from mica.agents.context import ContextBuilder
from mica.agents.harness import AgentHarness
from mica.agents.memory import MemoryManager, ProjectCompactor
from mica.api.atomx import router as atomx_router
from mica.api.dependencies import Services
from mica.api.errors import install_error_handlers
from mica.api.routes import router as api_router
from mica.api.slack_analysis import router as slack_analysis_router
from mica.config import Settings, get_settings
from mica.connectors.gmail import router as gmail_router
from mica.connectors.notifications import ChannelNotifier
from mica.connectors.slack import router as slack_router
from mica.execution.backends import ExecutionBackend, build_executor
from mica.services.datasets import DatasetInspector
from mica.services.dispatch import build_dispatcher
from mica.services.projects import ProjectService
from mica.services.tasks import TaskService
from mica.services.tools import ensure_bootstrap_tools
from mica.services.uploads import UploadService
from mica.storage.base import Repository
from mica.storage.firestore import FirestoreRepository
from mica.storage.memory import InMemoryRepository


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    executor: ExecutionBackend | None = None,
) -> FastAPI:
    config = settings or get_settings()
    store = repository or (
        FirestoreRepository(config.gcp_project or None)
        if config.store_backend == "firestore"
        else InMemoryRepository()
    )
    memory = MemoryManager(store, config)
    compactor = ProjectCompactor(store, config)
    harness = AgentHarness(store, config)
    context_builder = ContextBuilder(store, memory, config)
    task_service = TaskService(
        store,
        config,
        harness,
        context_builder,
        memory,
        compactor,
        executor or build_executor(config),
        ChannelNotifier(config, store),
        DatasetInspector(config.gcp_project or None) if config.env != "test" else None,
    )
    services = Services(
        settings=config,
        repository=store,
        projects=ProjectService(store),
        tasks=task_service,
        memory=memory,
        compactor=compactor,
        harness=harness,
        uploads=UploadService(config),
        dispatcher=build_dispatcher(config, task_service),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        from mica.agents.analysis.atomx_connections import Connections
        from mica.agents.analysis.config import Settings as AnalysisSettings

        analysis = AnalysisSettings()
        if analysis.transfers_enabled:
            Connections(config, analysis, store).validate_runtime()
        await ensure_bootstrap_tools(store)
        yield

    app = FastAPI(
        title="Mica Bioinformatics Agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.services = services
    app.include_router(api_router)
    app.include_router(atomx_router)
    app.include_router(slack_router)
    app.include_router(slack_analysis_router)
    app.include_router(gmail_router)
    install_error_handlers(app)
    return app


app = create_app()


def run() -> None:
    uvicorn.run("mica.api.app:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
