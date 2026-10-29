# ruff: noqa: F811
"""Account discovery and audience boundaries through the actual email delivery path."""

import json
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_email_transport import sent_messages
from test_gmail_intake import SENDER, email_record, mail, signing  # noqa: F401

from mica.agents.analysis import access, command_intent, direct
from mica.agents.analysis.email_delivery import deliver
from mica.agents.analysis.registry import Binding, Registry
from mica.agents.analysis.surface import EMAIL_STATE_PREFIX

QUESTION = "Can you list the projects that I am collaborating on?\n\nBest,\nAlice\nSent with Spark"


def project(mail, name, *, owner=SENDER):
    return Registry(mail.store).update(
        f"create:{name}",
        owner,
        "create fixture",
        lambda doc: doc.create(owner, name, f"gs://lab-data/{name}/"),
    )


def remove_access(mail, pid):
    def revoke(doc):
        doc.projects[pid].owner = "bob@example.com"
        doc.projects[pid].members.discard(SENDER)
        doc.projects[pid].access_revision += 1
        return pid

    Registry(mail.store).update(f"revoke:{pid}", SENDER, "revoke fixture", revoke)


def bind_conversation(mail, record, pid):
    mail.store.put(
        access.checkpoint_key(record["thread_key"]),
        json.dumps({"private_owner": SENDER, "context": {"project_slug": pid}}).encode(),
    )


def projects_intent(monkeypatch):
    classifier = AsyncMock(
        return_value=command_intent.Intent(
            route="command", proposal=command_intent.Proposal(operation="projects")
        )
    )
    monkeypatch.setattr(command_intent, "classify", classifier)
    return classifier


async def test_direct_markdown_reply_preserves_email_text(mail):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = "Explain the QC workflow"
    text = "**Slide QC**\n- Compare counts & genes.\n[source](https://example.org/qc)"
    direct.decide.return_value = direct.Decision(route="reply", reply=text)
    assert await direct.handle(mail, eid, record, mail.store)
    assert sent_messages(mail)[0].get_content().splitlines() == text.splitlines()
    assert record["terminal_reply"] == text
    mail.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("selection", ["none", "revoked", "conflicting_legacy"])
async def test_natural_project_question_without_valid_selection_or_memory(
    mail, monkeypatch, selection
):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = QUESTION
    available = project(mail, "available-collaboration")
    registry = Registry(mail.store)

    def configure(doc):
        doc.defaults.clear()
        doc.bindings.clear()
        if selection == "revoked":
            doc.defaults[SENDER] = mail.pid
            doc.projects[mail.pid].owner = "bob@example.com"
            doc.projects[mail.pid].members.discard(SENDER)
        elif selection == "conflicting_legacy":
            doc.bindings["T1:D1"] = Binding(project_id=mail.pid, bound_by=SENDER)
            doc.bindings["T2:D2"] = Binding(project_id=available, bound_by=SENDER)
        return available

    registry.update(f"selection:{selection}", SENDER, "fixture selection", configure)
    assert not access.resolve(mail.app, mail.settings, mail.store, record).allowed
    classifier = projects_intent(monkeypatch)
    memory_read = AsyncMock(side_effect=AssertionError("Account discovery read project memory"))
    monkeypatch.setattr(mail.memory_backend, "read", memory_read)
    assert await direct.handle(mail, eid, record, mail.store)
    classifier.assert_awaited_once()
    memory_read.assert_not_awaited()
    direct.decide.assert_not_awaited()
    mail.test_client.create_job.assert_not_called()
    messages = sent_messages(mail)
    assert len(messages) == 1
    assert str(messages[0]["To"]) == SENDER
    assert "available-collaboration" in messages[0].get_content()
    assert "Your projects:" in messages[0].get_content()
    assert record["payload"]["event"]["text"] == QUESTION
    assert record["status"] == "complete"
    if selection == "revoked":
        assert "liver" not in messages[0].get_content()


async def test_private_account_list_is_not_sent_to_current_project_collaborator(mail, monkeypatch):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = QUESTION
    record["payload"]["_email"]["recipients"] = [SENDER, "bob@example.com"]
    Registry(mail.store).update(
        "share-current",
        SENDER,
        "share",
        lambda doc: doc.share(mail.pid, SENDER, "bob@example.com"),
    )
    secret = project(mail, "private-account-project")
    assert not Registry(mail.store).load().can_use(secret, "bob@example.com")
    projects_intent(monkeypatch)
    assert await direct.handle(mail, eid, record, mail.store)
    final = sent_messages(mail)[0]
    assert "private-account-project" in final.get_content()
    assert {a.addr_spec for a in final["To"].addresses} == {SENDER}
    receipt = json.loads(mail.store.get(f"{EMAIL_STATE_PREFIX}/outbox/{eid}/final.json")[0])
    assert receipt["scope"] == "account" and receipt["audience"] == "sender_only"
    # Repeated final processing must use the recorded delivery, even if input changes.
    await deliver(mail, eid, record, "Different retry result", kind="final")
    assert len(sent_messages(mail)) == 1


async def test_typed_project_list_has_same_sender_only_audience(mail):
    eid, record = await email_record(mail)
    record["payload"]["_email"]["recipients"] = [SENDER, "bob@example.com"]
    Registry(mail.store).update(
        "share-current",
        SENDER,
        "share",
        lambda doc: doc.share(mail.pid, SENDER, "bob@example.com"),
    )
    project(mail, "private-account-project")
    assert await direct.handle(mail, eid, record, mail.store)
    final = sent_messages(mail)[0]
    assert str(final["To"]) == SENDER
    assert "private-account-project" in final.get_content()


