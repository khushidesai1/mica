"""Model-visible permission gateways match enforcement without disclosing private projects."""
# ruff: noqa: F811

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_conversation_tools import Client, reply, tool, turn  # noqa: F401

from mica.agents.analysis import access, command_approval, commands, conversation, direct
from mica.agents.analysis.command_intent import Proposal
from mica.agents.analysis.config import Settings
from mica.agents.analysis.registry import ProjectEntry, Registry, RegistryDoc


@pytest.fixture
def policy():
    entry = ProjectEntry(name="private-liver", owner="owner@example.com",
                         co_owners={"co@example.com"}, members={"member@example.com"},
                         data_uri="gs://secret/data", published_channels={"T1:C1"})
    doc = RegistryDoc(projects={"secret-project-id": entry})
    grant = access.Grant("member@example.com", True, {}, doc, "secret-project-id", True, "T1:C1")
    return grant, {"payload": payload()}, Settings(_env_file=None)


@pytest.mark.parametrize("role,email", [
    ("owner", "owner@example.com"), ("co_owner", "co@example.com"),
    ("member", "member@example.com"),
])
@pytest.mark.parametrize("surface", ["slack_dm", "slack_channel", "email"])
def test_role_surface_and_gateways(policy, role, email, surface):
    grant, record, settings = policy
    grant = replace(grant, email=email, private=surface != "slack_channel")
    if surface == "email":
        record["surface"] = "email"
    view = commands.permission_view(grant, record, settings)
    assert view["surface"] == surface
    assert view["project"] == {"id": grant.project_id, "name": "private-liver", "role": role}
    assert ("owner" in view["blocked"]) == (role == "member")
    if role == "member":
        assert view["blocked"]["owner"]["gateway"] == "the project owner (owner@example.com)"
    assert ("dm_only" in view["blocked"]) == (surface == "slack_channel")
    assert ("email_read_only" in view["blocked"]) == (surface == "email")
    assert view["blocked"]["write_admin"]["operations"] == ["write grant", "write revoke"]
    assert view["project_access"]["gateway"] is None
    assert "available" not in view


@pytest.mark.parametrize("private,published", [(False, True), (False, False), (True, True)])
def test_nonmember_metadata_matches_deny_text(policy, private, published):
    grant, record, settings = policy
    grant = replace(grant, email="outsider@example.com", private=private, allowed=False)
    if not published:
        grant.project.published_channels.clear()
    view = commands.permission_view(grant, record, settings)
    disclosed = not private and published
    assert ("private-liver" in json.dumps(view)) == disclosed
    assert ("owner@example.com" in json.dumps(view)) == disclosed
    assert ("private-liver" in access.deny_text(grant)) == disclosed
    assert "secret-project-id" not in json.dumps(view)
    assert "gs://secret" not in json.dumps(view)
    assert view["project_access"]["gateway"] == "ask the project owner"


def test_access_request_gateway_matches_handler_eligibility(policy):
    grant, record, settings = policy
    settings.access_request_owner_id = "T1:UOWNER"
    grant = replace(grant, email="outsider@example.com", allowed=False)
    assert commands.permission_view(grant, record, settings)["project_access"]["gateway"] == (
        "request_access owner card"
    )
    record["surface"] = "email"
    assert commands.permission_view(grant, record, settings)["project_access"]["gateway"] == (
        "ask the project owner"
    )
    record.pop("surface")
    settings.access_request_owner_id = "T2:UOWNER"
    assert commands.permission_view(grant, record, settings)["project_access"]["gateway"] == (
        "ask the project owner"
    )
    grant = replace(grant, email="member@example.com", project_id="")
    assert commands.permission_view(grant, record, settings)["project_access"]["gateway"] is None


