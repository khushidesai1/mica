"""FastAPI application factory for the Pistachio control plane."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from pistachio.agents.context import ContextBuilder
from pistachio.agents.harness import AgentHarness
from pistachio.agents.memory import MemoryManager, ProjectCompactor
from pistachio.api.dependencies import Services
from pistachio.api.errors import install_error_handlers
from pistachio.api.routes import router as api_router
from pistachio.api.slack_analysis import router as slack_analysis_router
from pistachio.config import Settings, get_settings
from pistachio.connectors.gmail import router as gmail_router
from pistachio.connectors.notifications import ChannelNotifier
from pistachio.connectors.slack import router as slack_router
from pistachio.execution.backends import ExecutionBackend, build_executor
from pistachio.services.datasets import DatasetInspector
from pistachio.services.dispatch import build_dispatcher
from pistachio.services.projects import ProjectService
from pistachio.services.tasks import TaskService
from pistachio.services.tools import ensure_bootstrap_tools
from pistachio.services.uploads import UploadService
from pistachio.storage.base import Repository
from pistachio.storage.firestore import FirestoreRepository
from pistachio.storage.memory import InMemoryRepository


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
        await ensure_bootstrap_tools(store)
        yield

    app = FastAPI(
        title="Pistachio Bioinformatics Agent",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.services = services
    app.include_router(api_router)
    app.include_router(slack_router)
    app.include_router(slack_analysis_router)
    app.include_router(gmail_router)
    install_error_handlers(app)
    return app


app = create_app()


def run() -> None:
    uvicorn.run("pistachio.api.app:app", host="0.0.0.0", port=8080)


if __name__ == "__main__":
    run()
