"""Lab grants stay live, surface-independent and separate from project ownership."""

# ruff: noqa: F811
import json

import pytest
from pydantic import ValidationError
from test_analysis_channel_access import (  # noqa: F401
    bridge,
    channels,
    chat,
    onboard,
    payload,
    receive,
)

from mica.agents.analysis import access, conversation, identity, lab
from mica.agents.analysis.registry import REGISTRY_KEY, Registry, RegistryDoc
from mica.agents.analysis.storage import Conflict, LocalStorage
from scripts.lab_members import main, replace

A, B, C = "alice@example.com", "bob@example.com", "carol@example.com"


def seed(storage, members=(A,), all_projects=()):
    storage.put(
        lab.KEY,
        lab.Lab(members=set(members), all_projects=set(all_projects)).model_dump_json().encode(),
    )


def test_validation_normalizes_and_deduplicates_and_missing_is_empty(tmp_path):
    assert lab.load(LocalStorage(tmp_path)) == lab.Lab()
    doc = lab.Lab(members=[" Alice@EXAMPLE.com ", A, B], all_projects=[" ALICE@example.com "])
    assert doc.members == {A, B} and doc.all_projects == {A}
    assert json.loads(doc.model_dump_json())["members"] == [A, B]


@pytest.mark.parametrize(
    "data",
    [
        {"members": [A], "all_projects": [B]},
        {"members": ["invalid"]},
        {"members": ["Alice <alice@example.com>"]},
        {"version": 2},
        {"all_project": [A]},
    ],
)
def test_invalid_lab_document_is_rejected(data, tmp_path):
    store = LocalStorage(tmp_path)
    store.put(lab.KEY, json.dumps(data).encode())
    with pytest.raises(ValidationError):
        lab.load(store)


@pytest.mark.parametrize("enabled", [False, True])
async def test_lab_admission_and_removal_at_intake_resolver_and_worker(channels, enabled):
    if enabled:
        await onboard(channels)
        channels.members["CACCESS1"].remove("U1")
    body = payload("2.0")
    record = {"payload": body, "thread_key": "empty"}
    assert not access.resolve(channels.app, channels.settings, channels.store, record).email
    assert await receive(channels, body) == {"ok": True}
    seed(channels.store)
    # Even cached profiles must not cache a lab grant.
    channels.settings.access_cache_seconds = 60
    grant = access.resolve(channels.app, channels.settings, channels.store, record)
    assert grant.email == A and grant.allowed
    channels.decision.return_value = conversation.Decision(route="compute", reply="Computing")
    event = await receive(channels, payload("3.0"))
    assert await channels.process(event["event_id"], 0) == "running"
    saved = await channels.repository.get_connector_state("claude-event:" + event["event_id"])
    grant = access.resolve(channels.app, channels.settings, channels.store, saved)
    request = json.loads(
        channels.store.get(f"tasks/slack-analysis/events/{event['event_id']}/request.json")[0]
    )
    assert access.worker_permitted(channels.store, request, grant.state, settings=channels.settings)
    assert await receive(channels, payload("4.0", user="U3", channel="D3")) == {"ok": True}
    seed(channels.store, members=())
    assert not access.worker_permitted(
        channels.store, request, grant.state, settings=channels.settings
    )
    assert not access.resolve(channels.app, channels.settings, channels.store, saved).email
    assert await receive(channels, payload("5.0")) == {"ok": True}


async def test_lab_member_without_project_membership_lists_no_projects(channels):
    seed(channels.store, members=(C,))
    doc = Registry(channels.store).load()
    assert not doc.visible(C) and not doc.can_use(channels.pid, C)
    event = await receive(channels, payload(user="U3", channel="D3", text="projects"))
    assert await channels.process(event["event_id"], 0) == "complete"
    saved = await channels.repository.get_connector_state("claude-event:" + event["event_id"])
    assert "No projects yet." in saved["notice_text"]
    assert "liver" not in saved["notice_text"]


@pytest.mark.parametrize("flag", ["deleted", "is_bot", "is_app_user", "wrong_workspace"])
def test_lab_fallback_requires_active_human_in_bot_workspace(channels, flag):
    seed(channels.store)
    if flag == "wrong_workspace":
        channels.member_api.auth_test.return_value["team_id"] = "T2"
    else:
        channels.member_api.users_info.side_effect = None
        channels.member_api.users_info.return_value = {
            "ok": True,
            "user": {"id": "U1", flag: True, "profile": {"email": A}},
        }
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U1") is None
    )


@pytest.mark.parametrize("failure", ["profile", "channel"])
async def test_lab_does_not_turn_slack_outage_into_denial_or_grant(channels, failure):
    from fastapi import HTTPException

    seed(channels.store)
    if failure == "channel":
        await onboard(channels)
        channels.member_api.conversations_members.side_effect = TimeoutError("Slack unavailable")
    else:
        channels.member_api.users_info.side_effect = TimeoutError("Slack unavailable")
    with pytest.raises(identity.IdentityUnavailable):
        access.resolve(
            channels.app,
            channels.settings,
            channels.store,
            {
                "payload": payload("2.0"),
                "thread_key": "empty",
            },
        )
    with pytest.raises(HTTPException) as error:
        await receive(channels, payload("2.0"))
    assert error.value.status_code == 503


