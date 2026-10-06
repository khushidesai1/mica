import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from pistachio.agents.analysis import write_access, write_worker
from pistachio.agents.analysis.runner import Context, TurnResult
from pistachio.agents.analysis.storage import LocalStorage
from pistachio.api import slack_analysis


@pytest.fixture
def writable(bridge):  # noqa: F811
    bridge.settings.write_admin_ids = "T1:U1"
    bridge.settings.write_allowed_prefixes = "gs://lab-data/liver/output/"
    bridge.settings.write_service_account = "writer@example.com"
    return bridge


async def allow(agent, ts="2.0", thread=None):
    event_id = await agent.accept(
        payload(ts, thread, "write grant liver gs://lab-data/liver/output/")
    )
    assert await agent.process(event_id, 0) == "complete"
    return write_access.active(agent.store, agent.pid, agent.settings)


async def test_commands_do_not_use_a_model_or_worker_and_revoke_bypasses_queue(writable):
    grant = await allow(writable)
    assert grant.expires_at is None
    writable.test_client.create_job.assert_not_called()
    compute = await writable.accept(payload("3.0"))
    assert await writable.process(compute, 0) == "running"
    manifest = json.loads(
        writable.store.get(f"tasks/slack-analysis/events/{compute}/request.json")[0]
    )
    assert manifest["write_grant"]["id"] == grant.id
    revoke = await writable.accept(payload("4.0", "3.0", "write revoke liver"))
    assert await writable.process(revoke, 0) == "complete"
    assert write_access.active(writable.store, writable.pid, writable.settings) is None
    queued = await writable.accept(payload("5.0", "3.0", "Make another plot"))
    assert await writable.process(queued, 0) == "pending"
    assert writable.test_client.create_job.call_count == 1


async def test_command_in_thread_targets_explicit_project_without_rebinding(writable):
    from analysis_helpers import seed_project

    other = seed_project(writable.store, name="other", channel="D2")
    compute = await writable.accept(payload("1.0"))
    await writable.process(compute, 0)
    command = await writable.accept(
        payload("2.0", "1.0", f"write grant {other} gs://lab-data/liver/output/")
    )
    await writable.process(command, 0)
    assert write_access.active(writable.store, other, writable.settings)
    assert write_access.active(writable.store, writable.pid, writable.settings) is None
    record = await writable.repository.get_connector_state("claude-event:" + compute)
    state = json.loads(
        writable.store.get(f"projects/_threads/{record['thread_key']}/checkpoint.json")[0]
    )
    assert state["context"]["project_slug"] == writable.pid


async def worker_request(writable, monkeypatch):
    await allow(writable)
    compute = await writable.accept(payload("3.0"))
    await writable.process(compute, 0)
    services = SimpleNamespace(settings=writable.app, repository=writable.repository)
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", b"Bearer identity")],
            "app": SimpleNamespace(state=SimpleNamespace(services=services)),
        }
    )
    verify = Mock(
        return_value={"email": writable.app.batch_service_account, "email_verified": True}
    )
    monkeypatch.setattr(slack_analysis.id_token, "verify_oauth2_token", verify)
    grant = write_access.active(writable.store, writable.pid, writable.settings)
    body = slack_analysis.WriteUpload(relative_path="report.csv", grant_id=grant.id)
    return compute, request, verify, body


async def test_broker_endpoint_checks_worker_event_and_current_grant(writable, monkeypatch):
    compute, request, verify, body = await worker_request(writable, monkeypatch)

    def upload(*args):
        args[-1]()  # Execute the endpoint's per-chunk authorization callback.
        return {"uri": "gs://lab-data/liver/output/report.csv", "status": "uploaded"}

    uploaded = Mock(side_effect=upload)
    monkeypatch.setattr(slack_analysis.write_broker, "upload", uploaded)
    response = Response()
    assert (await slack_analysis.write_output(compute, body, request, response))[
        "status"
    ] == "uploaded"
    assert response.headers["Cache-Control"] == "no-store"
    verify.return_value["email"] = "untrusted@example.com"
    with pytest.raises(HTTPException) as error:
        await slack_analysis.write_output(compute, body, request, Response())
    assert error.value.status_code == 403
    verify.return_value["email"] = writable.app.batch_service_account
    revoke = await writable.accept(payload("4.0", "3.0", "write revoke liver"))
    await writable.process(revoke, 0)
    with pytest.raises(HTTPException) as error:
        await slack_analysis.write_output(compute, body, request, Response())
    assert error.value.status_code == 403


