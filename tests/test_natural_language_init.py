"""Natural project creation reuses setup admission and conversation safeguards."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_email_transport import sent_messages
from test_gmail_intake import SENDER, email_record, mail, signing  # noqa: F401

from mica.agents.analysis import access, command_intent, conversation, direct
from mica.agents.analysis.registry import Registry


def classify_creation(monkeypatch, *, name="brain", uri="gs://lab-data/brain/"):
    monkeypatch.setattr(
        direct, "inventory", lambda *args, **kwargs: {"entries": [], "content_inspected": False}
    )
    async def respond(*args, **kwargs):
        if kwargs.get("init"):
            return conversation.Decision(
                route="reply", reply="Project ready.", guide="# Brain\nUser-provided project."
            )
        return conversation.Decision(
            route="command", reply="Creating the project.",
            command_proposal=command_intent.Proposal(
                operation="init", selector=name, scope=uri
            ).model_dump(),
        )

    monkeypatch.setattr(conversation, "run", AsyncMock(side_effect=respond))



async def request(mail, text, surface="slack", *, shared=False):
    if surface == "email":
        eid, record = await email_record(mail)
        record["payload"]["event"]["text"] = text
    else:
        eid = await mail.accept(
            payload(
                text=text,
                channel="C1" if shared else "D1",
                channel_type="channel" if shared else "im",
            )
        )
        record = await mail.repository.get_connector_state("claude-event:" + eid)
    return eid, record


@pytest.mark.parametrize("surface", ["slack", "email"])
async def test_natural_creation_uses_supplied_bucket_and_preserves_request(
    mail, monkeypatch, surface
):
    text = "Please create a project named brain using gs://lab-data/brain/."
    eid, record = await request(mail, text, surface)
    classify_creation(monkeypatch)
    assert await direct.handle(mail, eid, record, mail.store)
    doc = Registry(mail.store).load()
    pid = doc.find("brain", SENDER)
    assert doc.projects[pid].data_uri == "gs://lab-data/brain/"
    assert doc.defaults[SENDER] == pid
    state = json.loads(mail.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert state["context"]["project_slug"] == pid
    assert record["payload"]["event"]["text"] == text
    assert "init_inventory" in conversation.run.call_args.args[5]
    mail.test_client.create_job.assert_not_called()
    # Replaying the completed setup must not create another project or change its identity.
    assert await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load().find("brain", SENDER) == pid
    if surface == "email":
        assert len(sent_messages(mail)) == 1
        assert str(sent_messages(mail)[0]["To"]) == SENDER


@pytest.mark.parametrize(
    ("text", "name", "uri"),
    [
        ("Please create a new project.", "", ""),
        ("Create a project named brain.", "brain", ""),
        ("Create a project using gs://lab-data/brain/.", "", "gs://lab-data/brain/"),
        # Even a valid, admitted bucket must never be invented by the classifier.
        ("Create a project named brain.", "brain", "gs://lab-data/brain/"),
    ],
)
async def test_missing_or_invented_creation_details_clarify_without_mutation(
    mail, monkeypatch, text, name, uri
):
    eid, record = await request(mail, text)
    before = Registry(mail.store).load()
    classify_creation(monkeypatch, name=name, uri=uri)
    assert await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    assert record["notice_text"] and record["status"] == "complete"
    conversation.run.assert_awaited_once()
    mail.test_client.create_job.assert_not_called()


async def test_natural_creation_rejects_unadmitted_bucket_before_registry_write(mail, monkeypatch):
    eid, record = await request(
        mail, "Create a project named brain using gs://outside-project/brain/."
    )
    before = Registry(mail.store).load()
    classify_creation(monkeypatch, uri="gs://outside-project/brain/")
    with pytest.raises(ValueError, match="not available"):
        await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    conversation.run.assert_awaited_once()
    mail.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("surface", ["slack", "email"])
async def test_natural_creation_in_bound_conversation_requires_new_conversation(
    mail, monkeypatch, surface
):
    eid, record = await request(
        mail, "Create a project named brain using gs://lab-data/brain/.", surface
    )
    mail.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps(
            {
                "private_owner": SENDER,
                "context": {
                    "project_slug": mail.pid,
                    "data_path": str(mail.settings.data_mount_root / "lab-data/liver"),
                    "project_md": "# Existing project",
                    "thread_ts": record["payload"]["event"]["ts"],
                },
            }
        ).encode(),
    )
    before = Registry(mail.store).load()
    classify_creation(monkeypatch)
    with pytest.raises(ValueError, match="[Nn]ew (thread|conversation)"):
        await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    conversation.run.assert_awaited_once()
    mail.test_client.create_job.assert_not_called()


async def test_natural_creation_with_attached_files_keeps_existing_setup_guard(mail, monkeypatch):
    eid, record = await request(
        mail, "Create a project named brain using gs://lab-data/brain/."
    )
    record["payload"]["event"]["files"] = [{"id": "F1", "name": "counts.csv"}]
    before = Registry(mail.store).load()
    classify_creation(monkeypatch)
    monkeypatch.setattr(direct.attachments, "ingest", lambda *args, **kwargs: [])
    with pytest.raises(ValueError, match="[Ss]end files"):
        await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    conversation.run.assert_awaited_once()
    mail.test_client.create_job.assert_not_called()


async def test_shared_channel_cannot_create_private_project_from_model_proposal(mail, monkeypatch):
    eid, record = await request(
        mail, "Create a project named brain using gs://lab-data/brain/.", shared=True
    )
    before = Registry(mail.store).load()
    classify_creation(monkeypatch)
    assert await direct.handle(mail, eid, record, mail.store)
    assert Registry(mail.store).load() == before
    assert "private" in record["notice_text"].lower()
    conversation.run.assert_awaited_once()
    mail.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("natural", [True, False])
async def test_reused_project_setup_keeps_private_previous_default_out_of_cc(
    mail, monkeypatch, natural
):
    def shared_target(doc):
        pid = doc.create(SENDER, "brain", "gs://lab-data/brain/")
        doc.share(pid, SENDER, "bob@example.com")
        return pid

    target = Registry(mail.store).update("target-fixture", SENDER, "fixture", shared_target)
    before = Registry(mail.store).load()
    assert before.defaults[SENDER] == mail.pid
    assert before.can_use(target, "bob@example.com")
    assert not before.can_use(mail.pid, "bob@example.com")
    text = (
        "Create a project named brain using gs://lab-data/brain/."
        if natural
        else "init brain gs://lab-data/brain/"
    )
    eid, record = await request(mail, text, "email")
    record["payload"]["_email"]["recipients"] = [SENDER, "bob@example.com"]
    classify_creation(monkeypatch)
    assert await direct.handle(mail, eid, record, mail.store)
    final = sent_messages(mail)[0]
    assert "liver" in final.get_content()  # Setup offers the private previous selection.
    assert str(final["To"]) == SENDER
    assert record.get("reply_scope") != "account"
