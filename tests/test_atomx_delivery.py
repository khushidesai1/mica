"""Private AtoMx delivery works with existing DMs and terminates permanent refusals."""

# ruff: noqa: F811
from types import SimpleNamespace

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_slack_response import AsyncSlackResponse
from test_analysis_batch import bridge, payload  # noqa: F401
from test_atomx_connections import KEY, A, connections  # noqa: F401

from mica.agents.analysis import access, atomx_connections, atomx_delivery, transfers


def refusal(error="missing_scope"):
    return SlackApiError(
        "provider details must stay private",
        AsyncSlackResponse(
            client=None,
            http_verb="POST",
            api_url="https://slack.com/api/conversations.open",
            req_args={},
            data={"ok": False, "error": error},
            headers={},
            status_code=200,
        ),
    )


@pytest.fixture(autouse=True)
def private_delivery_client(bridge, monkeypatch):
    monkeypatch.setattr(atomx_delivery, "AsyncWebClient", lambda **kwargs: bridge.test_slack)


@pytest.fixture
def private_record(bridge, monkeypatch):
    monkeypatch.setattr(access, "resolve", lambda *args: SimpleNamespace(email=A, private=True))
    return {
        "surface": "slack",
        "intake_principal": A,
        "payload": {
            "event": {"user": "U1", "channel": "D1", "channel_type": "im", "ts": "2.0"}
        },
    }


async def test_existing_dm_does_not_need_im_write(bridge, private_record):
    bridge.test_slack.conversations_open.side_effect = refusal()
    await atomx_delivery.deliver(bridge, A, private_record, "event", "private-link", "connection")
    bridge.test_slack.conversations_open.assert_not_awaited()
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["channel"] == "D1"
    await atomx_delivery.deliver(bridge, A, private_record, "event", "private-link", "connection")
    bridge.test_slack.chat_postMessage.assert_awaited_once()


@pytest.mark.parametrize("thread", [None, "1.0"])
@pytest.mark.parametrize(
    ("state", "text", "buttons"),
    [
        ("inventory", "Checking AtoMx…", ("cancel",)),
        ("preview", "Transfer this sample?", ("confirm", "cancel")),
        ("running", "AtoMx transfer started.", ("cancel",)),
        ("done", "Transfer complete.", ()),
        ("failed", "AtoMx job failed.", ()),
    ],
)
async def test_transfer_notices_stay_in_request_dm_thread(
    bridge, private_record, thread, state, text, buttons
):
    bridge.app.signed_link_secret = KEY
    if thread:
        private_record["payload"]["event"]["thread_ts"] = thread
    await bridge.repository.set_connector_state("claude-event:event", private_record)
    pending = {"id": "event", "state": state, "requested_by": A, "attempt": 0}
    sender = transfers.Transfers(bridge, bridge.store)
    await sender.notify(pending, text, buttons)
    await sender.notify(pending, text, buttons)
    bridge.test_slack.chat_postMessage.assert_awaited_once()
    message = bridge.test_slack.chat_postMessage.call_args.kwargs
    assert message["channel"] == "D1"
    assert message["thread_ts"] == (thread or "2.0")
    assert not message.get("reply_broadcast")
    bridge.test_slack.conversations_open.assert_not_awaited()


async def test_shared_conversation_uses_separate_dm(bridge, private_record, monkeypatch):
    monkeypatch.setattr(access, "resolve", lambda *args: SimpleNamespace(email=A, private=False))
    private_record["payload"]["event"].update(
        channel="CPROJECT", channel_type="channel", thread_ts="1.0"
    )
    bridge.test_slack.conversations_open.return_value = {"channel": {"id": "DPRIVATE"}}
    await atomx_delivery.deliver(bridge, A, private_record, "event", "private-link", "connection")
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["channel"] == "DPRIVATE"
    assert bridge.test_slack.chat_postMessage.call_args.kwargs["thread_ts"] is None


async def test_missing_open_scope_is_terminal_and_never_leaks_link_to_channel(
    bridge, private_record
):
    private_record["payload"]["event"].update(channel="CPROJECT", channel_type="channel")
    bridge.test_slack.conversations_open.side_effect = refusal()
    with pytest.raises(ValueError, match="one-to-one DM") as error:
        await atomx_delivery.deliver(
            bridge, A, private_record, "event", "private-link", "connection"
        )
    assert "private-link" not in str(error.value)
    bridge.test_slack.chat_postMessage.assert_not_awaited()
    assert (
        next(iter(bridge.repository.failures.values())).observation.cause == "atomx_delivery_failed"
    )


