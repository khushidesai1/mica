"""Project role changes preserve ownership and use the existing approval boundary."""

# ruff: noqa: F811
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_email_project_access import intake
from test_gmail_intake import SENDER, mail, signing  # noqa: F401
from test_natural_language_commands import action, chat  # noqa: F401

from mica.agents.analysis import access, command_approval, command_intent, direct
from mica.agents.analysis.registry import Registry, RegistryDoc

A, B, C = "alice@example.com", "bob@example.com", "carol@example.com"


async def test_promotion_preserves_worker_authorization_but_refreshes_private_roles(chat):
    eid = await chat.accept(payload(text="Analyze the data"))
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    before = access.resolve(chat.app, chat.settings, chat.store, record)
    registry = Registry(chat.store)
    registry.update("promote", A, "add-owner", lambda d: d.set_co_owner(chat.pid, A, B))
    after = access.resolve(chat.app, chat.settings, chat.store, record)
    assert after.signature == before.signature
    assert command_intent.visibility(after) != command_intent.visibility(before)


def test_coowner_permissions_demotion_and_creator_protection():
    doc = RegistryDoc()
    pid = doc.create(A, "liver", "gs://data/liver/")
    doc.set_co_owner(pid, A, B.upper())
    assert doc.owns(pid, A) and doc.owns(pid, B)
    assert B in doc.projects[pid].members and doc.defaults[B] == pid
    revision = doc.projects[pid].access_revision
    doc.set_co_owner(pid, A, B)
    assert doc.projects[pid].access_revision == revision
    doc.share(pid, B, C)
    doc.rename(pid, B, "new-liver")
    doc.set_data_uri(pid, B, "gs://data/processed/")
    doc.publish(pid, B, "T1", "C1")
    doc.bind("T1", "C1", B, pid, private=False)
    for target in (A, B):
        with pytest.raises(ValueError):
            doc.share(pid, A, target, remove=True)
    with pytest.raises(ValueError, match="original"):
        doc.set_co_owner(pid, B, A, remove=True)
    with pytest.raises(ValueError, match="another owner"):
        doc.set_co_owner(pid, B, B, remove=True)
    with pytest.raises(ValueError, match="Only"):
        doc.set_co_owner(pid, C, "outsider@example.com")
    doc.set_co_owner(pid, A, B, remove=True)
    assert not doc.owns(pid, B) and doc.can_use(pid, B)
    with pytest.raises(ValueError, match="owner"):
        doc.set_data_uri(pid, B, "gs://data/elsewhere/")
    doc.share(pid, A, B, remove=True)
    assert not doc.can_use(pid, B)


def test_coowner_rename_preserves_creator_namespace_and_ambiguous_names():
    doc = RegistryDoc()
    first = doc.create(A, "liver", "gs://data/a/")
    doc.create(A, "brain", "gs://data/brain/")
    second = doc.create(C, "liver", "gs://data/c/")
    for pid in (first, second):
        doc.set_co_owner(pid, doc.projects[pid].owner, B)
    with pytest.raises(ValueError, match="already have"):
        doc.rename(first, B, "brain")
    with pytest.raises(ValueError, match="Choose a project ID"):
        doc.find("liver", B)
    own = doc.create(B, "liver", "gs://data/b/")
    assert doc.find("liver", B) == own


@pytest.mark.parametrize("natural", [False, True])
async def test_owner_change_requires_confirmation_and_replay_cannot_repromote(chat, natural):
    text = "add-owner liver <@U2>"
    if natural:
        text = "Make <@U2> an owner of this project"
        chat.intent.return_value = command_intent.Intent(
            route="command",
            proposal=command_intent.Proposal(
                operation="add-owner", selector="this project", target="<@U2>"
            ),
        )
    eid = await chat.accept(payload(text=text))
    assert await chat.process(eid, 0) == "complete"
    registry = Registry(chat.store)
    assert not registry.load().owns(chat.pid, B)
    await command_approval.handle(chat, chat.store, action(eid))
    assert registry.load().owns(chat.pid, B)
    registry.update(
        "demote", A, "remove-owner", lambda d: d.set_co_owner(chat.pid, A, B, remove=True)
    )
    await command_approval.handle(chat, chat.store, action(eid))
    assert not registry.load().owns(chat.pid, B)
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("scope", ["", "owner"])
async def test_ownership_misclassified_as_share_never_silently_shares(chat, scope):
    chat.intent.return_value = command_intent.Intent(
        route="command",
        proposal=command_intent.Proposal(
            operation="share", selector="this project", target="<@U2>", scope=scope
        ),
    )
    eid = await chat.accept(payload(text="Can you make <@U2> an owner of the project?"))
    assert await chat.process(eid, 0) == "complete"
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert "co-owner" in record["notice_text"]
    assert not Registry(chat.store).load().can_use(chat.pid, B)
    assert not chat.store.keys("projects/_command_approvals/")


async def test_member_cannot_promote_and_role_listing_is_private(chat):
    registry = Registry(chat.store)
    registry.update("share", A, "share", lambda d: d.share(chat.pid, A, B))
    eid = await chat.accept(payload(text=f"add-owner liver {C}", user="U2", channel="D2"))
    assert await chat.process(eid, 0) == "failed"
    assert not registry.load().owns(chat.pid, C)
    eid = await chat.accept(payload("2.0", text="owners liver"))
    assert await chat.process(eid, 0) == "complete"
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert A in record["notice_text"] and record["reply_scope"] == "account"
    eid = await chat.accept(
        payload("3.0", text="owners liver", channel="C1", channel_type="channel")
    )
    assert await chat.process(eid, 0) == "failed"


async def test_email_lists_owners_but_cannot_change_roles(mail):
    registry = Registry(mail.store)
    registry.update("coowner", SENDER, "add-owner", lambda d: d.set_co_owner(mail.pid, SENDER, B))
    eid, record = await intake(mail, "owners", B, "owners liver")
    assert await direct.handle(mail, eid, record, mail.store)
    assert B in record["notice_text"] and SENDER in record["notice_text"]
    assert record["reply_scope"] == "account"
    assert access.resolve(mail.app, mail.settings, mail.store, record).allowed
    eid, record = await intake(mail, "owner-change", B, f"add-owner liver {C}")
    assert await direct.handle(mail, eid, record, mail.store)
    assert "Slack DM" in record["notice_text"]
    assert not registry.load().owns(mail.pid, C)
