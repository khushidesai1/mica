"""Inviting a collaborator must not revoke another member's queued worker."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from test_analysis_batch import bridge, payload  # noqa: F401
from test_email_project_access import EXTERNAL, intake
from test_gmail_intake import SENDER, mail, signing  # noqa: F401

from mica.agents.analysis import access
from mica.agents.analysis.config import CHECKPOINT_PREFIX
from mica.agents.analysis.registry import Registry
from mica.api import slack_analysis


@pytest.mark.parametrize("email", [False, True])
@pytest.mark.parametrize("addition", ["share", "publish"])
async def test_invitation_preserves_queued_member_worker_and_revocation_still_blocks(
    mail, monkeypatch, email, addition
):
    registry = Registry(mail.store)
    member = EXTERNAL if email else "bob@example.com"
    registry.update("member", SENDER, "share", lambda d: d.share(mail.pid, SENDER, member))
    if email:
        eid, _ = await intake(mail, "compute-request", member, "Plot these data")
    else:
        eid = await mail.accept(payload(text="Plot these data", user="U2", channel="D2"))
    assert await mail.process(eid, 0) == "running"
    request = json.loads(mail.store.get(f"{CHECKPOINT_PREFIX}/events/{eid}/request.json")[0])
    state = json.loads(mail.store.get(access.checkpoint_key(request["thread_key"]))[0])
    assert access.worker_permitted(mail.store, request, state, settings=mail.settings)
    http_request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer worker-identity")],
            "app": SimpleNamespace(
                state=SimpleNamespace(
                    services=SimpleNamespace(settings=mail.app, repository=mail.repository)
                )
            ),
        }
    )
    monkeypatch.setattr(
        slack_analysis.id_token,
        "verify_oauth2_token",
        Mock(return_value={"email": mail.app.batch_service_account, "email_verified": True}),
    )
    await slack_analysis.worker_context(eid, http_request)
    registry.update(
        "another-invite",
        SENDER,
        addition,
        lambda d: (
            d.share(mail.pid, SENDER, "next@example.com")
            if addition == "share"
            else d.publish(mail.pid, SENDER, "T1", "C2")
        ),
    )
    assert access.worker_permitted(mail.store, request, state, settings=mail.settings)
    await slack_analysis.worker_context(eid, http_request)
    registry.update(
        "remove", SENDER, "unshare", lambda d: d.share(mail.pid, SENDER, member, remove=True)
    )
    assert not access.worker_permitted(mail.store, request, state, settings=mail.settings)
    with pytest.raises(HTTPException) as exc:
        await slack_analysis.worker_context(eid, http_request)
    assert exc.value.status_code == 403
    registry.update("reinvite", SENDER, "share", lambda d: d.share(mail.pid, SENDER, member))
    assert not access.worker_permitted(mail.store, request, state, settings=mail.settings)
    with pytest.raises(HTTPException) as exc:
        await slack_analysis.worker_context(eid, http_request)
    assert exc.value.status_code == 403


def test_channel_revocation_and_republication_never_revive_old_grants(bridge):
    registry = Registry(bridge.store)
    registry.update(
        "publish", SENDER, "publish", lambda d: d.publish(bridge.pid, SENDER, "T1", "C1")
    )
    before = registry.load().projects[bridge.pid].access_revision
    registry.update(
        "unpublish",
        SENDER,
        "unpublish",
        lambda d: d.publish(bridge.pid, SENDER, "T1", "C1", remove=True),
    )
    assert registry.load().projects[bridge.pid].access_revision == before + 1
    registry.update(
        "republish", SENDER, "publish", lambda d: d.publish(bridge.pid, SENDER, "T1", "C1")
    )
    assert registry.load().projects[bridge.pid].access_revision == before + 1
