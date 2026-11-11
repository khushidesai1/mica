"""Scripted provider streams exercise durable delivery without a network or model."""

# ruff: noqa: F811
import asyncio
import copy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_conversation_tools import Block, Client, reply, run, tool, turn  # noqa: F401

from mica.agents.analysis import answer_stream, direct
from mica.agents.analysis.slack_format import neutralize
from mica.agents.analysis.transport import SlackTransport


class StreamClient(Client):
    def __init__(self, *frames, pause=0):
        super().__init__(*frames)
        self.stream_calls = 0
        self.pause = pause

    def stream(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        self.stream_calls += 1
        frame = self.frames.pop(0)
        return ModelStream(frame, self.pause)


class ModelStream:
    def __init__(self, frame, pause):
        self.response = SimpleNamespace(stop_reason=frame[0], content=[Block(b) for b in frame[1]])
        self.pause = pause

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def __aiter__(self):
        for block in self.response.content:
            yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(
                type=block.type, text="",
            ))
            if block.type in {"text", "thinking"}:
                text = getattr(block, block.type)
                for offset in range(0, len(text), 101):
                    yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(
                        type=block.type + "_delta", **{block.type: text[offset:offset + 101]},
                    ))
                    if offset == 303 and self.pause:
                        await asyncio.sleep(self.pause)

    async def get_final_message(self):
        return self.response


@pytest.fixture
def streaming(turn, monkeypatch):
    bridge, event_id, record, _ = turn
    bridge.settings.slack_streaming = True
    bridge.settings.slack_sessions = True
    slack = bridge.test_slack

    async def call(method, *, json):
        saved = await bridge.repository.get_connector_state("claude-event:" + event_id)
        if method == "chat.startStream":
            assert "markdown_text" not in json and "chunks" not in json
            assert saved["stream"]["state"] == "starting" and "ts" not in saved["stream"]
            return {"ts": "8.0"}
        if method == "chat.appendStream":
            assert saved["stream"]["ts"] == "8.0"
        return {"ok": True}

    slack.api_call.side_effect = call
    monkeypatch.setattr(answer_stream, "transport", lambda bridge: SlackTransport(slack))
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(answer_stream.agent_session, "enqueue", enqueue)
    return slack, enqueue


def calls(slack, method):
    return [c.kwargs["json"] for c in slack.api_call.call_args_list if c.args == (method,)]


@pytest.mark.parametrize("length", [24, 304, 7000, 16000])
async def test_short_whole_long_streamed_full_answer_and_activity_retained(turn, streaming, length):
    bridge, event_id, record, _ = turn
    slack, enqueue = streaming
    text = "x" * length
    record.update(notice_ts="9.0", notice_title="Reading files", notice_text="Reading files")
    client = StreamClient(("end_turn", [
        {"type": "thinking", "thinking": "PRIVATE REASONING " * 100, "signature": "signed"},
        {"type": "text", "text": text},
    ]))
    decision = await run(turn, client)
    assert decision.reply == text
    appends = calls(slack, "chat.appendStream")
    assert bool(appends) is (length >= 300)
    assert sum(len(c["markdown_text"]) for c in appends) == (
        min(length, 10000) if length >= 300 else 0
    )
    assert "PRIVATE" not in str(slack.api_call.call_args_list)
    if length >= 300:
        enqueue.assert_awaited_once()
    slack.chat_update.reset_mock()
    slack.chat_postMessage.reset_mock()
    await direct.finish(bridge, event_id, record, text)
    assert record["terminal_reply"] == record["notice_text"] == text
    rendered = [c.kwargs["blocks"][0]["text"] for c in (
        slack.chat_update.call_args_list + slack.chat_postMessage.call_args_list
    ) if c.kwargs.get("blocks", [{}])[0].get("type") == "markdown"]
    assert "".join(rendered) == text
    if length >= 300:
        assert record["stream"]["state"] == "delivered"
        assert record["notice_ts"] == "8.0" and record["activity_ts"] == "9.0"
        assert slack.chat_update.call_args_list[0].kwargs["blocks"][0]["title"] == "Mica finished"
    updates = slack.chat_update.await_count
    await direct.finish(bridge, event_id, record, text)
    assert slack.chat_update.await_count == updates