def test_mapped_and_workspace_admission_unchanged(channels):
    seed(channels.store, members=(C,))
    channels.settings.access_mode = "mapped"
    channels.app.slack_user_map = {"T1:U1": A}
    assert (
        identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U3") is None
    )
    assert identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U1") == A
    channels.settings.access_mode = "workspace"
    assert identity.resolve_email(channels.app, channels.settings, channels.store, "T1", "U2") == B


def test_registry_grants_current_future_projects_without_persisting_or_ownership(tmp_path):
    store = LocalStorage(tmp_path)
    registry = Registry(store)
    seed(store, members=(B, C), all_projects=(B,))
    first = registry.update("first", A, "create", lambda d: d.create(A, "first", "gs://data/"))
    original = store.get(REGISTRY_KEY)
    doc = registry.load()
    assert doc.can_use(first, B.upper()) and not doc.owns(first, B)
    assert not doc.can_use("missing", B) and not doc.visible(C)
    assert B not in doc.projects[first].members
    assert not RegistryDoc.model_validate_json(doc.model_dump_json()).can_use(first, B)
    assert store.get(REGISTRY_KEY) == original
    registry.update("first", A, "create", lambda d: pytest.fail("replayed operation"))
    assert store.get(REGISTRY_KEY) == original
    second = registry.update("second", A, "create", lambda d: d.create(A, "second", "gs://data/"))
    assert set(registry.load().visible(B)) == {first, second}

    def select(doc):
        assert doc.can_use(second, B)
        doc.bind("T1", "D2", B, second, private=True)
        return second

    assert registry.update("select", B, "switch", select) == second
    persisted = RegistryDoc.model_validate_json(store.get(REGISTRY_KEY)[0])
    assert persisted.defaults[B] == second and not persisted.visible(B)
    assert "all_projects" not in store.get(REGISTRY_KEY)[0].decode()
    for change in (
        lambda d: d.share(first, B, C),
        lambda d: d.rename(first, B, "renamed"),
        lambda d: d.publish(first, B, "T1", "C1"),
        lambda d: d.set_co_owner(first, B, C),
    ):
        with pytest.raises(ValueError, match="owner"):
            registry.update("forbidden", B, "mutation", change)
    seed(store, members=(B, C))
    assert not registry.load().visible(B)
    with pytest.raises(ValueError, match="unavailable"):
        registry.update(
            "select-again", B, "switch", lambda d: d.bind("T1", "D2", B, second, private=True)
        )


async def test_all_projects_worker_requires_channel_publication_and_live_grant(channels):
    seed(channels.store, members=(B,), all_projects=(B,))
    registry = Registry(channels.store)

    def select(doc):
        doc.bind("T1", "D2", B, channels.pid, private=True)
        return channels.pid

    registry.update("select", B, "switch", select)
    channels.decision.return_value = conversation.Decision(route="compute", reply="Computing")
    event = await receive(channels, payload(user="U2", channel="D2"))
    assert await channels.process(event["event_id"], 0) == "running"
    saved = await channels.repository.get_connector_state("claude-event:" + event["event_id"])
    grant = access.resolve(channels.app, channels.settings, channels.store, saved)
    request = json.loads(
        channels.store.get(f"tasks/slack-analysis/events/{event['event_id']}/request.json")[0]
    )
    assert access.worker_permitted(channels.store, request, grant.state, settings=channels.settings)
    # Project grants do not bypass the independent audience publication check.
    request["payload"]["event"].update(channel="C1", channel_type="channel")
    request["private"] = False
    channel_grant = access.Grant(
        B,
        False,
        grant.state,
        registry.load(),
        channels.pid,
        True,
        "T1:C1",
        identity_policy="channel:T1",
    )
    request["access_signature"] = channel_grant.signature
    assert not access.worker_permitted(
        channels.store, request, grant.state, settings=channels.settings
    )
    registry.update("publish", A, "publish", lambda d: d.publish(channels.pid, A, "T1", "C1"))
    assert access.worker_permitted(channels.store, request, grant.state, settings=channels.settings)
    seed(channels.store, members=(B,))
    assert not access.worker_permitted(
        channels.store, request, grant.state, settings=channels.settings
    )
    request["payload"]["event"].update(channel="D2", channel_type="im")
    request["private"] = True
    request["access_signature"] = grant.signature
    assert not access.worker_permitted(
        channels.store, request, grant.state, settings=channels.settings
    )


def test_script_show_set_create_only_and_stale_generation(tmp_path, capsys):
    source = tmp_path / "lab.json"
    source.write_text(json.dumps({"members": [B, A.upper(), A], "all_projects": [B]}))
    root = tmp_path / "store"
    main(["--root", str(root), "show"])
    out = capsys.readouterr()
    assert json.loads(out.out) == {"version": 1, "members": [], "all_projects": []}
    assert "missing" in out.err
    main(["--root", str(root), "set", str(source), "--generation", "0"])
    assert "+" in capsys.readouterr().out
    store = LocalStorage(root)
    original = store.get(lab.KEY)
    with pytest.raises(Conflict):
        main(["--root", str(root), "set", str(source), "--generation", "0"])
    replace(store, lab.load(store), expected=original[1])
    assert "No changes." in capsys.readouterr().out
    assert store.get(lab.KEY) == original


def test_script_does_not_overwrite_or_retry_concurrent_operator_edit(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    original = store.put_if

    def race(key, data, generation):
        seed(store, members=(C,))
        return original(key, data, generation)

    monkeypatch.setattr(store, "put_if", race)
    with pytest.raises(Conflict):
        replace(store, lab.Lab(members={A}))
    assert lab.load(store).members == {C}