async def test_pending_account_list_regenerates_after_access_revocation(mail):
    eid, record = await email_record(mail)
    project(mail, "still-visible")
    record["payload"]["event"]["text"] = QUESTION
    record["command_intent"] = {
        "route": "command",
        "proposal": {"operation": "projects"},
    }
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    command_intent.private_reply(record, grant)
    bind_conversation(mail, record, mail.pid)
    remove_access(mail, mail.pid)
    await deliver(mail, eid, record, "STALE PRIVATE liver LIST", kind="final")
    final = sent_messages(mail)[0]
    assert "STALE PRIVATE" not in final.get_content()
    assert "liver" not in final.get_content()
    assert "still-visible" in final.get_content()
    assert str(final["To"]) == SENDER


async def test_visibility_change_during_email_preparation_sends_nothing_until_retry(
    mail, monkeypatch
):
    eid, record = await email_record(mail)
    removed = project(mail, "formerly-visible")
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    command_intent.private_reply(record, grant)
    original_resolve = access.resolve
    calls = 0

    def changing_visibility(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            remove_access(mail, removed)
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(access, "resolve", changing_visibility)
    with pytest.raises(RuntimeError, match="membership changed"):
        await deliver(mail, eid, record, "formerly-visible", kind="final")
    mail.gmail.users().messages().send.assert_not_called()
    with pytest.raises(FileNotFoundError):
        mail.store.get(f"{EMAIL_STATE_PREFIX}/outbox/{eid}/final.json")
    await deliver(mail, eid, record, "formerly-visible", kind="final")
    final = sent_messages(mail)[0]
    assert "formerly-visible" not in final.get_content()
    assert "liver" in final.get_content()
    assert str(final["To"]) == SENDER


async def test_sender_only_project_result_still_checks_project_revocation(mail):
    eid, record = await email_record(mail)
    bind_conversation(mail, record, mail.pid)
    record["reply_audience"] = "sender_only"
    record["payload"]["_email"]["recipients"] = ["bob@example.com"]
    remove_access(mail, mail.pid)
    await deliver(mail, eid, record, "PRIVATE PROJECT MEMORY CONTENT", kind="final")
    final = sent_messages(mail)[0]
    assert "PRIVATE PROJECT MEMORY CONTENT" not in final.get_content()
    assert "access is unavailable" in final.get_content()
    assert str(final["To"]) == SENDER


async def test_natural_memory_reply_retains_project_scope(mail, monkeypatch):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = "Please show the current project brief."
    monkeypatch.setattr(
        command_intent,
        "classify",
        AsyncMock(
            return_value=command_intent.Intent(
                route="command", proposal=command_intent.Proposal(operation="brief")
            )
        ),
    )
    assert await direct.handle(mail, eid, record, mail.store)
    receipt = json.loads(mail.store.get(f"{EMAIL_STATE_PREFIX}/outbox/{eid}/final.json")[0])
    assert receipt["scope"] == "project"
    assert "Species: TODO" in sent_messages(mail)[0].get_content()


async def test_private_account_reply_requires_same_verified_sender(mail):
    eid, record = await email_record(mail)
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    command_intent.private_reply(record, grant)
    record["intake_principal"] = "mallory@example.com"
    with pytest.raises(PermissionError, match="verification"):
        await deliver(mail, eid, record, "PRIVATE ACCOUNT CONTENT", kind="final")
    mail.gmail.users().messages().send.assert_not_called()


async def test_email_compute_followup_uses_original_context_session_key(mail, monkeypatch):
    from mica.agents.analysis import surface

    eid, first = await email_record(mail)
    first["payload"]["event"]["text"] = "What can we analyze?"
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(return_value=direct.Decision(route="reply", reply="Let's review the data.")),
    )
    assert await direct.handle(mail, eid, first, mail.store)
    original_thread = first["payload"]["event"]["ts"]
    followup = {
        "payload": deepcopy(first["payload"]),
        "surface": "email",
        "intake_principal": SENDER,
        "status": "pending",
    }
    followup["payload"]["_email"]["id"] = "a2"
    event = followup["payload"]["event"]
    event.update(ts="2000.0", text="Make a plot of the data")
    event.pop("thread_ts", None)
    second_id, followup["thread_key"] = surface.email_keys(followup["payload"], SENDER)
    monkeypatch.setattr(
        direct,
        "decide",
        AsyncMock(return_value=direct.Decision(route="compute", reply="I'll make that plot.")),
    )
    assert not await direct.handle(mail, second_id, followup, mail.store)
    assert event["thread_ts"] == original_thread
    state = json.loads(mail.store.get(access.checkpoint_key(followup["thread_key"]))[0])
    assert event["thread_ts"] == state["context"]["thread_ts"]


@pytest.mark.parametrize("operation", ["share", "rename", "publish", "write grant"])
async def test_email_admin_handoff_precedes_private_selector_resolution(
    mail, monkeypatch, operation
):
    eid, record = await email_record(mail)
    record["payload"]["event"]["text"] = "Please change that project setting"
    record["payload"]["_email"]["recipients"] = [SENDER, "bob@example.com"]
    Registry(mail.store).update(
        "share-current-for-admin",
        SENDER,
        "share",
        lambda doc: doc.share(mail.pid, SENDER, "bob@example.com"),
    )
    private_pid = project(mail, "private-account-project")
    monkeypatch.setattr(
        command_intent,
        "classify",
        AsyncMock(
            return_value=command_intent.Intent(
                route="command",
                proposal=command_intent.Proposal(
                    operation=operation, selector="private-account-project"
                ),
            )
        ),
    )
    assert await direct.handle(mail, eid, record, mail.store)
    final = sent_messages(mail)[0]
    assert str(final["To"]) == SENDER
    assert "Slack DM" in final.get_content()
    assert private_pid not in final.get_content()
    assert "private-account-project" not in final.get_content()
