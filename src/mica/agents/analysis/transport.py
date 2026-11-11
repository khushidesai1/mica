"""Surface delivery, with one semantic terminal result for email."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from html import unescape
from pathlib import Path
from typing import Any, Protocol

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from .outputs import MAX_ATTACHMENT_BYTES
from .slack_format import activity_fallback, to_mrkdwn

log = logging.getLogger(__name__)


def upload_batches(uploads: list[dict[str, str]]) -> Iterator[list[dict[str, str]]]:
    """Bound SDK memory use and the async client's completion URL parameters."""
    batch: list[dict[str, str]] = []
    size = 0
    for upload in uploads:
        length = Path(upload["file"]).stat().st_size
        if batch and (len(batch) >= 5 or size + length > MAX_ATTACHMENT_BYTES):
            yield batch
            batch, size = [], 0
        batch.append(upload)
        size += length
    if batch:
        yield batch


class Transport(Protocol):
    async def post(self, **kwargs: Any) -> Any: ...
    async def update(self, **kwargs: Any) -> Any: ...
    async def upload(self, **kwargs: Any) -> Any: ...


class SlackTransport:
    def __init__(self, client: AsyncWebClient) -> None:
        self.client = client
        self.activity_format = 0
        self.markdown_fallback = False

    async def post(self, **kwargs: Any) -> Any:
        return await self._message("chat_postMessage", kwargs)

    async def update(self, **kwargs: Any) -> Any:
        return await self._message("chat_update", kwargs)

    async def set_status(self, **kwargs: Any) -> Any:
        return await self.client.api_call("agents.sessions.setStatus", json=kwargs)

    async def start_stream(self, **kwargs: Any) -> Any:
        return await self.client.api_call("chat.startStream", json=kwargs)

    async def append_stream(self, **kwargs: Any) -> Any:
        return await self.client.api_call("chat.appendStream", json=kwargs)

    async def stop_stream(self, **kwargs: Any) -> Any:
        return await self.client.api_call("chat.stopStream", json=kwargs)

    async def delete(self, **kwargs: Any) -> Any:
        return await self.client.chat_delete(**kwargs)

    async def set_prompts(self, **kwargs: Any) -> Any:
        return await self.client.api_call("assistant.threads.setSuggestedPrompts", json=kwargs)

    async def _message(self, method: str, kwargs: dict[str, Any]) -> Any:
        blocks = kwargs.get("blocks") or []
        has_activity = any(b.get("type") == "plan" for b in blocks)
        has_markdown = any(b.get("type") == "markdown" for b in blocks)
        while True:
            if has_activity and self.activity_format:
                kwargs["blocks"] = activity_fallback(blocks)
            if has_markdown and self.markdown_fallback:
                kwargs["blocks"] = []
                kwargs["text"] = to_mrkdwn("\n".join(
                    unescape(b["text"]) for b in blocks if b.get("type") == "markdown"
                ))
            try:
                return await getattr(self.client, method)(**kwargs)
            except SlackApiError as exc:
                if has_markdown and not self.markdown_fallback and (
                    exc.response.get("error") == "invalid_blocks"
                ):
                    self.markdown_fallback = True
                    continue
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
