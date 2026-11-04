from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_write_flow import allow, writable  # noqa: F401

from mica.agents.analysis import conversation, write_access, write_approval


@pytest.fixture
def replacing(writable, monkeypatch):  # noqa: F811
    writable.inspect = Mock(return_value=["gs://lab-data/liver/output/nested/"])
    monkeypatch.setattr(write_approval, "inspect", writable.inspect)
    monkeypatch.setattr(write_approval, "AsyncWebClient", lambda **kw: writable.test_slack)
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
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
    assert "nested/" in message["text"] and "folder already has files" in message["text"]
    assert len(message["blocks"][-1]["elements"]) == 2
    buttons = message["blocks"][-1]["elements"]
    assert buttons[0]["style"] == "primary" and "style" not in buttons[1]
    action = click(event_id, record)
    await write_approval.handle(replacing, replacing.store, action)
    assert await replacing.process(event_id, record["tick"]) == "running"
    manifest = json.loads(
        replacing.store.get(f"tasks/slack-analysis/events/{event_id}/request.json")[0]
    )
    assert manifest["overwrite_paths"] == ["gs://lab-data/liver/output/nested/"]
    await write_approval.handle(replacing, replacing.store, action)
    assert replacing.test_client.create_job.call_count == 1
    approval = write_approval.load(replacing.store, event_id)[0]
    assert approval.paths == ["gs://lab-data/liver/output/nested/"]
    assert approval.inspected_at <= approval.approved_at
    assert "generations" not in approval.model_dump()


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
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
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


@pytest.mark.parametrize("count", [1, 25000])
def test_folder_approval_probes_only_one_object(monkeypatch, count):
    calls = []

    def blobs(bucket, **kwargs):
        calls.append(kwargs)
        assert kwargs["max_results"] == 1 and kwargs["page_size"] == 1
        yield SimpleNamespace(name="output/store/chunk0")
        raise AssertionError(f"Must not enumerate the remaining {count - 1} objects")

    monkeypatch.setattr("google.cloud.storage.Client", lambda: SimpleNamespace(list_blobs=blobs))
    assert write_approval.inspect(["gs://lab-data/output/store/"]) == [
        "gs://lab-data/output/store/"
    ]
    assert len(calls) == 1


async def test_only_nonempty_requested_folders_are_in_constant_size_approval(
    replacing, monkeypatch
):
    await allow(replacing)
    paths = [f"gs://lab-data/liver/output/tma{n}/" for n in [17, 18, 19]]
    monkeypatch.setattr(
        conversation,
        "run",
        AsyncMock(
            return_value=conversation.Decision(
                route="compute",
                reply="Regenerating.",
                write_paths=paths,
            )
        ),
    )
    replacing.inspect.return_value = paths[:2]
    event = await replacing.accept(payload("3.0", text="Regenerate the TMAs"))
    assert await replacing.process(event, 0) == "awaiting_approval"
    approval = write_approval.load(replacing.store, event)[0]
    assert approval.paths == paths[:2]
    assert len(approval.model_dump_json()) < 1000
    text, blocks = write_approval.blocks(approval, replacing.settings.write_allowed_prefixes)
    assert paths[0] in text and paths[1] in text and paths[2] not in text
    assert len(blocks[-1]["elements"]) == 2


def test_probe_handles_empty_folders_and_exact_files(monkeypatch):
    from google.api_core.exceptions import NotFound

    file = Mock()
    file.reload.side_effect = [None, NotFound("missing")]
    client = Mock()
    client.bucket.return_value.blob.return_value = file
    client.list_blobs.return_value = iter([])
    monkeypatch.setattr("google.cloud.storage.Client", lambda: client)
    assert write_approval.inspect(
        [
            "gs://lab-data/empty/",
            "gs://lab-data/file",
            "gs://lab-data/missing",
        ]
    ) == ["gs://lab-data/file"]


def test_legacy_approval_expires_instead_of_migrating(replacing):
    import json

    event = "a" * 32
    replacing.store.put(
        write_approval.key(event), json.dumps({"generations": {"file": 7}}).encode()
    )
    with pytest.raises(ValueError, match="expired during an update"):
        write_approval.load(replacing.store, event)


@pytest.mark.parametrize("signed", [False, True])
async def test_signed_slack_action_routes_to_run_confirmation(replacing, monkeypatch, signed):
    import json
    from urllib.parse import urlencode

    from fastapi import HTTPException
    from starlette.requests import Request
    from test_slack_projects import _headers

    from mica.agents.analysis import storage
    from mica.connectors import slack

    event_id, record = await start(replacing)
    replacing.app.slack_signing_secret = "test-secret"
    raw = urlencode({"payload": json.dumps(click(event_id, record))}).encode()
    headers = _headers(raw, "test-secret" if signed else "wrong-secret")
    services = SimpleNamespace(settings=replacing.app, repository=replacing.repository)
    monkeypatch.setattr(storage, "GCSStorage", lambda bucket: replacing.store)
    monkeypatch.setattr(slack, "_slack_principal", Mock(side_effect=AssertionError("Legacy route")))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/integrations/slack/actions",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "app": SimpleNamespace(state=SimpleNamespace(services=services)),
        },
        receive=receive,
    )
    if signed:
        assert await slack.slack_actions(request, BackgroundTasks()) == {}
        assert write_approval.load(replacing.store, event_id)[0].status == "approved"
    else:
        with pytest.raises(HTTPException) as error:
            await slack.slack_actions(request, BackgroundTasks())
        assert error.value.status_code == 401
        assert write_approval.load(replacing.store, event_id)[0].status == "pending"
    replacing.test_client.create_job.assert_not_called()  # Only the scheduled poll may launch.


async def test_expired_overwrite_keeps_outputs_and_requires_new_confirmation(replacing):
    from mica.agents.analysis import approvals

    event_id, record = await start(replacing)
    row, generation = write_approval.load(replacing.store, event_id)
    row.expires_at = 1
    approvals.save(replacing.store, write_approval.key(event_id), row, generation)
    saved_path = f"tasks/slack-analysis/events/{event_id}/completed-output.csv"
    replacing.store.put(saved_path, b"saved result")
    await write_approval.KIND.expire(replacing, replacing.store, event_id)
    assert write_approval.load(replacing.store, event_id)[0].status == "expired"
    assert replacing.test_slack.chat_update.call_args.kwargs["blocks"][-1]["elements"][0][
        "action_id"
    ] == write_approval.ACTION_PREFIX + "again"
    legacy = click(event_id, record, "again")
    legacy["actions"][0]["action_id"] = "pistachio_overwrite_again"
    await write_approval.handle(replacing, replacing.store, legacy)
    renewed = write_approval.load(replacing.store, event_id)[0]
    assert renewed.status == "pending" and renewed.approved_at == 0
    assert replacing.inspect.call_count == 2  # Fresh destination inspection before the new card.
    assert replacing.store.get(saved_path)[0] == b"saved result"
    replacing.test_client.create_job.assert_not_called()
    await write_approval.handle(replacing, replacing.store, click(event_id, record, "cancel"))
    await write_approval.KIND.expire(replacing, replacing.store, event_id)
    assert write_approval.load(replacing.store, event_id)[0].status == "cancelled"
