"""Email membership is independent of Slack admission and channel publication."""

# ruff: noqa: F811
import hashlib
from unittest.mock import AsyncMock, Mock

import pytest
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse
from test_analysis_batch import bridge, payload  # noqa: F401
from test_email_transport import sent_messages
from test_gmail_intake import SENDER, mail, signing  # noqa: F401
from test_natural_language_commands import action

from mica.agents.analysis import access, channel_access, command_approval, command_intent, direct
from mica.agents.analysis.identity import IdentityUnavailable, member_email, recipient_email
from mica.agents.analysis.registry import Registry
from mica.connectors import gmail

EXTERNAL = "collaborator@example.com"


@pytest.mark.parametrize(
    "token",
    [
        EXTERNAL,
        "COLLABORATOR@EXAMPLE.COM",
        "<mailto:collaborator@example.com|Collaborator>",
        "person+project@example.org",
    ],
)
def test_explicit_recipient_address_normalizes(token):
    assert "@" in recipient_email(token)
    assert recipient_email(token) == recipient_email(token).lower()


@pytest.mark.parametrize(
    "token",
    [
        "",
        "a@@example.com",
        "a@example.com,b@example.com",
        "Person <a@example.com>",
        "a@example.com\nBcc:x@example.com",
        "not-an-email",
        "<mailto:a@example.com|x> trailing",
        "a@exam ple.com",
        "ü@example.com",
    ],
)
def test_invalid_recipient_is_rejected(token):
    with pytest.raises(ValueError):
        recipient_email(token)


@pytest.mark.parametrize("mode", ["mapped", "workspace", "channel"])
def test_email_target_never_calls_slack_or_changes_slack_admission(bridge, monkeypatch, mode):
    bridge.settings.access_mode = mode
    monkeypatch.setattr(channel_access, "client_for", Mock(side_effect=AssertionError("Slack")))
    assert EXTERNAL not in bridge.app.slack_user_map.values()
    assert member_email(bridge.app, bridge.settings, bridge.store, "T1", EXTERNAL) == EXTERNAL
    assert (
        member_email(bridge.app, bridge.settings, bridge.store, "T1", EXTERNAL, removing=True)
        == EXTERNAL
    )
    assert EXTERNAL not in bridge.app.slack_user_map.values()


@pytest.mark.parametrize("raised", [True, False])
def test_slack_missing_recipient_is_not_an_outage(raised):
    response = SlackResponse(
        client=None,
        http_verb="GET",
        api_url="",
        req_args={},
        data={"ok": False, "error": "users_not_found"},
        headers={},
        status_code=200,
    )

    def fetch():
        if raised:
            raise SlackApiError("missing", response)
        return response

    assert channel_access.lookup(fetch) is None


async def test_exact_external_share_completes_instead_of_staying_on_it(bridge):
    text = f"share liver <mailto:{EXTERNAL}|{EXTERNAL}>"
    eid = await bridge.accept(payload(text=text))
    assert await bridge.process(eid, 0) == "complete"
    assert await bridge.process(eid, 0) == "complete"
    doc = Registry(bridge.store).load()
    assert doc.can_use(bridge.pid, EXTERNAL)
    assert doc.defaults[EXTERNAL] == bridge.pid
    assert not doc.projects[bridge.pid].published_channels
    assert doc.projects[bridge.pid].access_revision == 1
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    assert "Shared with" in record["notice_text"]
    assert "channel publication are not required" in record["notice_text"]
    assert not record.get("identity_unavailable_since")
    bridge.test_client.create_job.assert_not_called()
    revoke = await bridge.accept(payload("2.0", text=f"unshare liver {EXTERNAL}"))
    assert await bridge.process(revoke, 0) == "complete"
    assert not Registry(bridge.store).load().can_use(bridge.pid, EXTERNAL)
    # Replaying the old successful share cannot restore later-revoked membership.
    assert await bridge.process(eid, 0) == "complete"
    assert not Registry(bridge.store).load().can_use(bridge.pid, EXTERNAL)


async def test_natural_external_share_preserves_owner_confirmation(bridge, monkeypatch):
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    monkeypatch.setattr(
        command_intent,
        "classify",
        AsyncMock(
            return_value=command_intent.Intent(
                route="command",
                proposal=command_intent.Proposal(
                    operation="share", selector="liver", target=EXTERNAL
                ),
            )
        ),
    )
    eid = await bridge.accept(payload(text=f"Give {EXTERNAL} access to liver by email"))
    assert await bridge.process(eid, 0) == "complete"
    assert not Registry(bridge.store).load().can_use(bridge.pid, EXTERNAL)
    await command_approval.handle(bridge, bridge.store, action(eid))
    assert Registry(bridge.store).load().can_use(bridge.pid, EXTERNAL)
    assert Registry(bridge.store).load().defaults[EXTERNAL] == bridge.pid
    await command_approval.handle(bridge, bridge.store, action(eid))
    assert Registry(bridge.store).load().projects[bridge.pid].access_revision == 1


