from __future__ import annotations

import asyncio
import logging

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from .config import Settings
from .preflight import check
from .projects import Projects
from .runner import Runner
from .sessions import Sessions
from .slack_app import SlackAgent, register
from .storage import GCSStorage, LocalStorage, Storage


async def main() -> None:
    settings = Settings()
    settings.require_credentials(slack=True)
    check(settings)
    storage: Storage = (
        GCSStorage(settings.bucket)
        if settings.storage == "gcs"
        else LocalStorage(settings.local_storage_root)
    )
    projects = Projects(storage, settings)
    await asyncio.to_thread(projects.reload)
    sessions = Sessions(settings.state_db)
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())
    auth = await app.client.auth_test()
    agent = SlackAgent(settings, projects, sessions, Runner(settings), app.client, auth["user_id"])
    register(app, agent)
    handler = AsyncSocketModeHandler(app, settings.slack_app_token.get_secret_value())
    try:
        await handler.start_async()  # type: ignore[no-untyped-call]
    finally:
        await handler.close_async()  # type: ignore[no-untyped-call]
        if agent.tasks:
            await asyncio.gather(*agent.tasks, return_exceptions=True)
        sessions.close()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())


if __name__ == "__main__":
    run()