@pytest.mark.parametrize("length", [50, 600])
async def test_tool_classification_and_one_stream_per_request(turn, streaming, length):
    bridge, _, record, grant = turn
    frame = tool("read_sources", repos=[], file_ids=[])
    frame[1].insert(0, {"type": "text", "text": "N" * length})
    client = StreamClient(frame, reply("Final " * 100))
    result = await run(turn, client)
    assert result.reply == "Final " * 99 + "Final"
    slack, _ = streaming
    assert len(calls(slack, "chat.startStream")) == 1
    if length >= 300:
        assert record["stream"]["state"] == "narration"
        assert client.stream_calls == 1 and len(client.calls) == 2
        narration = "".join(c["markdown_text"] for c in calls(slack, "chat.appendStream"))
        assert narration == "N" * length
    else:
        assert client.stream_calls == 2  # Short narration never consumed the single stream.
        assert record["stream"]["answer_hash"] == answer_stream.fingerprint(result.reply)


async def test_split_angle_tokens_and_channel_recipients(turn, streaming, monkeypatch):
    bridge, event_id, record, grant = turn
    record["payload"]["event"]["channel"] = "C1"
    stream = answer_stream.AnswerStream(bridge, event_id, record, grant)
    monkeypatch.setattr(stream, "authorized", AsyncMock(return_value=True))
    # The first delta ends in an unfinished angle token beyond the flush threshold.
    text = "a" * 402 + "<!here> <@U123> [source](https://example.org)"
    await stream.create(StreamClient(reply(text)), {})
    slack, _ = streaming
    start = calls(slack, "chat.startStream")[0]
    assert start["recipient_user_id"] == "U1" and start["recipient_team_id"] == "T1"
    assert start["channel"] == "C1"
    appends = calls(slack, "chat.appendStream")
    assert "".join(c["markdown_text"] for c in appends) == neutralize(text)
    assert all(not c["markdown_text"].endswith("&lt;!") for c in appends)


@pytest.mark.parametrize("state", [
    {"state": "starting", "at": 10},
    {"state": "open", "at": 10, "ts": "8.0", "characters": 500},
])
async def test_recovery_deletes_orphan_and_never_starts_again(turn, streaming, state):
    bridge, event_id, record, _ = turn
    record["payload"]["api_app_id"] = "A1"
    record["stream"] = dict(state)
    slack, _ = streaming
    slack.conversations_replies.return_value = {"messages": [
        {"app_id": "OTHER", "text": "", "ts": "10.1", "thread_ts": "1.0"},
        {"app_id": "A1", "text": "", "ts": "8.0", "thread_ts": "1.0"},
    ]}
    await answer_stream.cleanup(bridge, event_id, record, tick=1)
    slack.chat_delete.assert_awaited_once_with(channel="D1", ts="8.0")
    client = StreamClient(reply("whole " * 150))
    decision = await run(turn, client)
    assert client.stream_calls == 0
    await direct.finish(bridge, event_id, record, decision.reply)
    assert record["notice_text"] == decision.reply
    assert not calls(slack, "chat.startStream")


async def test_failed_cleanup_does_not_block_answer_and_gets_one_terminal_retry(
    turn, streaming, monkeypatch,
):
    bridge, event_id, record, _ = turn
    record["stream"] = {"state": "open", "ts": "8.0", "at": time.time()}
    slack, _ = streaming
    slack.chat_delete.side_effect = RuntimeError("ambiguous network failure")
    report = AsyncMock()
    monkeypatch.setattr(answer_stream.observability, "report", report)
    await answer_stream.cleanup(bridge, event_id, record, tick=0)
    await direct.finish(bridge, event_id, record, "Whole answer")
    assert record["notice_text"] == "Whole answer"
    await answer_stream.cleanup(bridge, event_id, record, tick=0)
    bridge.schedule.assert_awaited_with(event_id, 1)
    await answer_stream.cleanup(bridge, event_id, record, tick=1)
    report.assert_awaited_once()
    assert record["stream"]["state"] == "abandoned"
    attempts = slack.chat_delete.await_count
    await answer_stream.cleanup(bridge, event_id, record, tick=2)
    assert slack.chat_delete.await_count == attempts


async def test_unknown_orphan_ambiguity_never_deletes_another_message(turn, streaming):
    bridge, event_id, record, _ = turn
    record["stream"] = {"state": "starting", "at": 10}
    record["payload"]["api_app_id"] = "A1"
    slack, _ = streaming
    slack.conversations_replies.return_value = {"messages": [
        {"app_id": "A1", "text": "", "ts": ts, "thread_ts": "1.0"} for ts in ("10.1", "10.2")
    ]}
    await answer_stream.cleanup(bridge, event_id, record, tick=1)
    slack.chat_delete.assert_not_called()
    assert record["stream"]["state"] == "cleanup_pending"