@pytest.mark.parametrize("operation", [n for n, s in commands.COMMANDS.items() if s.requires])
def test_every_role_policy_matches_approval_and_view(policy, operation):
    grant, record, settings = policy
    cmd = commands.Command(operation, (grant.project_id, "value"))
    reason = commands.spec(operation).requires
    with pytest.raises(ValueError, match="owner" if reason == "owner" else "administrators"):
        command_approval.authorize(SimpleNamespace(settings=settings), None, cmd, grant, record)
    failure, = commands.permission_blocks(operation, grant, record, settings)
    assert failure.reason == reason
    group = commands.permission_view(grant, record, settings)["blocked"][failure.reason]
    assert operation in group["operations"] and group["gateway"] == failure.gateway


def test_write_admin_uses_slack_identity_not_owner_role(policy):
    grant, record, settings = policy
    settings.write_admin_ids = "T1:U1"
    view = commands.permission_view(grant, record, settings)
    assert "write_admin" not in view["blocked"] and "owner" in view["blocked"]
    command_approval.authorize(SimpleNamespace(settings=settings), None,
                               commands.Command("write revoke", (grant.project_id,)), grant, record)


def test_command_checks_resolved_project_not_current_thread(policy):
    grant, record, settings = policy
    grant.doc.projects["owned-project"] = ProjectEntry(
        name="owned", owner=grant.email, data_uri="gs://data/owned",
    )
    cmd = conversation.validate_command(
        Proposal(operation="rename", selector="owned", target="renamed"), grant, record,
        settings=settings,
    )
    assert cmd.args[0] == "owned-project"
    grant = replace(grant, project_id="owned-project")
    with pytest.raises(commands.PermissionRequired) as error:
        conversation.validate_command(
            Proposal(operation="rename", selector="private-liver", target="renamed"),
            grant, record, settings=settings,
        )
    assert error.value.gateway == "the project owner (owner@example.com)"


async def test_member_denial_is_repairable_and_model_writes_reply(turn, monkeypatch):
    bridge, event_id, record, _ = turn
    Registry(bridge.store).update(
        "share", "alice@example.com", "share",
        lambda d: d.share(bridge.pid, "alice@example.com", "bob@example.com"),
    )
    record["payload"]["event"].update(user="U2", channel="D2")
    record["payload"]["event"]["text"] = "Rename liver to renamed"
    sdk = Client(tool("propose_command", operation="rename", selector="liver", target="renamed"),
                 reply("Please ask alice@example.com, the project owner, to rename it."))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    error = sdk.calls[1]["messages"][-1]["content"][0]
    assert error["is_error"] and json.loads(error["content"])["reason"] == "owner"
    assert record["notice_text"].startswith("Please ask alice@example.com")
    assert not bridge.store.keys("projects/_command_approvals/")
    bridge.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("prior", [False, True])
async def test_share_not_as_owner_reaches_confirmation_card(turn, monkeypatch, prior):
    bridge, event_id, record, _ = turn
    request = "share liver with bob@example.com, not as an owner"
    record["payload"]["event"]["text"] = "yes" if prior else request
    if prior:
        record["intent_prior_request"] = request
    sdk = Client(tool("propose_command", operation="share", selector="liver",
                      target="bob@example.com", scope="collaborator"))
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: sdk)
    monkeypatch.setattr(command_approval, "AsyncWebClient", lambda **kw: bridge.test_slack)
    assert await direct.handle(bridge, event_id, record, bridge.store)
    row = json.loads(bridge.store.get(command_approval.key(event_id))[0])
    assert row["state"] == "pending" and row["command"]["name"] == "share"
    assert "collaborator access" in str(bridge.test_slack.chat_update.call_args)
    assert not Registry(bridge.store).load().can_use(bridge.pid, "bob@example.com")


def test_share_still_rejects_ownership_scope(policy):
    grant, record, settings = policy
    with pytest.raises(ValueError, match="collaborator access"):
        conversation.validate_command(
            Proposal(operation="share", selector="private-liver", target="new@example.com",
                     scope="owner"), grant, record, settings=settings,
        )


def test_declared_roles_preserve_existing_approval_policy():
    for name, policy in commands.COMMANDS.items():
        expected = ("owner" if policy.registry and policy.approval else
                    "write_admin" if name in {"write grant", "write revoke"} else "")
        assert policy.requires == expected
