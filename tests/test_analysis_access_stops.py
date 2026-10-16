"""Deterministic access outages and stopped publication; no live models or waits."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError

from mica.agents.analysis import access, channel_access, write_broker, write_worker
from mica.agents.analysis.config import Settings
from mica.agents.analysis.identity import IdentityUnavailable
from mica.agents.analysis.runner import Context, TurnResult
from mica.agents.analysis.storage import LocalStorage


@pytest.mark.parametrize("failure", [TimeoutError(), OSError(), "internal_error", "ratelimited"])
def test_profile_outage_is_unknown_and_not_cached(monkeypatch, failure):
    channel_access._CACHE.clear()
    client = Mock()
    client.auth_test.return_value = {"ok": True, "team_id": "T"}
    client.users_info.side_effect = (
        SlackApiError("Unavailable", {"ok": False, "error": failure})
        if isinstance(failure, str)
        else failure
    )
    monkeypatch.setattr(channel_access, "client_for", lambda _: client)
    settings = Settings(_env_file=None, slack_bot_token="fake", access_cache_seconds=30)
    with pytest.raises(IdentityUnavailable):
        channel_access.profile(settings, settings, "T", "U")
    client.users_info.side_effect = None
    client.users_info.return_value = {
        "ok": True,
        "user": {"id": "U", "profile": {"email": "person@example.com"}},
    }
    assert channel_access.profile(settings, settings, "T", "U") == "person@example.com"
    assert client.users_info.call_count == 2


@pytest.mark.parametrize(
    "response",
    [
        {"ok": False, "error": "user_not_found"},
        {"ok": True, "user": {"id": "U", "deleted": True}},
        {"ok": True, "user": {"id": "U", "profile": {}}},
    ],
)
def test_definite_identity_denial_is_not_cached(monkeypatch, response):
    channel_access._CACHE.clear()
    client = Mock()
    client.auth_test.return_value = {"ok": True, "team_id": "T"}
    client.users_info.return_value = response
    monkeypatch.setattr(channel_access, "client_for", lambda _: client)
    settings = Settings(_env_file=None, slack_bot_token="fake")
    assert channel_access.profile(settings, settings, "T", "U") is None
    assert channel_access.profile(settings, settings, "T", "U") is None
    assert client.users_info.call_count == 2


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(access, "time", SimpleNamespace(monotonic=lambda: now[0]))
    sleep = asyncio.sleep

    async def advance(seconds):
        now[0] += seconds
        await sleep(0)

    monkeypatch.setattr(access.asyncio, "sleep", advance)
    return now


async def test_transient_checks_keep_running_then_recover(clock, caplog):
    check = Mock(side_effect=[True, *[IdentityUnavailable("timeout") for _ in range(3)], True])
    monitor = access.AccessMonitor(check, 1800, "test-event")
    assert await monitor.allowed(delivery=True)
    for _ in range(3):
        clock[0] += 400
        assert await monitor.allowed()
    assert not monitor.stop_reason
    assert await monitor.allowed(delivery=True)
    assert caplog.text.count("access_check_unavailable event=test-event") == 3


async def test_outage_grace_expiry_and_definite_revocation(clock):
    check = Mock(side_effect=[True, IdentityUnavailable("timeout"), IdentityUnavailable("timeout")])
    monitor = access.AccessMonitor(check, 120, "event")
    assert await monitor.allowed()
    assert await monitor.allowed()
    clock[0] += 121
    assert not await monitor.allowed()
    assert monitor.stop_reason == "access_unverifiable"
    revoked = access.AccessMonitor(Mock(side_effect=[True, False]), 1800, "event")
    assert await revoked.allowed()
    assert not await revoked.allowed()
    assert revoked.stop_reason == "access_revoked"


async def test_delivery_waits_for_verified_access_or_fails_closed(clock):
    check = Mock(side_effect=[True, IdentityUnavailable("timeout"), True])
    monitor = access.AccessMonitor(check, 120, "event")
    assert await monitor.allowed()
    assert await monitor.allowed(delivery=True)
    assert clock[0] == 101
    check.side_effect = IdentityUnavailable("timeout")
    start = clock[0]
    assert not await monitor.allowed(delivery=True)
    assert clock[0] - start == 120
    assert monitor.stop_reason == "access_unverifiable"


async def test_cancel_blocked_publisher_preserves_receipts_and_partial_totals(tmp_path):
    store = LocalStorage(tmp_path / "store")
    ctx = Context(tmp_path / "work", tmp_path / "data", "project", "brief", thread_ts="1")
    folder = ctx.workdir / "outputs/gcs/images"
    folder.mkdir(parents=True)
    for n in range(2000):
        (folder / f"{n:04}.txt").write_text("test")
    event = "a" * 32
    publisher = write_worker.Publisher(store, {"write_grant": {"uri": "gs://test/"}}, event, "url")
    started = asyncio.Event()

    async def blocked(paths, outcome):
        for relative in list(paths)[:17]:
            publisher.uploaded[relative] = "gs://test/" + relative
            store.put(write_broker.receipt_key(event, relative), b"{}")
        # One controller-confirmed object whose HTTP response was lost.
        store.put(write_broker.receipt_key(event, list(paths)[17]), b"{}")
        started.set()
        await asyncio.Event().wait()

    publisher.transfer = blocked
    task = asyncio.create_task(publisher.publish(ctx, TurnResult(), folder="outputs/gcs/images"))
    await asyncio.wait_for(started.wait(), 2)
    await asyncio.wait_for(publisher.cancel(), 1)
    assert task.cancelled() and not publisher.lock.locked()
    assert len(store.keys(f"write-access/uploads/{event}/")) == 18
    assert write_broker.stopped_summary(store, event, "gs://test/") == (
        "`gs://test/images/`: 18 of 2,000 files published; republish before use."
    )
    result = await publisher.publish(ctx, TurnResult(), folder="outputs/gcs/images")
    assert result.errors and not result.uploaded
    index = json.loads(store.get(write_broker.manifest_stage(event))[0])
    assert index["destinations"][0]["files"] == 17
    assert index["destinations"][0]["errors"] == 1983


async def test_sdk_interrupt_itself_is_bounded_and_tools_cancel_first(monkeypatch):
    from claude_agent_sdk import ClaudeAgentOptions

    from mica.agents.analysis import recovery_tools

    disconnected = asyncio.Event()
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def query(self, prompt):
            pass

        async def interrupt(self):
            calls.append("interrupt")
            await asyncio.Event().wait()

        async def disconnect(self):
            calls.append("disconnect")
            disconnected.set()

        async def receive_response(self):
            await disconnected.wait()
            return
            yield  # Make this the same async iterator interface as the SDK.

    async def cancel_tools():
        calls.append("cancel_tools")

    async def wait_for(task, timeout):
        assert timeout <= 5
        return await asyncio.wait_for(task, 0.01)

    async def sleep(seconds):
        assert seconds == 30

    monkeypatch.setattr(recovery_tools, "ClaudeSDKClient", lambda **kw: Client())
    monkeypatch.setattr(
        recovery_tools,
        "asyncio",
        SimpleNamespace(
            create_task=asyncio.create_task,
            wait_for=wait_for,
            sleep=sleep,
            CancelledError=asyncio.CancelledError,
        ),
    )
    stop = asyncio.Event()
    stop.set()
    manager = SimpleNamespace(stop=stop, stop_reason="access_revoked", stop_tools=cancel_tools)
    async for _ in recovery_tools.run_query("test", ClaudeAgentOptions(), manager):
        pass
    assert calls == ["cancel_tools", "interrupt", "disconnect"]


def test_process_backstop_is_armed_within_one_minute(monkeypatch):
    from mica.agents.analysis import batch_worker

    timer = Mock()
    constructor = Mock(return_value=timer)
    monkeypatch.setattr(batch_worker.threading, "Timer", constructor)
    batch_worker.arm_stop_deadline()
    assert constructor.call_args.args[0] == 45
    assert constructor.call_args.args[1] is batch_worker.os._exit
    assert timer.daemon
    timer.start.assert_called_once()
