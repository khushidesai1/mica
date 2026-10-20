"""Surface delivery, with one semantic terminal result for email."""

from __future__ import annotations

from typing import Any, Protocol

from slack_sdk.web.async_client import AsyncWebClient


class Transport(Protocol):
    async def post(self, **kwargs: Any) -> Any: ...
    async def update(self, **kwargs: Any) -> Any: ...
    async def upload(self, **kwargs: Any) -> Any: ...


class SlackTransport:
    def __init__(self, client: AsyncWebClient) -> None:
        self.client = client

    async def post(self, **kwargs: Any) -> Any:
        return await self.client.chat_postMessage(**kwargs)

    async def update(self, **kwargs: Any) -> Any:
        return await self.client.chat_update(**kwargs)

    async def upload(self, **kwargs: Any) -> Any:
        return await self.client.files_upload_v2(**kwargs)


class EmailWorkerTransport:
    """Worker progress stays local; the authenticated terminal callback sends the MIME result."""

    async def post(self, **kwargs: Any) -> Any:
        return {"ts": "email"}

    async def update(self, **kwargs: Any) -> Any:
        return {"ts": "email"}

    async def upload(self, **kwargs: Any) -> Any:
        raise RuntimeError("Email artifacts must travel with the terminal result")
