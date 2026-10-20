"""Real data-root mutations, approval boundaries, and existing-thread refresh."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_email_transport import sent_messages
from test_gmail_intake import SENDER, email_record, mail, signing  # noqa: F401
from test_natural_language_commands import action

from mica.agents.analysis import access, command_approval, command_intent, commands, direct
from mica.agents.analysis.registry import Registry

URI = "gs://lab-data/liver/processed"


async def propose(bridge, monkeypatch, *, natural=True, uri=URI, user="U1", shared=False):
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    monkeypatch.setattr(
        command_intent,
        "classify",
        AsyncMock(
            return_value=command_intent.Intent(
                route="command",
                proposal=command_intent.Proposal(
                    operation="data-root", selector="liver", scope=uri
                ),
            )
        ),
    )
    text = (
        f"Set liver's default mounted data folder to {uri}."
        if natural
        else f"data-root liver {uri}"
    )
    eid = await bridge.accept(
        payload(
            text=text,
            user=user,
            channel="C1" if shared else "D1",
            channel_type="channel" if shared else "im",
        )
    )
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    return eid, record


@pytest.mark.parametrize("natural", [True, False])
async def test_owner_confirms_actual_registry_change_and_replay(bridge, monkeypatch, natural):
    eid, record = await propose(bridge, monkeypatch, natural=natural)
    before = Registry(bridge.store).load()
    assert await direct.handle(bridge, eid, record, bridge.store)
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    assert Registry(bridge.store).load() == before
    assert "Current location: gs://lab-data/liver/" in record["notice_text"]
    assert "every project member" in record["notice_text"]
    await command_approval.handle(bridge, bridge.store, action(eid))
    after = Registry(bridge.store).load()
    expected = before.projects[bridge.pid].model_copy(update={"data_uri": URI})
    assert after.projects[bridge.pid] == expected
    assert after.defaults == before.defaults and after.bindings == before.bindings
    assert eid in after.applied_events
    await command_approval.handle(bridge, bridge.store, action(eid))
    assert Registry(bridge.store).load() == after
    bridge.test_client.create_job.assert_not_called()
    direct.decide.assert_not_awaited()


async def test_member_cannot_change_project_data_root(bridge, monkeypatch):
    Registry(bridge.store).update(
        "share-fixture",
        "alice@example.com",
        "share",
        lambda d: d.share(bridge.pid, "alice@example.com", "bob@example.com"),
    )
    eid, record = await propose(bridge, monkeypatch, user="U2")
    before = Registry(bridge.store).load()
    with pytest.raises(ValueError, match="owner"):
        await direct.handle(bridge, eid, record, bridge.store)
    assert Registry(bridge.store).load() == before


@pytest.mark.parametrize("natural", [True, False])
async def test_shared_channel_never_discloses_or_mutates(bridge, monkeypatch, natural):
    eid, record = await propose(bridge, monkeypatch, shared=True, natural=natural)
    before = Registry(bridge.store).load()
    if natural:
        assert await direct.handle(bridge, eid, record, bridge.store)
        assert "private" in record["notice_text"]
        assert URI not in record["notice_text"]
    else:
        with pytest.raises(ValueError, match="private"):
            await direct.handle(bridge, eid, record, bridge.store)
    assert Registry(bridge.store).load() == before


async def test_unadmitted_bucket_rejected_before_confirmation(bridge, monkeypatch):
    eid, record = await propose(bridge, monkeypatch, uri="gs://unapproved/liver")
    before = Registry(bridge.store).load()
    with pytest.raises(ValueError, match="not available"):
        await direct.handle(bridge, eid, record, bridge.store)
    assert Registry(bridge.store).load() == before
    with pytest.raises(FileNotFoundError):
        bridge.store.get(command_approval.key(eid))


async def test_invented_uri_clarifies_instead_of_editing_brief(bridge, monkeypatch):
    eid, record = await propose(bridge, monkeypatch)
    record["payload"]["event"]["text"] = "Change liver's default folder."
    before = Registry(bridge.store).load()
    assert await direct.handle(bridge, eid, record, bridge.store)
    assert "exact gs://" in record["notice_text"]
    assert Registry(bridge.store).load() == before
    direct.decide.assert_not_awaited()


async def test_changed_settings_and_removed_bucket_invalidate_confirmation(bridge, monkeypatch):
    eid, record = await propose(bridge, monkeypatch)
    assert await direct.handle(bridge, eid, record, bridge.store)
    await bridge.repository.set_connector_state("claude-event:" + eid, record)
    bridge.settings.data_buckets = "another-bucket"
    with pytest.raises(ValueError, match="not available"):
        await command_approval.handle(bridge, bridge.store, action(eid))
    assert Registry(bridge.store).load().projects[bridge.pid].data_uri != URI
    bridge.settings.data_buckets = "lab-data"
    Registry(bridge.store).update(
        "changed",
        "alice@example.com",
        "other-path",
        lambda d: d.set_data_uri(bridge.pid, "alice@example.com", "gs://lab-data/other"),
    )
    with pytest.raises(ValueError, match="settings changed"):
        await command_approval.handle(bridge, bridge.store, action(eid))


@pytest.mark.parametrize("natural", [True, False])
async def test_email_handoff_is_sender_only_without_mutation(mail, monkeypatch, natural):
    eid, record = await email_record(mail)
    text = f"Set liver's default data folder to {URI}." if natural else f"data-root liver {URI}"
    record["payload"]["event"]["text"] = text
    record["payload"]["_email"]["recipients"] = [SENDER, "bob@example.com"]
    monkeypatch.setattr(
        command_intent,
        "classify",
        AsyncMock(
            return_value=command_intent.Intent(
                route="command",
                proposal=command_intent.Proposal(
                    operation="data-root", selector="liver", scope=URI
                ),
            )
        ),
    )
    before = Registry(mail.store).load()
    assert await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    assert "Slack" in record["notice_text"]
    assert len(sent_messages(mail)) == 1
    assert str(sent_messages(mail)[0]["To"]) == SENDER
    direct.decide.assert_not_awaited()


async def test_existing_conversation_uses_new_root_and_preserves_history(bridge):
    eid = await bridge.accept(payload(text="What is the data location?"))
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    old = {
        "context": {
            "project_slug": bridge.pid,
            "data_path": "/mnt/gcs/lab-data/liver",
            "workdir": "/workspace/saved",
            "session_id": "old-sdk-session",
            "thread_ts": "1.0",
            "project_md": "old guide",
        },
        "messages": [{"id": "older", "user": "earlier", "assistant": "result"}],
        "turn": 2,
    }
    bridge.store.put(access.checkpoint_key(record["thread_key"]), json.dumps(old).encode())
    grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    Registry(bridge.store).update(
        "move",
        "alice@example.com",
        "move",
        lambda d: d.set_data_uri(bridge.pid, "alice@example.com", URI),
    )
    assert (
        access.resolve(bridge.app, bridge.settings, bridge.store, record).signature
        != grant.signature
    )
    direct.decide.return_value = direct.Decision(route="reply", reply="Current data folder.")
    assert await direct.handle(bridge, eid, record, bridge.store)
    context = direct.decide.call_args.args[2]
    assert context["data_uri"] == URI
    assert context["data_path"].endswith("/lab-data/liver/processed")
    saved = json.loads(bridge.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert saved["context"]["session_id"] is None
    assert saved["context"]["workdir"] == old["context"]["workdir"]
    assert any(m["id"] == "older" for m in saved["messages"])


@pytest.mark.parametrize("uri", ["gs://lab-data/../private", "gs://lab-data/path?secret=x"])
def test_invalid_data_root_rejected(uri):
    with pytest.raises(ValueError):
        commands.parse(f"data-root liver {uri}")
