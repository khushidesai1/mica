import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from analysis_helpers import seed_project
from test_analysis_batch import bridge, payload  # noqa: F401

from pistachio.agents.analysis import batch, direct


@pytest.fixture
def chat(bridge, monkeypatch):  # noqa: F811
    bridge.settings.storage = "local"
    store = batch.GCSStorage(bridge.app.gcs_bucket)
    pid = seed_project(store)
    bridge.test_slack.conversations_replies.return_value = {"messages": []}
    bridge.decision = AsyncMock(
        return_value=direct.Decision(
            route="reply", reply="Updated.", guide="# Project\nSpecies: human (user provided)"
        )
    )
    monkeypatch.setattr(direct, "decide", bridge.decision)
    bridge.store, bridge.pid = store, pid
    return bridge


async def test_direct_edits_save_and_followups_never_create_workers(chat, monkeypatch):
    from pistachio.agents.analysis import job_lifecycle, memory_tasks

    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    monkeypatch.setattr(
        memory_tasks,
        "edit",
        AsyncMock(return_value={"PROJECT.md": "# Project\nSpecies: human (Alice provided)"}),
    )
    first = await chat.accept(payload(text="These are human samples"))
    assert await chat.process(first, 0) == "complete"
    assert await chat.process(first, 0) == "complete"
    chat.decision.assert_awaited_once()
    record = await chat.repository.get_connector_state("claude-event:" + first)
    pointer = f"projects/_threads/{record['thread_key']}/checkpoint.json"
    state = json.loads(chat.store.get(pointer)[0])
    assert "TODO" in state["context"]["project_md"]
    await memory_tasks.process(chat, first, chat.store)
    assert "human" in (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"].text
    for i, command in enumerate(["brief", "save brief", "help", "cancel"], start=2):
        event = await chat.accept(payload(f"{i}.0", "1.0", command))
        assert await chat.process(event, 0) == "complete"
    assert "human" in (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"].text
    assert chat.decision.await_count == 1
    chat.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_compute_uses_direct_brief_and_cached_routing(chat, monkeypatch):
    from pistachio.agents.analysis import job_lifecycle, memory_tasks

    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    monkeypatch.setattr(
        memory_tasks,
        "edit",
        AsyncMock(return_value={"PROJECT.md": "# Project\nSpecies: human (Alice provided)"}),
    )
    first = await chat.accept(payload(text="These are human samples"))
    await chat.process(first, 0)
    await memory_tasks.process(chat, first, chat.store)
    chat.decision.return_value = direct.Decision(
        route="compute", reply="Plotting needs data access."
    )
    compute = await chat.accept(payload("2.0", "1.0", "Plot the spatial layout"))
    assert await chat.process(compute, 0) == "running"
    assert await chat.process(compute, 1) == "running"
    assert chat.test_client.create_job.call_count == 1
    assert chat.decision.await_count == 2
    supplied = chat.decision.call_args.args[2]
    assert "human" in supplied["brief"]
    assert supplied["recent_messages"][0]["user"] == "These are human samples"
    record = await chat.repository.get_connector_state("claude-event:" + compute)
    assert record["route"] == "compute" and record["mount_buckets"] == ["lab-data"]


async def test_direct_init_and_invalid_bucket(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", lambda *a: {"entries": [], "content_inspected": False})
    init = await chat.accept(payload(text="init liver gs://lab-data/liver/"))
    assert await chat.process(init, 0) == "complete"
    assert "init_inventory" in chat.decision.call_args.args[2]
    chat.test_client.create_job.assert_not_called()
    bad = await chat.accept(payload("2.0", text="init other gs://outside-project/"))
    assert await chat.process(bad, 0) == "failed"
    assert chat.decision.await_count == 1
    chat.test_client.create_job.assert_not_called()


async def test_direct_failure_does_not_fall_back_to_compute(chat):
    chat.decision.side_effect = httpx.ReadTimeout("timeout")
    event = await chat.accept(payload(text="Please revise my brief"))
    assert await chat.process(event, 0) == "failed"
    chat.test_client.create_job.assert_not_called()
    assert "TODO" in (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"].text


async def test_file_listing_uses_project_metadata_without_worker_and_caches_reply(
    chat, monkeypatch
):
    metadata = {
        "entries": [{"path": "liver/raw/sample.csv.gz", "kind": "file", "size_bytes": 4096}],
        "coverage": "bounded metadata sample, not a complete inventory",
        "content_inspected": False,
    }
    listing = Mock(return_value=metadata)
    monkeypatch.setattr(direct, "inventory", listing)
    chat.decision.side_effect = [
        direct.Decision(route="inventory", reply="Checking.", inventory_path="raw/"),
        direct.Decision(route="reply", reply="I found sample.csv.gz (4 KB) in the sampled list."),
    ]
    event = await chat.accept(payload(text="What files are in raw, and how big are they?"))
    assert await chat.process(event, 0) == "complete"
    assert await chat.process(event, 0) == "complete"
    listing.assert_called_once()
    assert listing.call_args.args[0] == "gs://lab-data/liver/raw/"
    assert chat.decision.await_count == 2
    supplied = chat.decision.call_args.args[2]
    assert supplied["data_inventory"] == metadata
    assert supplied["data_uri"] == "gs://lab-data/liver/"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert record["direct_decision"]["route"] == "reply"
    assert "4 KB" in record["notice_text"]
    chat.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


@pytest.mark.parametrize("route", ["reply", "compute", "inventory"])
async def test_unavailable_listing_never_falls_back_to_worker(chat, monkeypatch, route):
    monkeypatch.setattr(
        direct, "inventory", Mock(return_value={"entries": [], "stopped": "time budget"})
    )
    chat.decision.side_effect = [
        direct.Decision(route="inventory", reply="Checking."),
        direct.Decision(route=route, reply="The file listing timed out. Please try again."),
    ]
    event = await chat.accept(payload(text="What files are in the bucket?"))
    expected = "complete" if route == "reply" else "failed"
    assert await chat.process(event, 0) == expected
    chat.test_client.create_job.assert_not_called()


async def test_inventory_reply_failure_never_falls_back_to_worker(chat, monkeypatch):
    monkeypatch.setattr(direct, "inventory", Mock(return_value={"entries": []}))
    chat.decision.side_effect = [
        direct.Decision(route="inventory", reply="Checking."),
        httpx.ReadTimeout("timeout"),
    ]
    event = await chat.accept(payload(text="What files are in the bucket?"))
    assert await chat.process(event, 0) == "failed"
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("path", ["../other/", "gs://other-bucket/", "/liver/"])
async def test_inventory_cannot_escape_project(chat, monkeypatch, path):
    listing = Mock()
    monkeypatch.setattr(direct, "inventory", listing)
    chat.decision.return_value = direct.Decision(
        route="inventory", reply="Checking.", inventory_path=path
    )
    event = await chat.accept(payload(text="List files"))
    assert await chat.process(event, 0) == "failed"
    listing.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_inventory_checks_bucket_admission_before_listing(chat, monkeypatch):
    listing = Mock()
    monkeypatch.setattr(direct, "inventory", listing)
    chat.settings.data_buckets = "another-bucket"
    chat.decision.return_value = direct.Decision(route="inventory", reply="Checking.")
    event = await chat.accept(payload(text="List files"))
    assert await chat.process(event, 0) == "failed"
    listing.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_inventory_rechecks_project_access_before_listing(chat, monkeypatch):
    from pistachio.agents.analysis.registry import Registry

    listing = Mock()
    monkeypatch.setattr(direct, "inventory", listing)

    async def switch_project(*args, **kwargs):
        def switch(doc):
            doc.bind("T1", "D1", "alice@example.com", other, private=True, force=True)
            return other

        Registry(chat.store).update("switch-project", "alice@example.com", "switch", switch)
        return direct.Decision(route="inventory", reply="Checking.")

    other = seed_project(chat.store, name="other", channel="D2", uri="gs://lab-data/other/")
    chat.decision.side_effect = switch_project
    event = await chat.accept(payload(text="List files"))
    assert await chat.process(event, 0) == "complete"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert "Project access changed" in record["notice_text"]
    listing.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_inventory_delivery_retry_reuses_saved_result(chat, monkeypatch):
    listing = Mock(return_value={"entries": []})
    monkeypatch.setattr(direct, "inventory", listing)
    chat.decision.side_effect = [
        direct.Decision(route="inventory", reply="Checking.", guide="Revised draft"),
        direct.Decision(route="reply", reply="The sampled folder is empty."),
    ]

    async def unavailable(**kwargs):
        if kwargs["text"] == "The sampled folder is empty.":
            raise RuntimeError("Slack unavailable")
        return {"ts": kwargs["ts"]}

    chat.test_slack.chat_update.side_effect = unavailable
    event = await chat.accept(payload(text="List files"))
    with pytest.raises(RuntimeError, match="delivery failed"):
        await chat.process(event, 0)
    chat.test_slack.chat_update.side_effect = None
    assert await chat.process(event, 0) == "complete"
    listing.assert_called_once()
    assert chat.decision.await_count == 2
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert record["direct_decision"]["guide"] == "Revised draft"
    chat.test_client.create_job.assert_not_called()


async def test_retry_after_state_write_reuses_decision(chat, monkeypatch):
    original = chat.store.put
    failed = False

    def put(key, data):
        nonlocal failed
        original(key, data)
        if key.endswith("checkpoint.json") and not failed:
            failed = True
            raise RuntimeError("response lost")

    monkeypatch.setattr(chat.store, "put", put)
    event = await chat.accept(payload(text="These are human samples"))
    with pytest.raises(RuntimeError, match="response lost"):
        await chat.process(event, 0)
    assert await chat.process(event, 0) == "complete"
    chat.decision.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_cancel_bypasses_queue_and_releases_slot_only_after_job_ends(chat):
    chat.decision.return_value = direct.Decision(route="compute", reply="Needs computation.")
    first = await chat.accept(payload(text="Plot data"))
    assert await chat.process(first, 0) == "running"
    queued = await chat.accept(payload("2.0", "1.0", "Change the plot colors"))
    cancel = await chat.accept(payload("3.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    chat.test_client.delete_job.assert_called_once()
    assert (await chat.repository.get_connector_state("claude-event:" + queued))[
        "status"
    ] == "failed"
    assert any(k.startswith("claude-slot") for k in chat.repository.event_claims)
    del chat.test_states[next(iter(chat.test_states))]
    assert await chat.process(first, 1) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    chat.test_client.create_job.assert_called_once()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_read_only_brief_ignores_attachment_without_download(chat):
    body = payload(text="brief")
    body["event"]["files"] = [
        {"name": "PROJECT.md", "url_private_download": "https://evil.example/x"}
    ]
    event = await chat.accept(body)
    assert await chat.process(event, 0) == "complete"
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_router_rejects_truncated_output(monkeypatch):
    def respond(request):
        return httpx.Response(200, json={"stop_reason": "max_tokens", "content": []})

    transport = httpx.MockTransport(respond)
    client = httpx.AsyncClient(transport=transport)
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    with pytest.raises(ValueError, match="too long"):
        await direct.decide("fake-key", "test-model", {"request": "hi"})


async def test_reply_delivery_retry_does_not_repeat_model_or_create_worker(chat):
    original = chat.test_slack.chat_update
    failed = False

    async def update(**kwargs):
        nonlocal failed
        if kwargs["text"] == "Updated." and not failed:
            failed = True
            raise RuntimeError("Slack unavailable")
        return {"ts": kwargs["ts"]}

    original.side_effect = update
    event = await chat.accept(payload(text="These are human samples"))
    with pytest.raises(RuntimeError, match="delivery failed"):
        await chat.process(event, 0)
    assert await chat.process(event, 0) == "complete"
    chat.decision.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_cancel_during_interrupted_submission_never_submits_later(chat):
    chat.decision.return_value = direct.Decision(route="compute", reply="Needs computation.")
    chat.test_client.create_job.side_effect = RuntimeError("submission interrupted")
    first = await chat.accept(payload(text="Plot data"))
    with pytest.raises(RuntimeError, match="submission interrupted"):
        await chat.process(first, 0)
    cancel = await chat.accept(payload("2.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    assert await chat.process(first, 0) == "failed"
    assert await chat.process(cancel, 1) == "complete"
    chat.test_client.create_job.assert_called_once()


@pytest.fixture
def upload_http(monkeypatch):
    from test_analysis_attachments import PNG, mock_download

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=PNG if request.url.path.endswith("png") else b"a,b\n1,2")

    mock_download(monkeypatch, respond)
    return calls


def attached(name="plot.png", **kwargs):
    return {
        "id": "F1",
        "name": name,
        "size": 10,
        "url_private_download": "https://files.slack.com/" + name,
        **kwargs,
    }


async def attachment_message(
    chat, files, text="what's wrong with this plot?", ts="1.0", thread=None
):
    body = payload(ts, thread, text)
    body["event"]["files"] = files
    return await chat.accept(body)


async def thread_state(chat, event):
    record = await chat.repository.get_connector_state("claude-event:" + event)
    return record, json.loads(
        chat.store.get(f"projects/_threads/{record['thread_key']}/checkpoint.json")[0]
    )


async def test_image_question_answered_without_worker(chat, upload_http):
    event = await attachment_message(chat, [attached()])
    assert await chat.process(event, 0) == "complete"
    record, state = await thread_state(chat, event)
    media = chat.decision.call_args.kwargs["media"]
    assert media[1]["type"] == "image"
    file = state["thread_files"][0]
    assert media[0]["text"] == f"Attachment {file['id']}: plot.png"
    assert file["key"].startswith("tasks/slack-analysis/uploads/_users/")
    assert file["user"] == "U1"
    assert state["messages"][-1]["files"] == ["plot.png"]
    assert record["attachments"][0]["id"] == file["id"]
    assert chat.decision.call_args.args[2]["attachments"][0]["shown"]
    chat.test_client.create_job.assert_not_called()
    assert len(upload_http) == 1


async def test_csv_compute_reaches_manifest(chat, upload_http):
    chat.decision.return_value = direct.Decision(route="compute", reply="Plotting the data.")
    event = await attachment_message(chat, [attached("data.csv")], "plot B against A")
    assert await chat.process(event, 0) == "running"
    record, state = await thread_state(chat, event)
    manifest = json.loads(chat.store.get(f"tasks/slack-analysis/events/{event}/request.json")[0])
    assert manifest["attachments"] == [state["thread_files"][0]["id"]]
    listing = chat.decision.call_args.args[2]["attachments"][0]
    assert listing["head"] == "a,b\n1,2"
    assert "slack_bot_token" not in json.dumps(manifest).lower()


async def test_denied_user_files_never_downloaded(chat, upload_http):
    from pistachio.agents.analysis.registry import Registry

    def revoke(doc):
        doc.projects[chat.pid].members = set()
        doc.projects[chat.pid].owner = "other@example.com"
        return "revoked"

    Registry(chat.store).update("revoke", "alice@example.com", "revoke", revoke)
    event = await attachment_message(chat, [attached()])
    assert await chat.process(event, 0) == "complete"
    assert not upload_http
    chat.decision.assert_not_called()


@pytest.mark.parametrize("command", ["init new gs://lab-data/new/", "save brief", "brief"])
async def test_attachment_command_rejections(chat, upload_http, command):
    event = await attachment_message(chat, [attached()], command)
    assert await chat.process(event, 0) == ("failed" if command.startswith("init") else "complete")
    assert not upload_http
    chat.test_client.create_job.assert_not_called()


async def test_brief_is_read_only_even_with_uploaded_guide(chat, upload_http):
    before = await chat.memory_backend.read(chat.pid)
    event = await attachment_message(chat, [attached("PROJECT.md")], "brief")
    assert await chat.process(event, 0) == "complete"
    assert await chat.memory_backend.read(chat.pid) == before
    assert not upload_http
    chat.decision.assert_not_called()


async def test_retry_after_router_failure_does_not_redownload(chat, upload_http):
    chat.decision.side_effect = RuntimeError("interrupted router")
    event = await attachment_message(chat, [attached()])
    with pytest.raises(RuntimeError, match="interrupted router"):
        await chat.process(event, 0)
    chat.decision.side_effect = None
    assert await chat.process(event, 0) == "complete"
    record, state = await thread_state(chat, event)
    assert len(upload_http) == len(state["thread_files"]) == 1


async def test_oversized_upload_rejected_with_cap_message(chat, upload_http):
    event = await attachment_message(chat, [attached(size=150_000_000)])
    assert await chat.process(event, 0) == "failed"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert "100 MB" in record["notice_text"] and "GCS" in record["notice_text"]
    assert not upload_http


async def test_router_400_retries_without_media(monkeypatch):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(400, json={"error": "invalid image"})
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "route_request",
                        "input": {"route": "reply", "reply": "Please re-upload the image."},
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    result = await direct.decide(
        "fake",
        "model",
        {"attachments": [{"id": "f1", "shown": True}]},
        media=[{"type": "image", "source": {}}],
    )
    assert result.route == "reply" and len(requests) == 2
    content = requests[1]["messages"][0]["content"]
    context = json.loads(content[0]["text"])
    assert len(content) == 1 and "only metadata" in context["attachments_note"]
    assert not context["attachments"][0]["shown"]


async def seed_earlier_output(chat):
    from test_analysis_attachments import file_record

    first = await chat.accept(payload(text="hello"))
    await chat.process(first, 0)
    record, state = await thread_state(chat, first)
    output = file_record(chat.store, id="o12345678", source="agent")
    state["thread_files"] = [output.model_dump()]
    chat.store.put(
        f"projects/_threads/{record['thread_key']}/checkpoint.json", json.dumps(state).encode()
    )
    return output


async def test_view_earlier_output_then_reply_without_worker(chat):
    output = await seed_earlier_output(chat)
    chat.decision.reset_mock()
    chat.decision.side_effect = [
        direct.Decision(route="view", view_ids=[output.id], reply="Looking", guide="Kept draft"),
        direct.Decision(route="reply", reply="The plot shows a red square."),
    ]
    event = await chat.accept(payload("2.0", "1.0", "What does the figure you sent show?"))
    assert await chat.process(event, 0) == "complete"
    first, second = chat.decision.call_args_list
    assert not first.kwargs.get("media")
    assert second.kwargs["media"][0]["text"].startswith("Attachment o12345678:")
    assert second.kwargs["media"][1]["type"] == "image"
    record, state = await thread_state(chat, event)
    assert state["context"]["project_md"] == "# Project\nSpecies: TODO"
    chat.test_client.create_job.assert_not_called()


async def test_view_ignores_foreign_and_already_shown_ids(chat, upload_http, monkeypatch):
    from pistachio.agents.analysis import attachments

    actual = attachments.router_media
    calls = []

    def media(storage, files):
        calls.append([f.id for f in files])
        return actual(storage, files)

    monkeypatch.setattr(attachments, "router_media", media)

    async def decide(*args, **kwargs):
        if len(calls) == 1:
            id = args[2]["attachments"][0]["id"]
            return direct.Decision(route="view", view_ids=[id, "foreign-file"], reply="Looking")
        return direct.Decision(route="reply", reply="That's the image you sent.")

    chat.decision.side_effect = decide
    event = await attachment_message(chat, [attached()])
    assert await chat.process(event, 0) == "complete"
    assert calls[1] == []  # No new reads; current media can be reused from the first pass.


async def test_view_rechecks_access(chat, monkeypatch):
    output = await seed_earlier_output(chat)
    from pistachio.agents.analysis import attachments

    media = Mock(wraps=attachments.router_media)
    monkeypatch.setattr(attachments, "router_media", media)

    async def forget(*args, **kwargs):
        from pistachio.agents.analysis.registry import Registry

        def alter(doc):
            doc.projects[chat.pid].access_revision += 1
            return "changed"

        Registry(chat.store).update("changed", "alice@example.com", "changed", alter)
        return direct.Decision(route="view", view_ids=[output.id], reply="Looking")

    chat.decision.side_effect = forget
    event = await chat.accept(payload("2.0", "1.0", "What did you send?"))
    assert await chat.process(event, 0) == "complete"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert "Project access changed" in record["notice_text"]
    assert media.call_count == 1 and media.call_args.args[1] == []


async def test_second_view_rejected(chat):
    output = await seed_earlier_output(chat)
    chat.decision.return_value = direct.Decision(route="view", view_ids=[output.id], reply="Look")
    event = await chat.accept(payload("2.0", "1.0", "What did you send?"))
    assert await chat.process(event, 0) == "failed"
    chat.test_client.create_job.assert_not_called()


async def test_upload_rejection_lists_each_problem(chat, upload_http):
    event = await attachment_message(
        chat,
        [
            attached("external.csv", mode="external"),
            attached("large.csv", size=150_000_000),
        ],
    )
    assert await chat.process(event, 0) == "failed"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert "external.csv" in record["notice_text"]
    assert "large.csv" in record["notice_text"] and "GCS" in record["notice_text"]
    assert not upload_http


@pytest.mark.parametrize("archive", [False, True])
async def test_router_retries_missing_tool_and_logs_only_metadata(monkeypatch, caplog, archive):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "private patient text"}],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    if archive:
        result = await direct.decide("fake", "model", {}, event_id="event-test", has_archive=True)
        assert result.route == "compute"
    else:
        with pytest.raises(ValueError, match="Couldn't determine the next step"):
            await direct.decide("fake", "model", {}, event_id="event-test")
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]["tool_choice"] == {
        "type": "auto",
        "disable_parallel_tool_use": True,
    }
    assert "thinking" not in requests[0]
    assert "event-test" in caplog.text and "end_turn" in caplog.text
    assert "text" in caplog.text and "private patient text" not in caplog.text


async def test_router_uses_valid_second_decision(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        content = (
            [{"type": "text", "text": "no tool"}]
            if len(calls) == 1
            else [
                {
                    "type": "tool_use",
                    "name": "route_request",
                    "input": {"route": "reply", "reply": "The denominator is AtoMx export cells."},
                }
            ]
        )
        return httpx.Response(200, json={"content": content})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(direct.httpx, "AsyncClient", lambda **kw: client)
    result = await direct.decide("fake", "model", {})
    assert result.reply == "The denominator is AtoMx export cells." and len(calls) == 2


async def test_results_followup_uses_prior_methods_without_worker(chat):
    first = await chat.accept(payload(text="Compare the counts"))
    await chat.process(first, 0)
    record, state = await thread_state(chat, first)
    # Old messages without methods still load alongside the new optional note.
    state["messages"][0].pop("methods", None)
    state["archive"] = "existing/checkpoint.tar.gz"
    direct.record_exchange(
        state,
        "earlier",
        "Counts?",
        "AtoMx CellPose cell count",
        methods=("Panel 1 (cells kept): Proseg cells / AtoMx export cells; OK = >=80%."),
    )
    chat.store.put(
        f"projects/_threads/{record['thread_key']}/checkpoint.json", json.dumps(state).encode()
    )
    chat.decision.return_value = direct.Decision(
        route="reply", reply="The denominator is the AtoMx export, not CellPose."
    )
    followup = await chat.accept(
        payload(
            "2.0",
            "1.0",
            (
                "Is the first one a percentage of the AtomX or CellPose? you wrote AtomX CellPose "
                "but theyre two different ones"
            ),
        )
    )
    assert await chat.process(followup, 0) == "complete"
    context = chat.decision.call_args.args[2]
    assert "AtoMx export" in context["recent_messages"][-1]["methods"]
    assert chat.decision.call_args.kwargs["has_archive"] is True
    assert chat.decision.call_args.kwargs["event_id"] == followup
    _, saved = await thread_state(chat, followup)
    assert saved["messages"][-1]["assistant"] == chat.decision.return_value.reply
    assert saved["messages"][-1]["methods"] == ""
    chat.test_client.create_job.assert_not_called()


def test_followup_and_methods_prompt_rules():
    from pistachio.agents.analysis import prompts

    assert "earlier reply or its methods note" in direct.SYSTEM
    assert "ask instead of guessing or launching a worker" not in direct.SYSTEM
    assert "notes/methods.md" in prompts.SYSTEM
