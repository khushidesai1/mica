import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_write_flow import allow, writable  # noqa: F401

from pistachio.agents.analysis import direct, write_access, write_approval


@pytest.fixture
def replacing(writable, monkeypatch):  # noqa: F811
    writable.inspect = Mock(return_value={"nested/result.csv": 7})
    monkeypatch.setattr(write_approval, "inspect", writable.inspect)
    monkeypatch.setattr(write_approval, "AsyncWebClient", lambda **kw: writable.test_slack)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(
            return_value=direct.Decision(
                route="compute",
                reply="Prepare the revised results.",
                write_paths=["gs://lab-data/liver/output/nested/"],
            )
        ),
    )
    return writable


async def start(agent):
    await allow(agent)
    event_id = await agent.accept(payload("3.0", text="Replace the nested result"))
    assert await agent.process(event_id, 0) == "awaiting_approval"
    record = await agent.repository.get_connector_state("claude-event:" + event_id)
    return event_id, record


def click(event_id, record, action="approve"):
    return dict(
        type="block_actions",
        team={"id": "T1"},
        user={"id": "U1"},
        channel={"id": "D1"},
        message={"ts": record["notice_ts"]},
        actions=[{"action_id": write_approval.ACTION_PREFIX + action, "value": event_id}],
    )


async def test_exact_requester_approval_starts_once_and_pins_manifest(replacing):
    import json

    event_id, record = await start(replacing)
    replacing.test_client.create_job.assert_not_called()
    assert "slot" not in record
    message = replacing.test_slack.chat_update.call_args.kwargs
    assert "nested/" in message["text"] and "1 existing file" in message["text"]
    assert len(message["blocks"][-1]["elements"]) == 2
    action = click(event_id, record)
    await write_approval.handle(replacing, replacing.store, action)
    assert await replacing.process(event_id, record["tick"]) == "running"
    manifest = json.loads(
        replacing.store.get(f"tasks/slack-analysis/events/{event_id}/request.json")[0]
    )
    assert manifest["overwrite_paths"] == ["gs://lab-data/liver/output/nested/"]
    await write_approval.handle(replacing, replacing.store, action)
    assert replacing.test_client.create_job.call_count == 1
    grant = write_access.active(replacing.store, replacing.pid, replacing.settings)
    assert (
        write_approval.expected_generation(
            replacing.store, event_id, replacing.pid, grant, "nested/result.csv"
        )
        == 7
    )
    assert (
        write_approval.expected_generation(
            replacing.store, event_id, replacing.pid, grant, "sibling.csv"
        )
        == 0
    )


@pytest.mark.parametrize("field", ["team", "user", "channel", "message"])
async def test_wrong_actor_or_message_cannot_confirm(replacing, field):
    event_id, record = await start(replacing)
    action = click(event_id, record)
    action[field] = {"ts" if field == "message" else "id": "other"}
    with pytest.raises(ValueError, match="Only the requester"):
        await write_approval.handle(replacing, replacing.store, action)
    assert write_approval.load(replacing.store, event_id)[0].status == "pending"
    replacing.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("reason", ["cancel", "expire", "revoke", "access"])
async def test_cancel_expiry_revocation_or_membership_change_never_starts(
    replacing, monkeypatch, reason
):
    event_id, record = await start(replacing)
    if reason == "cancel":
        await write_approval.handle(replacing, replacing.store, click(event_id, record, "cancel"))
    elif reason == "expire":
        deadline = write_approval.load(replacing.store, event_id)[0].expires_at
        monkeypatch.setattr(write_approval.time, "time", lambda: deadline + 1)
    elif reason == "revoke":
        state = write_access.load(replacing.store, replacing.pid)
        state.grant = None
        replacing.store.put(write_access.key(replacing.pid), state.model_dump_json().encode())
    else:
        replacing.app.slack_user_map["T1:U1"] = "other@example.com"
    assert await replacing.process(event_id, record["tick"]) == "failed"
    replacing.test_client.create_job.assert_not_called()


async def test_no_existing_outputs_starts_without_confirmation(replacing):
    await allow(replacing)
    replacing.inspect.return_value = {}
    event_id = await replacing.accept(payload("3.0"))
    assert await replacing.process(event_id, 0) == "running"
    assert not write_approval.load(replacing.store, event_id)


@pytest.mark.parametrize(
    "path",
    [
        "gs://lab-data/liver/output/",
        "gs://lab-data/liver/output2/file.csv",
        "gs://other/file.csv",
        "gs://lab-data/liver/output/../private.csv",
        "gs://lab-data/liver/output/file?query",
        "gs://lab-data/liver/output/file#fragment",
    ],
)
async def test_invalid_targets_rejected_before_listing(replacing, monkeypatch, path):
    await allow(replacing)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(
            return_value=direct.Decision(
                route="compute",
                reply="Compute the revision.",
                write_paths=[path],
            )
        ),
    )
    event_id = await replacing.accept(payload("3.0"))
    assert await replacing.process(event_id, 0) == "failed"
    replacing.inspect.assert_not_called()
    replacing.test_client.create_job.assert_not_called()


def test_recursive_snapshot_follows_pages_and_bounds_count(monkeypatch):
    grant = write_access.WriteGrant(
        id="a" * 32,
        uri="gs://lab-data/output/",
        actor="alice",
        admin_id="T:U",
        created_at=write_access.now(),
    )
    calls = []

    def blobs(bucket, **kwargs):
        calls.append(kwargs)
        number = 1 if kwargs["page_token"] is None else 2
        return SimpleNamespace(
            pages=iter([[SimpleNamespace(name=f"output/store/chunk{number}", generation=number)]]),
            next_page_token="next" if number == 1 else None,
        )

    monkeypatch.setattr("google.cloud.storage.Client", lambda: SimpleNamespace(list_blobs=blobs))
    result = write_approval.inspect(["gs://lab-data/output/store/"], grant, 2)
    assert result == {"store/chunk1": 1, "store/chunk2": 2}
    assert calls[1]["page_token"] == "next" and calls[1]["timeout"] <= calls[0]["timeout"]
    with pytest.raises(ValueError, match="at most"):
        write_approval.inspect(["gs://lab-data/output/store/"], grant, 1)
    clock = iter([time.monotonic(), time.monotonic() + 21])
    monkeypatch.setattr(write_approval.time, "monotonic", lambda: next(clock))
    with pytest.raises(ValueError, match="timed out"):
        write_approval.inspect(["gs://lab-data/output/store/"], grant, 2)