async def test_slack_recipient_outage_finishes_without_false_actor_outage(bridge, monkeypatch):
    # Actor authentication still succeeds; only the recipient lookup is unavailable.
    bridge.settings.access_mode = "workspace"
    monkeypatch.setattr(access, "resolve_email", lambda *a: SENDER)
    monkeypatch.setattr(
        channel_access, "target_email", Mock(side_effect=IdentityUnavailable("http_503"))
    )
    eid = await bridge.accept(payload(text="share liver <@U2>"))
    assert await bridge.process(eid, 0) == "failed"
    record = await bridge.repository.get_connector_state("claude-event:" + eid)
    assert "No access was changed" in record["notice_text"]
    assert "complete email address" in record["notice_text"]
    assert not record.get("identity_unavailable_since")
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")


async def test_nonowner_cannot_share_even_to_an_explicit_address(bridge):
    Registry(bridge.store).update(
        "member", SENDER, "share", lambda d: d.share(bridge.pid, SENDER, "bob@example.com")
    )
    eid = await bridge.accept(payload(text=f"share liver {EXTERNAL}", user="U2", channel="D2"))
    assert await bridge.process(eid, 0) == "failed"
    assert not Registry(bridge.store).load().can_use(bridge.pid, EXTERNAL)


async def intake(mail, mid, sender, text):
    mid = hashlib.sha256(mid.encode()).hexdigest()[:16]
    mail.add_message(mid, sender=sender, text=text)
    await gmail.ingest(mail, mail.gmail, mid, "Label_1", 0)
    return next(
        (k.split(":", 1)[1], v)
        for k, v in mail.repository.connector_state.items()
        if k.startswith("claude-event:") and v["payload"]["_email"]["id"] == mid
    )


async def test_owner_and_email_only_member_use_unpublished_project(mail, monkeypatch):
    registry = Registry(mail.store)
    registry.update("grant", SENDER, "share", lambda d: d.share(mail.pid, SENDER, EXTERNAL))
    assert not registry.load().projects[mail.pid].published_channels
    mail.app.allowed_emails = ("unrelated@example.org",)
    mail.settings.access_mode = "channel"
    monkeypatch.setattr(access, "resolve_email", Mock(side_effect=AssertionError("Slack actor")))
    direct.decide.return_value = direct.Decision(route="reply", reply="Project answer.")
    # The creator already has the project selected, independent of channel publication.
    eid, record = await intake(mail, "owner", SENDER, "Tell me about this project")
    assert await direct.handle(mail, eid, record, mail.store)
    assert access.resolve(mail.app, mail.settings, mail.store, record).allowed
    # A member with no Slack profile can discover and select it in email.
    eid, record = await intake(mail, "list", EXTERNAL, "projects")
    assert await direct.handle(mail, eid, record, mail.store)
    assert "liver" in record["notice_text"]
    eid, record = await intake(mail, "choose", EXTERNAL, "use liver")
    assert await direct.handle(mail, eid, record, mail.store)
    eid, record = await intake(mail, "question", EXTERNAL, "Tell me about this project")
    assert await direct.handle(mail, eid, record, mail.store)
    assert record["notice_text"] == "Project answer."
    assert access.resolve(mail.app, mail.settings, mail.store, record).allowed
    assert EXTERNAL in str(sent_messages(mail)[-1]["To"])
    registry.update(
        "revoke", SENDER, "unshare", lambda d: d.share(mail.pid, SENDER, EXTERNAL, remove=True)
    )
    assert not access.resolve(mail.app, mail.settings, mail.store, record).allowed
    assert not registry.load().projects[mail.pid].published_channels


async def test_first_invitation_routes_first_email_without_selection(mail):
    Registry(mail.store).update(
        "grant", SENDER, "share", lambda d: d.share(mail.pid, SENDER, EXTERNAL)
    )
    direct.decide.return_value = direct.Decision(route="reply", reply="Project answer.")
    eid, record = await intake(mail, "first-question", EXTERNAL, "Tell me about my project")
    grant = access.resolve(mail.app, mail.settings, mail.store, record)
    assert grant.allowed and grant.project_id == mail.pid
    assert await direct.handle(mail, eid, record, mail.store)
    assert record["notice_text"] == "Project answer."
    assert EXTERNAL in str(sent_messages(mail)[-1]["To"])
    _, outsider = await intake(mail, "other-address", "other@example.com", "My project?")
    assert not access.resolve(mail.app, mail.settings, mail.store, outsider).allowed


async def test_legacy_member_without_selection_gets_email_instructions(mail):
    def legacy_invite(doc):
        doc.projects[mail.pid].members.add(EXTERNAL)
        return mail.pid

    Registry(mail.store).update("legacy-grant", SENDER, "share", legacy_invite)
    eid, record = await intake(mail, "unselected", EXTERNAL, "Tell me about my project")
    assert await direct.handle(mail, eid, record, mail.store)
    assert "List my projects" in record["notice_text"]
    assert "Use <project name>" in record["notice_text"]
    assert "publishing to a Slack channel" in record["notice_text"]


async def test_unshared_verified_sender_cannot_read_unpublished_project(mail):
    eid, record = await intake(mail, "outsider", EXTERNAL, "projects")
    assert await direct.handle(mail, eid, record, mail.store)
    assert "liver" not in record["notice_text"]
    eid, record = await intake(mail, "unauthorized", EXTERNAL, "use liver")
    with pytest.raises(ValueError, match="unavailable"):
        await direct.handle(mail, eid, record, mail.store)
    direct.decide.assert_not_awaited()