@pytest.mark.parametrize("reason", ["access", "cancel", "repository"])
async def test_revocation_or_cancel_prevents_more_appends(turn, streaming, monkeypatch, reason):
    bridge, event_id, record, grant = turn
    stream = answer_stream.AnswerStream(bridge, event_id, record, grant)
    stream.text = "a" * 400
    await stream.append()
    slack, _ = streaming
    assert len(calls(slack, "chat.appendStream")) == 1
    if reason == "cancel":
        monkeypatch.setattr(
            answer_stream.request_controls, "cancelled", AsyncMock(return_value=True),
        )
    elif reason == "repository":
        record["information_repos"] = ["owner/removed"]
    else:
        bridge.app.slack_user_map = {}
    stream.text += "never sent " * 100
    await stream.append()
    assert len(calls(slack, "chat.appendStream")) == 1
    assert record["stream"]["state"] == "cleanup_pending"


async def test_nine_minute_cap_stops_even_while_provider_stalls(turn, streaming, monkeypatch):
    bridge, event_id, record, grant = turn
    monkeypatch.setattr(answer_stream, "STREAM_SECONDS", 0.01)
    result = await answer_stream.AnswerStream(bridge, event_id, record, grant).create(
        StreamClient(reply("a" * 1000), pause=0.04), {},
    )
    slack, _ = streaming
    assert result.content[0].text == "a" * 1000
    assert len(calls(slack, "chat.stopStream")) == 1
    assert sum(len(c["markdown_text"]) for c in calls(slack, "chat.appendStream")) == 303


async def test_model_deadline_stops_partial_stream(turn, streaming):
    bridge, event_id, record, grant = turn
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.02):
            await answer_stream.AnswerStream(bridge, event_id, record, grant).create(
                StreamClient(reply("a" * 1000), pause=0.2), {},
            )
    assert len(calls(streaming[0], "chat.stopStream")) == 1
    assert "answer_hash" not in record["stream"]


async def test_flag_off_uses_unchanged_provider_call(turn, streaming):
    turn[0].settings.slack_streaming = False
    client = StreamClient(reply("Long " * 100))
    await run(turn, client)
    assert client.stream_calls == 0 and not calls(streaming[0], "chat.startStream")


async def test_partial_rollover_retry_replaces_stream_without_resending_confirmed_parts(
    turn, streaming,
):
    bridge, event_id, record, _ = turn
    # The conversation's non-flexible response cap is irrelevant to delivery recovery.
    text = "a" * 10000 + "b" * 10000 + "c" * 50
    record["stream"] = {"state": "stopped", "ts": "8.0", "at": time.time(),
                        "answer_hash": answer_stream.fingerprint(text)}
    slack, _ = streaming
    slack.chat_postMessage.side_effect = [
        {"ts": "10.0"}, RuntimeError("temporary failure"), {"ts": "11.0"}, {"ts": "12.0"},
    ]
    await bridge.notify(event_id, record, "complete", text)
    assert "notice_text" not in record
    saved = await bridge.repository.get_connector_state("claude-event:" + event_id)
    saved = copy.deepcopy(saved)
    assert saved["notice_chunks"]["sent"] == 2
    await answer_stream.cleanup(bridge, event_id, saved, tick=1)
    await direct.finish(bridge, event_id, saved, text)
    assert saved["notice_text"] == text
    posts = [c.kwargs["text"] for c in slack.chat_postMessage.call_args_list]
    assert posts == ["b" * 10000, "c" * 50, "a" * 10000, "c" * 50]
    assert slack.chat_delete.call_args.kwargs["ts"] == "8.0"


async def test_start_id_save_failure_sends_no_content_and_falls_back(turn, streaming, monkeypatch):
    bridge, event_id, record, grant = turn
    original = bridge.repository.set_connector_state
    failed = False

    async def save(key, value):
        nonlocal failed
        if value.get("stream", {}).get("ts") and not failed:
            failed = True
            raise RuntimeError("receipt write interrupted")
        return await original(key, value)

    monkeypatch.setattr(bridge.repository, "set_connector_state", save)
    result = await answer_stream.AnswerStream(bridge, event_id, record, grant).create(
        StreamClient(reply("Answer " * 100)), {},
    )
    assert result.content[0].text == "Answer " * 100
    assert not calls(streaming[0], "chat.appendStream")
    await direct.finish(bridge, event_id, record, result.content[0].text)
    assert record["notice_text"] == result.content[0].text