async def test_transient_slack_error_remains_retryable_without_logging_link(bridge, private_record):
    bridge.test_slack.chat_postMessage.side_effect = TimeoutError("private-link")
    with pytest.raises(RuntimeError, match="needs retry") as error:
        await atomx_delivery.deliver(
            bridge, A, private_record, "event", "private-link", "connection"
        )
    assert "private-link" not in str(error.value)
    assert "private-link" not in str(bridge.repository.failures)


async def test_unverified_owner_never_receives_link(bridge, private_record):
    with pytest.raises(ValueError, match="verify a private recipient"):
        await atomx_delivery.deliver(
            bridge,
            "someone-else@example.com",
            private_record,
            "event",
            "private-link",
            "connection",
        )
    bridge.test_slack.chat_postMessage.assert_not_awaited()
    bridge.test_slack.conversations_open.assert_not_awaited()


async def test_open_response_cannot_redirect_private_link_to_shared_channel(bridge, private_record):
    private_record["payload"]["event"].update(channel="CPROJECT", channel_type="channel")
    bridge.test_slack.conversations_open.return_value = {"channel": {"id": "CPROJECT"}}
    with pytest.raises(ValueError):
        await atomx_delivery.deliver(
            bridge, A, private_record, "event", "private-link", "connection"
        )
    bridge.test_slack.chat_postMessage.assert_not_awaited()


async def test_account_connection_completes_instead_of_leaving_on_it(bridge):
    bridge.app.signed_link_secret = KEY
    bridge.settings.transfers_enabled = True
    bridge.test_slack.conversations_open.side_effect = refusal()
    event_id = await bridge.accept(payload(text="connect atomx"), principal=A)
    connection = atomx_connections.Connections(bridge.app, bridge.settings, bridge.repository)
    old = await connection.issue(A, "connection", event_id)
    key = "atomx-link:" + old.split("#")[1].split(".")[0]
    expired = await bridge.repository.get_connector_state(key)
    expired["expires"] = 0
    await bridge.repository.set_connector_state(key, expired)
    assert await bridge.process(event_id, 0) == "complete"
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    assert record["notice_phase"] == "complete"
    assert "private link" in record["notice_text"]
    bridge.test_slack.conversations_open.assert_not_awaited()
    links = [
        c.kwargs
        for c in bridge.test_slack.chat_postMessage.call_args_list
        if "/integrations/atomx#" in c.kwargs.get("text", "")
    ]
    assert len(links) == 1 and links[0]["channel"] == "D1"
    assert links[0]["thread_ts"] == "1.0"
    assert old not in links[0]["text"]
    with pytest.raises(ValueError, match="expired"):
        await connection.exchange(old.split("#")[1])
    assert "/integrations/atomx#" not in str(record)
    assert await bridge.process(event_id, 0) == "complete"
    assert (
        len(
            [
                c
                for c in bridge.test_slack.chat_postMessage.call_args_list
                if "/integrations/atomx#" in c.kwargs.get("text", "")
            ]
        )
        == 1
    )
    bridge.test_client.create_job.assert_not_called()


async def test_expired_unused_link_rotates_without_reviving_old_bearer(connections, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(atomx_connections.time, "time", lambda: clock[0])
    old = await connections.issue(A, "connection", "delayed-request")
    assert await connections.issue(A, "connection", "delayed-request") == old
    clock[0] += atomx_connections.LIFETIME + 1
    fresh = await connections.issue(A, "connection", "delayed-request")
    assert fresh != old
    assert await connections.issue(A, "connection", "delayed-request") == fresh
    with pytest.raises(ValueError, match="expired"):
        await connections.exchange(old.split("#")[1])
    session = await connections.exchange(fresh.split("#")[1])
    assert (await connections.session(session))["owner"] == A
    clock[0] += atomx_connections.LIFETIME + 1
    assert await connections.issue(A, "connection", "delayed-request") == fresh
    with pytest.raises(ValueError, match="expired"):
        await connections.exchange(fresh.split("#")[1])


async def test_multiple_expired_deliveries_always_get_fresh_bound_links(connections, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(atomx_connections.time, "time", lambda: clock[0])
    links = []
    for _ in range(3):
        links.append(await connections.issue(A, "approval", "delayed", "request"))
        clock[0] += atomx_connections.LIFETIME + 1
    assert len(set(links)) == 3
    for link in links:
        with pytest.raises(ValueError):
            await connections.exchange(link.split("#")[1])
    fresh = await connections.issue(A, "approval", "delayed", "request")
    session = await connections.exchange(fresh.split("#")[1])
    assert (await connections.session(session))["pending"] == "request"
