import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from mica.agents.analysis import write_access, write_broker, write_worker
from mica.agents.analysis.runner import Context, TurnResult
from mica.agents.analysis.storage import LocalStorage
from mica.api import slack_analysis


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

    compute = await writable.accept(payload("1.0"))
    await writable.process(compute, 0)
    # A later selection changes the user's default but cannot move this pinned thread.
    other = seed_project(writable.store, name="other", channel="D2")
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
    body = slack_analysis.WriteUpload(relative_paths=["report.csv"], grant_id=grant.id)
    return compute, request, verify, body


async def test_broker_endpoint_checks_worker_event_and_current_grant(writable, monkeypatch):
    compute, request, verify, body = await worker_request(writable, monkeypatch)

    def upload(*args, **kwargs):
        args[-1]()  # Execute the endpoint's per-chunk authorization callback.
        return {"uri": "gs://lab-data/liver/output/report.csv", "status": "uploaded"}

    uploaded = Mock(side_effect=upload)
    monkeypatch.setattr(slack_analysis.write_broker, "upload", uploaded)
    response = Response()
    assert (await slack_analysis.write_output(compute, body, request, response))["results"][0][
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
    denied = await slack_analysis.write_output(compute, body, request, Response())
    assert denied["results"][0]["status"] == "denied"


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


async def test_broker_returns_unknown_access_as_retry_without_delivering(writable, monkeypatch):
    from mica.agents.analysis.identity import IdentityUnavailable

    compute, request, _, body = await worker_request(writable, monkeypatch)
    body.relative_paths = ["one", "two", "three"]
    upload = Mock(side_effect=IdentityUnavailable("timeout"))
    monkeypatch.setattr(slack_analysis.write_broker, "upload", upload)
    result = await slack_analysis.write_output(compute, body, request, Response())
    assert all(item["status"] == "in_progress" for item in result["results"])
    assert all(item["reason"] == "identity_unavailable" for item in result["results"])
    upload.assert_called_once()
    assert not any(key.startswith("claude-write:") for key in writable.repository.event_claims)


@pytest.mark.parametrize("stop", ["cancelled", "access_unverifiable"])
async def test_inflight_broker_stops_after_stop_marker(writable, monkeypatch, stop):
    compute, request, _, body = await worker_request(writable, monkeypatch)
    body.relative_paths = ["one", "two", "three"]

    def upload(*args, **kwargs):
        root = f"tasks/slack-analysis/events/{compute}"
        if stop == "cancelled":
            writable.store.put(root + "/memory-cancelled.json", b"true")
        else:
            writable.store.put(root + "/attempt-a0.json", b'{"outcome":"stopped"}')
        args[-1]()
        raise AssertionError("Stopped attempt must not publish")

    copied = Mock(side_effect=upload)
    monkeypatch.setattr(slack_analysis.write_broker, "upload", copied)
    result = await slack_analysis.write_output(compute, body, request, Response())
    assert all(item["status"] == "denied" for item in result["results"])
    copied.assert_called_once()


@pytest.mark.parametrize(
    "kind",
    ["tls", "timeout", "unavailable", "iam_unavailable", "iam_rate_limit", "refresh_retryable"],
)
async def test_batch_preserves_successes_when_one_gcs_request_is_interrupted(
    writable, monkeypatch, kind
):
    from google.api_core.exceptions import ServiceUnavailable
    from google.auth.exceptions import RefreshError
    from requests.exceptions import ReadTimeout, SSLError

    compute, request, _, body = await worker_request(writable, monkeypatch)
    body.relative_paths = ["first.csv", "retry.csv", "last.csv"]
    failed = False
    errors = {
        "tls": SSLError("connection interrupted"),
        "timeout": ReadTimeout("connection interrupted"),
        "unavailable": ServiceUnavailable("connection interrupted"),
        "iam_unavailable": RefreshError(
            "Unable to acquire impersonated credentials", '{"error":{"code":503}}'
        ),
        "iam_rate_limit": RefreshError(
            "Unable to acquire impersonated credentials", '{"error":{"code":429}}'
        ),
        "refresh_retryable": RefreshError("Service temporarily unavailable", retryable=True),
    }

    def upload(*args, **kwargs):
        nonlocal failed
        args[-1]()
        path = args[4]
        if path == "retry.csv" and not failed:
            failed = True
            raise errors[kind]
        return {"status": "uploaded", "uri": "gs://lab-data/liver/output/" + path}

    copied = Mock(side_effect=upload)
    monkeypatch.setattr(slack_analysis.write_broker, "upload", copied)
    result = await slack_analysis.write_output(compute, body, request, Response())
    assert [r["status"] for r in result["results"]] == ["uploaded", "in_progress", "uploaded"]
    assert not any(k.startswith("claude-write:") for k in writable.repository.event_claims)
    body.relative_paths = [
        r["relative_path"] for r in result["results"] if r["status"] == "in_progress"
    ]
    retried = await slack_analysis.write_output(compute, body, request, Response())
    assert retried["results"][0]["status"] == "uploaded"
    assert [call.args[4] for call in copied.call_args_list] == [
        "first.csv",
        "retry.csv",
        "last.csv",
        "retry.csv",
    ]


@pytest.mark.parametrize(
    "response",
    ['{"error":{"code":403}}', '{"error":{"code":401}}', "503 unavailable", "[]", '{"error":{}}'],
)
async def test_batch_does_not_retry_permanent_or_unclassified_refresh_errors(
    writable, monkeypatch, response
):
    from google.auth.exceptions import RefreshError

    compute, request, _, body = await worker_request(writable, monkeypatch)
    error = RefreshError("Unable to acquire impersonated credentials", response)
    monkeypatch.setattr(slack_analysis.write_broker, "upload", Mock(side_effect=error))
    with pytest.raises(RefreshError) as caught:
        await slack_analysis.write_output(compute, body, request, Response())
    assert caught.value is error
    assert not any(k.startswith("claude-write:") for k in writable.repository.event_claims)


async def uploaded_batch(url, **kwargs):
    return httpx.Response(
        200,
        json={
            "results": [
                {"relative_path": p, "status": "uploaded", "uri": "gs://lab-data/" + p}
                for p in kwargs["json"]["relative_paths"]
            ]
        },
        request=httpx.Request("POST", url),
    )


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
    client.post.side_effect = uploaded_batch
    monkeypatch.setattr(write_worker.httpx, "AsyncClient", lambda **kwargs: client)
    request = {"write_grant": {"id": "b" * 32, "uri": "gs://lab-data/"}}
    return SimpleNamespace(
        store=store, ctx=ctx, result=result, request=request, client=client, token=token, path=path
    )


async def publish(worker):
    return await write_worker.publish(
        worker.store, worker.request, "a" * 32, "https://control", worker.ctx, worker.result
    )


async def test_worker_stages_changed_exports_and_sends_no_cloud_credential(worker):
    report = await publish(worker)
    assert report.uploaded == ["gs://lab-data/report.csv"] and not report.errors
    call = worker.client.post.call_args.kwargs
    assert call["json"] == {"relative_paths": ["report.csv"], "grant_id": "b" * 32}
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
    worker.client.post.side_effect = None
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
    bot.publish_gcs.return_value = write_worker.Publication(
        uploaded=["gs://lab-data/result.csv"],
        destinations=[write_worker.Destination("gs://lab-data/", files=1, bytes=8)],
    )
    await send(bot, event("Save it now", ts="2.0", thread="1.0"))
    assert "Saved to GCS" in bot.last_reply and "gs://lab-data/" in bot.last_reply
    assert not bot.last_error


def publisher(worker):
    return write_worker.Publisher(worker.store, worker.request, "a" * 32, "https://control")


def add_store(worker, name, count=1):
    root = worker.ctx.workdir / "outputs/gcs" / name
    root.mkdir(parents=True, exist_ok=True)
    paths = [root / str(i) for i in range(count)]
    for path in paths:
        path.write_bytes(b"chunk")
    return paths


async def test_result_store_preserves_601_paths_with_bounded_concurrency(worker, monkeypatch):
    import asyncio

    paths = add_store(worker, "result/tables/proseg_table.zarr", 600)
    paths += add_store(worker, "result/points", 1)
    worker.result.files = paths
    measure = write_worker.file_sizes
    monkeypatch.setattr(
        write_worker,
        "file_sizes",
        lambda selected: {
            p: 600 * 1024**2 if "/points/" in p else n for p, n in measure(selected).items()
        },
    )
    active = maximum = 0

    async def post(url, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(active, maximum)
        await asyncio.sleep(0.001)
        active -= 1
        return await uploaded_batch(url, **kwargs)

    worker.client.post.side_effect = post
    result = await publish(worker)
    assert not result.errors and len(result.uploaded) == 601
    assert 1 <= maximum <= write_worker.CONCURRENCY
    assert worker.client.post.call_count == 2
    assert set(result.uploaded) == {
        "gs://lab-data/" + p.relative_to(worker.ctx.workdir / "outputs/gcs").as_posix()
        for p in paths
    }


@pytest.mark.parametrize("count,size", [(25000, 5), (1, 10 * 1024**3)])
async def test_large_runs_and_large_files_have_sharded_state_without_scale_caps(
    worker, monkeypatch, count, size
):
    worker.result.files = add_store(worker, "result", count)
    monkeypatch.setattr(write_worker, "file_sizes", lambda selected: {p: size for p in selected})
    result = await publish(worker)
    assert not result.errors and len(result.uploaded) == count
    assert (
        worker.client.post.call_count
        == (count + write_broker.BATCH_SIZE - 1) // write_broker.BATCH_SIZE
    )
    index = json.loads(worker.store.get(write_broker.manifest_stage("a" * 32))[0])
    assert "uploaded" not in index and "sizes" not in index
    assert len(index["shards"]) <= 3 * worker.client.post.call_count
    recovered = publisher(worker)
    recovered.load()
    assert len(recovered.uploaded) == count and sum(recovered.sizes.values()) == count * size
    write_broker.archive_manifests(worker.store, "a" * 32, "gs://lab-data/")
    archive = json.loads(worker.store.get("write-access/uploads/" + "a" * 32 + "/manifest.json")[0])
    assert archive["destinations"][0]["files"] == count
    assert all(worker.store.get(key)[0] for key in archive["shards"])


async def test_mid_turn_publish_survives_error_and_end_turn_skips_confirmed_paths(worker):
    first, second = add_store(worker, "first"), add_store(worker, "second")
    service = publisher(worker)
    response = await service.tool(worker.ctx).handler({"folder": "outputs/gcs/first"})
    assert not response["isError"]
    assert json.loads(response["content"][0]["text"])["files"] == 1
    failed = await service.publish(worker.ctx, TurnResult(files=first + second, error="late error"))
    assert failed.uploaded and failed.errors and worker.client.post.call_count == 1
    result = await service.publish(worker.ctx, TurnResult(files=first + second))
    assert len(result.uploaded) == 2 and not result.errors
    assert worker.client.post.call_count == 2
    # Durable transport receipts survive worker replacement, too.
    result = await publisher(worker).publish(worker.ctx, TurnResult(files=first + second))
    assert len(result.uploaded) == 2 and worker.client.post.call_count == 2


async def test_lost_broker_response_reuses_immutable_stage(worker):
    calls = 0

    async def lost_once(url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("lost acknowledgement")
        return await uploaded_batch(url, **kwargs)

    worker.client.post.side_effect = lost_once
    worker.store.upload_new = Mock(wraps=worker.store.upload_new)
    result = await publish(worker)
    assert result.uploaded and not result.errors and calls == 2
    staged = [c for c in worker.store.upload_new.call_args_list if "/write-exports/" in c.args[0]]
    assert len(staged) == 1


async def test_in_progress_batch_retries_only_unfinished_paths(worker):
    worker.result.files = add_store(worker, "result", 2)
    calls = []

    async def resumable(url, **kwargs):
        paths = kwargs["json"]["relative_paths"]
        calls.append(paths)
        response = await uploaded_batch(url, **kwargs)
        body = response.json()
        if len(calls) == 1:
            body["results"][0] = {"relative_path": paths[0], "status": "in_progress"}
        return httpx.Response(200, json=body, request=httpx.Request("POST", url))

    worker.client.post.side_effect = resumable
    result = await publish(worker)
    assert len(result.uploaded) == 2 and not result.errors
    assert calls == [["result/0", "result/1"], ["result/0"]]


@pytest.mark.parametrize(
    "folder",
    [
        "../secret",
        "/tmp",
        "outputs/gcs/../outside",
        "outputs/other",
        "outputs/gcs/missing",
        "outputs/gcs/link",
    ],
)
async def test_tool_rejects_escaping_missing_or_symlink_folders(worker, folder):
    (worker.ctx.workdir / "outputs/gcs/link").symlink_to(
        worker.ctx.workdir, target_is_directory=True
    )
    result = await publisher(worker).tool(worker.ctx).handler({"folder": folder})
    assert result["isError"]
    worker.client.post.assert_not_called()


@pytest.mark.parametrize("granted", [True, False])
def test_publish_tool_is_registered_only_with_current_grant(worker, granted):
    from mica.agents.analysis.config import Settings
    from mica.agents.analysis.runner import build_options

    if not granted:
        worker.request["write_grant"] = None
    options = build_options(worker.ctx, Settings(_env_file=None), gcs_publisher=publisher(worker))
    assert (write_worker.TOOL_NAME in options.allowed_tools) is granted
    assert "mcp__pistachio__send_files" in options.allowed_tools
    assert options.mcp_servers  # Local packaging does not grant GCS publication.
    if granted:
        assert int(options.env["MCP_TOOL_TIMEOUT"]) >= 840_000


async def test_current_grant_is_passed_separately_on_each_resumed_turn(bot):  # noqa: F811
    from datetime import UTC, datetime

    await send(bot, event("Earlier there was no write grant"))
    bot.write_grant = write_access.WriteGrant(
        id="b" * 32,
        uri="gs://lab-data/results/",
        actor="alice@example.com",
        admin_id="T:U",
        created_at=datetime.now(UTC),
    )
    await send(
        bot,
        event(
            "Save the full result",
            ts="2.0",
            thread="1.0",
            _conversation_context=[{"assistant": "No GCS write grant for this run."}],
        ),
    )
    session, _, prompt, _ = bot.runner.calls[-1]
    assert session == "session"
    assert "No GCS write grant for this run." in prompt
    assert "gs://lab-data/results/" not in prompt
    assert "gs://lab-data/results/" in bot.runner.write_access
    assert "GCS write access for this turn" not in prompt
    bot.write_grant = None
    await send(bot, event("Try again", ts="3.0", thread="1.0"))
    assert "No GCS write grant for this run." in bot.runner.write_access
    assert "GCS write access" not in bot.runner.calls[-1][2]


async def test_upload_mutex_outlives_streaming_deadline(writable, monkeypatch):
    compute, request, _, body = await worker_request(writable, monkeypatch)
    monkeypatch.setattr(
        slack_analysis.write_broker,
        "upload",
        Mock(return_value={"uri": "gs://ok", "status": "uploaded"}),
    )
    begin = AsyncMock(wraps=writable.repository.begin_event)
    monkeypatch.setattr(writable.repository, "begin_event", begin)
    await slack_analysis.write_output(compute, body, request, Response())
    assert begin.call_args.kwargs["lease_seconds"] == 900


async def test_revocation_stops_remaining_store_files(worker):
    import asyncio

    worker.result.files = add_store(worker, "store", 32)
    started = 0

    async def deny(url, **kwargs):
        nonlocal started
        started += 1
        await asyncio.sleep(0.01)
        return httpx.Response(403, json={"detail": "Write access revoked."})

    worker.client.post.side_effect = deny
    outcome = await publish(worker)
    assert not outcome.uploaded and outcome.errors
    assert 1 <= started <= write_worker.CONCURRENCY
    assert (
        len(worker.store.keys("tasks/slack-analysis/events/" + "a" * 32 + "/write-exports"))
        <= write_broker.BATCH_SIZE
    )


async def test_complete_folder_rejects_symlinks_before_publishing_any_part(worker):
    paths = add_store(worker, "store")
    paths[0].with_name("link").symlink_to(worker.path)
    outcome = await publisher(worker).publish(worker.ctx, TurnResult(), folder="outputs/gcs/store")
    assert outcome.errors and not outcome.uploaded
    worker.client.post.assert_not_called()


async def test_worker_manifest_carries_only_broker_request_time_slice(writable):
    await allow(writable)
    compute = await writable.accept(payload("3.0"))
    await writable.process(compute, 0)
    manifest = json.loads(
        writable.store.get(f"tasks/slack-analysis/events/{compute}/request.json")[0]
    )
    assert manifest["write_upload_seconds"] == 840
    assert not any(k.startswith("write_max_") for k in manifest)


async def test_large_publish_has_compact_tool_and_slack_receipts_and_full_manifests(worker, bot):  # noqa: F811
    paths = add_store(worker, "resegmentation/tma15_atomx_prior/tables/proseg.zarr", 1073)

    async def uploaded(url, **kwargs):
        return await uploaded_batch(url, **kwargs)

    worker.client.post.side_effect = uploaded
    service = publisher(worker)
    response = await service.tool(worker.ctx).handler(
        {"folder": "outputs/gcs/resegmentation/tma15_atomx_prior"}
    )
    text = response["content"][0]["text"]
    assert len(text) < 2000 and not response["isError"]
    summary = json.loads(text)
    assert summary["files"] == 1073 and summary["bytes"] == 1073 * 5 and summary["failed"] == 0
    assert summary["destinations"] == [
        {
            "uri": "gs://lab-data/resegmentation/tma15_atomx_prior/",
            "files": 1073,
            "bytes": 5365,
            "errors": 0,
        }
    ]
    manifest = json.loads((worker.ctx.workdir / summary["manifest"]).read_text())
    assert manifest["files"] == 1073 and manifest["shards"]
    final = await publisher(worker).publish(worker.ctx, TurnResult(files=paths))
    assert worker.client.post.call_count == 3  # Final receipt does not re-upload the store.
    assert final.destinations == [
        write_worker.Destination(
            "gs://lab-data/resegmentation/tma15_atomx_prior/", files=1073, bytes=5365
        )
    ]
    assert not worker.store.keys("write-access/uploads")
    write_broker.archive_manifests(worker.store, "a" * 32, "gs://lab-data/")
    index = json.loads(worker.store.get("write-access/uploads/" + "a" * 32 + "/manifest.json")[0])
    assert index["destinations"][0]["files"] == 1073 and len(index["publications"]) == 2
    assert all(json.loads(worker.store.get(k)[0])["files"] == 1073 for k in index["publications"])
    bot.publish_gcs = AsyncMock(return_value=final)
    await send(bot, event("Save checked result store"))
    assert bot.last_reply.startswith("Data: fixture.h5ad")
    assert bot.last_reply.count("Saved to GCS:") == 1 and "1,073 files" in bot.last_reply
    assert "proseg.zarr/" not in bot.last_reply and len(bot.last_reply) < 500
    assert all(
        "logs/" not in f["file"]
        for f in bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
    )


async def test_failed_files_have_counts_and_bounded_examples(worker, bot):  # noqa: F811
    worker.result.files = add_store(worker, "result", 10)

    async def refused(url, **kwargs):
        return httpx.Response(
            200,
            json={
                "results": [
                    {"relative_path": p, "status": "failed", "error": "Destination exists."}
                    for p in kwargs["json"]["relative_paths"]
                ]
            },
            request=httpx.Request("POST", url),
        )

    worker.client.post.side_effect = refused
    service = publisher(worker)
    response = await service.tool(worker.ctx).handler({"folder": "outputs/gcs/result"})
    summary = json.loads(response["content"][0]["text"])
    assert response["isError"] and summary["failed"] == 10
    assert len(summary["error_examples"]) == 5
    assert summary["destinations"][0]["errors"] == 10
    manifest = json.loads((worker.ctx.workdir / summary["manifest"]).read_text())
    assert manifest["failed"] == 10 and len(manifest["error_examples"]) == 5
    outcome = await service.publish(worker.ctx, worker.result)
    bot.publish_gcs = AsyncMock(return_value=outcome)
    await send(bot, event("Save the results"))
    assert bot.last_reply.startswith("Data: fixture.h5ad")
    assert "10 failed" in bot.last_reply and bot.last_reply.count("Destination exists.") == 3
    assert "Saved to GCS" not in bot.last_reply and bot.last_error


async def test_worker_manifest_uses_only_event_transport_permissions(worker, monkeypatch):
    put, put_if = worker.store.put, worker.store.put_if

    def worker_put(key, *args):
        if key.startswith("write-access/"):
            raise PermissionError("Controller only")
        return put(key, *args)

    def worker_put_if(key, *args):
        if key.startswith("write-access/"):
            raise PermissionError("Controller only")
        return put_if(key, *args)

    monkeypatch.setattr(worker.store, "put", worker_put)
    monkeypatch.setattr(worker.store, "put_if", worker_put_if)
    report = await publish(worker)
    assert not report.errors and report.uploaded
    index = json.loads(worker.store.get(write_broker.manifest_stage("a" * 32))[0])
    assert len(index["publications"]) == 1


async def test_controller_archives_reports_during_poll(writable):
    await allow(writable)
    event_id = await writable.accept(payload("3.0"))
    await writable.process(event_id, 0)
    identifier = "c" * 32
    prefix = f"write-access/uploads/{event_id}/"
    key = prefix + f"publications/{identifier}/manifest.json"
    report = {"event_id": event_id, "uploaded": ["gs://lab-data/liver/output/report.csv"]}
    writable.store.put(
        write_broker.manifest_stage(event_id, identifier), json.dumps(report).encode()
    )
    index = {**report, "publications": [key]}
    writable.store.put(write_broker.manifest_stage(event_id), json.dumps(index).encode())
    assert await writable.process(event_id, 1) == "running"
    assert json.loads(writable.store.get(key)[0]) == report
    assert json.loads(writable.store.get(prefix + "manifest.json")[0]) == index
    assert await writable.process(event_id, 2) == "running"  # Idempotent next poll.


@pytest.mark.parametrize("problem", ["foreign-event", "foreign-object", "foreign-key", "mutation"])
def test_controller_manifest_archive_rejects_invalid_reports(worker, problem):
    event_id, identifier = "a" * 32, "c" * 32
    key = f"write-access/uploads/{event_id}/publications/{identifier}/manifest.json"
    report = {"event_id": event_id, "uploaded": ["gs://lab-data/result.csv"]}
    index = {**report, "publications": [key]}
    if problem == "foreign-event":
        report["event_id"] = "b" * 32
    elif problem == "foreign-object":
        report["uploaded"] = ["gs://other-bucket/private.csv"]
    elif problem == "foreign-key":
        index["publications"] = ["write-access/projects/private.json"]
    else:
        worker.store.put(key, b"earlier immutable report")
    worker.store.put(write_broker.manifest_stage(event_id, identifier), json.dumps(report).encode())
    worker.store.put(write_broker.manifest_stage(event_id), json.dumps(index).encode())
    with pytest.raises(ValueError):
        write_broker.archive_manifests(worker.store, event_id, "gs://lab-data/")
    assert not worker.store.path(f"write-access/uploads/{event_id}/manifest.json").exists()