async def test_broker_does_not_accept_new_grant_for_old_run(writable, monkeypatch):
    compute, request, _, body = await worker_request(writable, monkeypatch)
    new = await allow(writable, ts="4.0", thread="3.0")
    body.grant_id = new.id
    upload = Mock()
    monkeypatch.setattr(slack_analysis.write_broker, "upload", upload)
    with pytest.raises(HTTPException) as error:
        await slack_analysis.write_output(compute, body, request, Response())
    assert error.value.status_code == 403
    upload.assert_not_called()


@pytest.fixture
def worker(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path / "storage")
    ctx = Context(tmp_path / "work", tmp_path / "data")
    root = ctx.workdir / "outputs/gcs"
    root.mkdir(parents=True)
    path = root / "report.csv"
    path.write_text("a,b\n1,2\n")
    result = TurnResult(text="Prepared report", files=[path])
    token = Mock(return_value="trusted-worker-id-token")
    monkeypatch.setattr(write_worker.id_token, "fetch_id_token", token)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(
        200,
        json={"uri": "gs://lab-data/result.csv"},
        request=httpx.Request("POST", "https://control/write"),
    )
    monkeypatch.setattr(write_worker.httpx, "AsyncClient", lambda **kwargs: client)
    request = {"write_grant": {"id": "b" * 32}}
    return SimpleNamespace(
        store=store, ctx=ctx, result=result, request=request, client=client, token=token, path=path
    )


async def publish(worker):
    return await write_worker.publish(
        worker.store, worker.request, "a" * 32, "https://control", worker.ctx, worker.result
    )


async def test_worker_stages_changed_exports_and_sends_no_cloud_credential(worker):
    report = await publish(worker)
    assert report.uploaded == ["gs://lab-data/result.csv"] and not report.errors
    call = worker.client.post.call_args.kwargs
    assert call["json"] == {"relative_path": "report.csv", "grant_id": "b" * 32}
    assert (
        worker.store.get("tasks/slack-analysis/events/" + "a" * 32 + "/write-exports/report.csv")[0]
        == worker.path.read_bytes()
    )


@pytest.mark.parametrize("problem", ["no-grant", "failed-turn", "symlink", "unchanged"])
async def test_worker_does_not_publish_without_eligible_files_and_grant(worker, problem):
    if problem == "no-grant":
        worker.request["write_grant"] = None
    elif problem == "failed-turn":
        worker.result.error = "stopped"
    elif problem == "symlink":
        worker.path.unlink()
        worker.path.symlink_to(worker.ctx.workdir / "outside")
        (worker.ctx.workdir / "outside").write_text("private")
    else:
        worker.result.files = []
    report = await publish(worker)
    assert not report.uploaded
    worker.token.assert_not_called()
    worker.client.post.assert_not_called()


async def test_worker_reports_denial_and_stops_after_revocation(worker):
    second = worker.path.with_name("second.csv")
    second.write_text("second")
    worker.result.files.append(second)
    worker.client.post.return_value = httpx.Response(403, json={"detail": "Write access expired."})
    report = await publish(worker)
    assert not report.uploaded and "expired" in report.errors[0]
    assert worker.client.post.call_count == 1


async def test_slack_uses_confirmed_publication_outcome_over_model_claim(bot):  # noqa: F811
    bot.publish_gcs = AsyncMock(
        return_value=write_worker.Publication(errors=["GCS write revoked."])
    )
    await send(bot, event("Save the plot to GCS"))
    assert "GCS write revoked" in bot.last_reply and bot.last_error
    bot.publish_gcs.return_value = write_worker.Publication(uploaded=["gs://lab-data/result.csv"])
    await send(bot, event("Save it now", ts="2.0", thread="1.0"))
    assert "Saved to GCS" in bot.last_reply and "gs://lab-data/result.csv" in bot.last_reply
    assert not bot.last_error
