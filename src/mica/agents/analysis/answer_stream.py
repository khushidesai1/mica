"""One best-effort Slack stream per request, with durable crash recovery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from . import access, agent_session, observability, reference_repos, request_controls, surface
from .slack_format import neutralize
from .transport import SlackTransport

if TYPE_CHECKING:
    from .batch import BatchBridge

log = logging.getLogger(__name__)
SETTLED = {"delivered", "narration", "cleaned", "abandoned"}
STREAM_SECONDS = 540


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def transport(bridge: BatchBridge) -> SlackTransport:
    # In particular, never automatically retry the non-idempotent startStream call.
    return SlackTransport(AsyncWebClient(
        token=bridge.app.slack_bot_token, timeout=1, retry_handlers=[],
    ))


async def save(bridge: BatchBridge, event_id: str, record: dict[str, Any]) -> None:
    await bridge.repository.set_connector_state("claude-event:" + event_id, record)


async def cleanup(
    bridge: BatchBridge, event_id: str, record: dict[str, Any], *, tick: int,
) -> None:
    """Retry uncertain deletes on later ticks, including one final terminal retry."""
    state = record.get("stream")
    if not state or state.get("state") in SETTLED:
        return
    terminal = record.get("status") in {"complete", "failed"}
    attempt = f"{tick}:{terminal}"
    if state.get("cleanup_tick") == attempt:
        return
    state["cleanup_tick"] = attempt
    state["state"] = "cleanup_pending"
    client = transport(bridge)
    event = record["payload"]["event"]
    try:
        async with asyncio.timeout(2):
            ts = state.get("ts")
            if not ts:
                # Empty-start orphans cannot contain answer text. Only a unique, empty
                # message from this app, in this thread and the bounded start window,
                # is attributable. An incomplete/ambiguous listing is left untouched.
                reply = await client.client.conversations_replies(
                    channel=event["channel"], ts=event.get("thread_ts") or event["ts"],
                    oldest=str(state["at"]), latest=str(state["at"] + 2), limit=100,
                )
                app_id = record["payload"].get("api_app_id")
                messages: list[dict[str, Any]] = reply.get("messages", [])
                metadata: dict[str, Any] = reply.get("response_metadata", {})
                candidates = [m for m in messages if (
                    app_id and m.get("app_id") == app_id and not m.get("text")
                    and not m.get("blocks") and not m.get("files")
                    and m.get("thread_ts") == (event.get("thread_ts") or event["ts"])
                )]
                if (len(candidates) != 1 or reply.get("has_more")
                        or metadata.get("next_cursor")):
                    raise ValueError("Empty stream cannot be attributed unambiguously")
                ts = state["ts"] = candidates[0]["ts"]
                await save(bridge, event_id, record)
            try:
                await client.delete(channel=event["channel"], ts=ts)
            except SlackApiError as exc:
                if exc.response.get("error") != "message_not_found":
                    raise
            if record.get("notice_ts") == ts:
                record.pop("notice_ts", None)
                # Later ordinary chunks may already be confirmed. Replace only the
                # removed first message; retain their cursor and original chunk size.
                state["replace_first"] = True
            state["state"] = "cleaned"
    except Exception:
        log.warning("Slack stream cleanup pending event=%s", event_id)
    if terminal and state["state"] == "cleanup_pending":
        if "terminal_retry_tick" not in state:
            await bridge.schedule(event_id, tick + 1)
            state["terminal_retry_tick"] = tick + 1
        elif tick >= state["terminal_retry_tick"]:
            await observability.report(
                bridge, "slack_delivery_failed", event_id, record, unit="stream_cleanup",
            )
            state["state"] = "abandoned"
    await save(bridge, event_id, record)


class AnswerStream:
    def __init__(
        self, bridge: BatchBridge, event_id: str, record: dict[str, Any], grant: access.Grant,
        *, init: bool = False,
    ) -> None:
        self.bridge, self.event_id, self.record, self.grant = bridge, event_id, record, grant
        self.enabled = (bridge.settings.slack_streaming and grant.allowed and not init
                        and not surface.is_email(record) and not record.get("ambient"))
        self.client = transport(bridge)
        self.text = ""
        self.sent = 0
        self.text_blocks = 0
        self.tool = False
        self.closed = False
        self.expires = float("inf")

    async def authorized(self) -> bool:
        if await request_controls.cancelled(self.bridge.storage, self.event_id):
            return False
        current = await asyncio.to_thread(
            access.resolve, self.bridge.app, self.bridge.settings, self.bridge.storage, self.record,
        )
        if not current.allowed or current.signature != self.grant.signature:
            return False
        if self.record.get("information_repos"):
            available = current.project.reference_repos | await reference_repos.shared_repos(
                self.bridge.repository, self.bridge.settings,
            )
            return set(self.record["information_repos"]) <= available
        return True

    async def append(self, *, final: bool = False) -> None:
        if self.closed or self.tool or (len(self.text) < 300 and not self.sent):
            return
        # Flush at useful intervals, not on every token. Hold an incomplete angle token
        # until its closing delimiter arrives (or the model finishes).
        end = len(self.text)
        if not final and self.text.rfind("<") > self.text.rfind(">"):
            end = self.text.rfind("<")
        pending = self.text[self.sent:end]
        if not pending or (len(pending) < 300 and not final):
            return
        try:
            if not await self.authorized():
                raise ValueError("Stream authorization changed")
            state = self.record.get("stream")
            event = self.record["payload"]["event"]
            if state is None:
                state = self.record["stream"] = {"state": "starting", "at": time.time()}
                await save(self.bridge, self.event_id, self.record)
                response = await self.client.start_stream(
                    channel=event["channel"], thread_ts=event.get("thread_ts") or event["ts"],
                    recipient_user_id=event["user"],
                    recipient_team_id=self.record["payload"]["team_id"],
                )
                state.update(ts=response["ts"], state="open")
                # This write must complete before *any* answer content is sent.
                await save(self.bridge, self.event_id, self.record)
                self.expires = asyncio.get_running_loop().time() + STREAM_SECONDS
            budget = 10000 - int(state.get("characters", 0))
            end = min(len(pending), budget)
            while end and len(neutralize(pending[:end])) > budget:
                end -= 1
            # Do not cut a token at the cap either. Remainder uses whole answer blocks.
            piece = pending[:end]
            if end < len(pending) and piece.rfind("<") > piece.rfind(">"):
                piece = piece[:piece.rfind("<")]
            if piece:
                await self.client.append_stream(
                    channel=event["channel"], ts=state["ts"], markdown_text=neutralize(piece),
                )
                self.sent += len(piece)
                state["characters"] = int(state.get("characters", 0)) + len(neutralize(piece))
                await save(self.bridge, self.event_id, self.record)
            if len(piece) < len(pending) or state.get("characters", 0) >= 10000:
                await self.stop()
        except Exception:
            # A Slack failure never loses the assembled provider answer. Ambiguous
            # appends are deleted on recovery; normal chunked delivery remains available.
            log.warning("Slack stream unavailable event=%s", self.event_id)
            await self.stop()
            if state := self.record.get("stream"):
                state["state"] = "cleanup_pending"
            self.closed = True

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        state = self.record.get("stream")
        if not state or not state.get("ts"):
            return
        try:
            event = self.record["payload"]["event"]
            await self.client.stop_stream(channel=event["channel"], ts=state["ts"])
            state["state"] = "stopped"
        except Exception:
            state["state"] = "cleanup_pending"
            log.warning("Slack stream stop pending event=%s", self.event_id)
        finally:
            # stopStream defaults the session to active. The durable thread decides.
            agent_session.mark(self.bridge, self.record)
            await save(self.bridge, self.event_id, self.record)
            await agent_session.enqueue(self.bridge, self.record)

    async def create(self, client: Any, request: dict[str, Any]) -> Any:
        if not self.enabled or "stream" in self.record:
            return await client.beta.messages.create(**request)
        self.text, self.sent, self.text_blocks = "", 0, 0
        self.tool, self.closed, self.expires = False, False, float("inf")
        pending: asyncio.Task[Any] | None = None
        try:
            async with client.beta.messages.stream(**request) as stream:
                iterator = stream.__aiter__()
                while True:
                    pending = asyncio.create_task(anext(iterator))
                    wait = max(0, self.expires - asyncio.get_running_loop().time())
                    if not self.closed and wait != float("inf"):
                        done, _ = await asyncio.wait({pending}, timeout=wait)
                        if not done:
                            await self.stop()
                    try:
                        event = await pending
                    except StopAsyncIteration:
                        break
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "tool_use":
                            await self.append(final=True)
                            self.tool = True
                            await self.stop()
                        elif block.type == "text":
                            self.text += ("\n" if self.text_blocks else "") + block.text
                            self.text_blocks += 1
                    elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                        self.text += event.delta.text
                        await self.append()
                response = await stream.get_final_message()
                await self.append(final=True)
                await self.stop()
                state = self.record.get("stream")
                if state and state.get("state") == "stopped":
                    if response.stop_reason == "end_turn":
                        answer = "\n".join(
                            b.text for b in response.content if b.type == "text"
                        ).strip()
                        state["answer_hash"] = fingerprint(answer)
                    elif response.stop_reason == "tool_use":
                        state["state"] = "narration"
                    else:
                        state["state"] = "cleanup_pending"
                    await save(self.bridge, self.event_id, self.record)
                return response
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await self.stop()
