import json
from unittest.mock import AsyncMock

import httpx
import pytest
from analysis_helpers import conversation_mock, seed_project
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import batch, conversation, direct


@pytest.fixture
def chat(bridge, monkeypatch):  # noqa: F811
    bridge.settings.storage = "local"
    store = batch.GCSStorage(bridge.app.gcs_bucket)
    pid = seed_project(store)
    bridge.test_slack.conversations_replies.return_value = {"messages": []}
    bridge.decision = conversation_mock(
        conversation.Decision(
            route="reply", reply="Updated.", guide="# Project\nSpecies: human (user provided)"
        )
    )
    monkeypatch.setattr(conversation, "run", bridge.decision)
    bridge.store, bridge.pid = store, pid
    return bridge


async def test_direct_edits_save_and_followups_never_create_workers(chat, monkeypatch):
    from mica.agents.analysis import job_lifecycle, memory_tasks

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
    from mica.agents.analysis import job_lifecycle, memory_tasks

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
    chat.decision.return_value = conversation.Decision(
        route="compute", reply="Plotting needs data access."
    )
    compute = await chat.accept(payload("2.0", "1.0", "Plot the spatial layout"))
    assert await chat.process(compute, 0) == "running"
    assert await chat.process(compute, 1) == "running"
    assert chat.test_client.create_job.call_count == 1
    assert chat.decision.await_count == 2
    supplied = chat.decision.call_args.args[5]
    assert "human" in supplied["brief"]
    assert supplied["recent_messages"][0]["user"] == "These are human samples"
    record = await chat.repository.get_connector_state("claude-event:" + compute)
    assert record["route"] == "compute" and record["mount_buckets"] == ["lab-data"]


async def test_direct_init_and_invalid_bucket(chat, monkeypatch):
    monkeypatch.setattr(
        direct, "inventory", lambda *a, **kw: {"entries": [], "content_inspected": False}
    )
    init = await chat.accept(payload(text="init liver gs://lab-data/liver/"))
    assert await chat.process(init, 0) == "complete"
    assert "init_inventory" in chat.decision.call_args.args[5]
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
    chat.decision.return_value = conversation.Decision(route="compute", reply="Needs computation.")
    first = await chat.accept(payload(text="Plot data"))
    assert await chat.process(first, 0) == "running"
    queued = await chat.accept(payload("2.0", "1.0", "Change the plot colors"))
    cancel = await chat.accept(payload("3.0", "1.0", "cancel"))
    assert await chat.process(cancel, 0) == "pending"
    chat.test_client.delete_job.assert_not_called()
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
    chat.decision.assert_not_awaited()
    chat.test_client.create_job.assert_not_called()


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
    chat.decision.return_value = conversation.Decision(route="compute", reply="Needs computation.")
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
    assert chat.decision.call_args.args[5]["attachments"][0]["shown"]
    chat.test_client.create_job.assert_not_called()
    assert len(upload_http) == 1


async def test_csv_compute_reaches_manifest(chat, upload_http):
    chat.decision.return_value = conversation.Decision(route="compute", reply="Plotting the data.")
    event = await attachment_message(chat, [attached("data.csv")], "plot B against A")
    assert await chat.process(event, 0) == "running"
    record, state = await thread_state(chat, event)
    manifest = json.loads(chat.store.get(f"tasks/slack-analysis/events/{event}/request.json")[0])
    assert manifest["attachments"] == [state["thread_files"][0]["id"]]
    listing = chat.decision.call_args.args[5]["attachments"][0]
    assert listing["head"] == "a,b\n1,2"
    assert "slack_bot_token" not in json.dumps(manifest).lower()


async def test_denied_user_files_never_downloaded(chat, upload_http):
    from mica.agents.analysis.registry import Registry

    def revoke(doc):
        doc.projects[chat.pid].members = set()
        doc.projects[chat.pid].owner = "other@example.com"
        return "revoked"

    Registry(chat.store).update("revoke", "alice@example.com", "revoke", revoke)
    event = await attachment_message(chat, [attached()])
    assert await chat.process(event, 0) == "complete"
    assert not upload_http
    chat.decision.assert_awaited_once()
    assert chat.decision.call_args.args[5] == {"request": "what's wrong with this plot?"}


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
    chat.decision.assert_not_awaited()


@pytest.mark.parametrize("queued_retry", [False, True])
async def test_retry_after_router_failure_does_not_redownload(
    chat, upload_http, monkeypatch, queued_retry
):
    now = [100.0]
    monkeypatch.setattr(batch.time, "time", lambda: now[0])
    chat.decision.side_effect = (
        direct.RoutingUnavailable() if queued_retry else RuntimeError("interrupted router")
    )
    event = await attachment_message(chat, [attached()])
    if queued_retry:
        assert await chat.process(event, 0) == "pending"
    else:
        with pytest.raises(RuntimeError, match="interrupted router"):
            await chat.process(event, 0)
    chat.decision.side_effect = None
    now[0] = 130
    assert await chat.process(event, int(queued_retry)) == "complete"
    record, state = await thread_state(chat, event)
    assert len(upload_http) == len(state["thread_files"]) == 1


async def test_oversized_upload_rejected_with_cap_message(chat, upload_http):
    event = await attachment_message(chat, [attached(size=150_000_000)])
    assert await chat.process(event, 0) == "failed"
    record = await chat.repository.get_connector_state("claude-event:" + event)
    assert "100 MB" in record["notice_text"] and "GCS" in record["notice_text"]
    assert not upload_http


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
    chat.decision.return_value = conversation.Decision(
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
    context = chat.decision.call_args.args[5]
    assert "AtoMx export" in context["recent_messages"][-1]["methods"]
    assert chat.decision.call_args.args[1] == followup
    _, saved = await thread_state(chat, followup)
    assert saved["messages"][-1]["assistant"] == chat.decision.return_value.reply
    assert saved["messages"][-1]["methods"] == ""
    chat.test_client.create_job.assert_not_called()


async def test_imported_layout_is_supplied_in_router_context(chat):
    from tools.memory_import import layout_from_memory

    # Historical project without a guide; ordinary writers can no longer delete core files.
    chat.github.seed(
        {
            f"projects/{chat.pid}/PROJECT.md": None,
            f"projects/{chat.pid}/open.md": "type: open\nsource: analysis, 2026-10-11\n"
            "status: current\n\nAtoMx raw: `raw_outputs/raw/<TMA>/`",
        }
    )
    await layout_from_memory(
        chat.store,
        chat.memory_backend,
        chat.pid,
        ["open.md::AtoMx raw: `raw_outputs/raw/<TMA>/`"],
        apply=True,
    )
    event = await chat.accept(payload(text="Where are the raw data?"))
    assert await chat.process(event, 0) == "complete"
    context = chat.decision.call_args.args[5]
    assert "## Data" in context["project_md"]
    assert "raw_outputs/raw/<TMA>/" in context["project_md"]
