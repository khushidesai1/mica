"""Surface delivery, with one semantic terminal result for email."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .slack_format import activity_fallback

log = logging.getLogger(__name__)


class Transport(Protocol):
    async def post(self, **kwargs: Any) -> Any: ...
    async def update(self, **kwargs: Any) -> Any: ...
    async def upload(self, **kwargs: Any) -> Any: ...


class SlackTransport:
    def __init__(self, client: AsyncWebClient) -> None:
        self.client = client
        self.activity_format = 0

    async def post(self, **kwargs: Any) -> Any:
        return await self._message("chat_postMessage", kwargs)

    async def update(self, **kwargs: Any) -> Any:
        return await self._message("chat_update", kwargs)

    async def _message(self, method: str, kwargs: dict[str, Any]) -> Any:
        blocks = kwargs.get("blocks") or []
        has_activity = any(b.get("type") == "plan" for b in blocks)
        while True:
            if has_activity and self.activity_format:
                kwargs["blocks"] = activity_fallback(blocks)
            try:
                return await getattr(self.client, method)(**kwargs)
            except SlackApiError as exc:
                if not (
                    has_activity
                    and self.activity_format < 1
                    and exc.response.get("error") == "invalid_blocks"
                ):
                    raise
                self.activity_format += 1
                log.warning(
                    "Slack activity format rejected; using compatibility level %s",
                    self.activity_format,
                )

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
