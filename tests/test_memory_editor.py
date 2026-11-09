"""Bounded editing, repair feedback, and recovery of previously rejected save inputs."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_memory_tasks import compact_edit

from mica.agents.analysis import job_lifecycle, memory_tasks, project_memory, thread_memory

OPEN = "type: open\nsource: Alice\nstatus: current\n\nUnrelated work must remain.\n"


def snapshot(files):
    return project_memory.Snapshot(
        head_oid="a" * 40,
        files={
            p: project_memory.File(text=t, oid=str(i) * 40)
            for i, (p, t) in enumerate(files.items())
        },
    )


def model_response(changes=None, thread=None, **extra):
    return {
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "name": "edit_project_memory",
                "input": {
                    "changes": changes or {},
                    "thread": thread or {"paraphrase": "A pending request."},
                },
            }
        ],
        **extra,
    }


def mock_model(monkeypatch, body, requests):
    factory = httpx.AsyncClient

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    monkeypatch.setattr(
        memory_tasks.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond)),
    )


def test_required_open_precedes_optional_files_and_never_truncates():
    files = {
        "PROJECT.md": "p" * 2893,
        "conventions.md": "c" * 180,
        "open.md": "o" * 10011,
        "lessons.md": "l" * 5917,
    }
    selected = memory_tasks.editor_files(snapshot(files), "pending work", 12000)
    assert selected["open.md"] == files["open.md"]
    assert "PROJECT.md" not in selected
    selected = memory_tasks.editor_files(snapshot(files), "pending work", 32000)
    assert selected == files
    with pytest.raises(memory_tasks.EditorError, match="context_limit"):
        memory_tasks.editor_files(snapshot(files), "pending work", 1000)


def test_repair_includes_required_topic_before_optional_core():
    files = {
        "open.md": OPEN,
        "PROJECT.md": "p" * 3000,
        "results/topic.md": "t" * 2000,
        "lessons.md": "l" * 2000,
    }
    selected = memory_tasks.editor_files(
        snapshot(files),
        "unrelated request",
        3200,
        required=("open.md", "results/topic.md"),
    )
    assert selected == {p: files[p] for p in ("open.md", "results/topic.md")}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (model_response({"open.md": "PRIVATE"}), "unseen_file"),
        (model_response({"archive/private.md": "PRIVATE"}), "unsupported_path"),
        (model_response(thread={"paraphrase": "PRIVATE", "unknown": "PRIVATE"}), "invalid_schema"),
        (model_response(stop_reason="max_tokens"), "incomplete_output"),
        ({"content": []}, "incomplete_output"),
        (
            {
                "content": [
                    {"type": "tool_use", "name": "edit_project_memory", "input": {"changes": {}}}
                ]
            },
            "missing_thread",
        ),
    ],
)
async def test_validation_reports_safe_reason_and_preserves_unseen_file_guard(
    monkeypatch,
    body,
    reason,
):
    requests = []
    mock_model(monkeypatch, body, requests)
    with pytest.raises(memory_tasks.EditorError) as error:
        await memory_tasks.edit(
            "test",
            "test",
            {
                "files": {},
                "existing_paths": ["open.md"],
                "thread": {"summary": ""},
            },
        )
    assert error.value.reason == reason
    assert "PRIVATE" not in str(error.value)
    schema = requests[0]["tools"][0]["input_schema"]
    assert schema["required"] == ["thread"]
    assert schema["properties"]["thread"] == {"$ref": "#/$defs/Prose"}


async def test_failed_turn_rejects_verified_result_and_only_allows_pending_work(monkeypatch):
    requests = []
    mock_model(monkeypatch, model_response({"results/new.md": "Unverified result"}), requests)
    with pytest.raises(memory_tasks.EditorError, match="failed_turn_path"):
        await memory_tasks.edit("test", "test", {"files": {}, "success": False})
    schema = requests[0]["tools"][0]["input_schema"]
    assert schema["properties"]["changes"]["propertyNames"] == {"enum": ["open.md"]}


async def start(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    event = await chat.accept(payload(text="We need to follow up this pending request"))
    await chat.process(event, 0)
    record = await chat.repository.get_connector_state("claude-event:" + event)
    key = f"projects/_threads/{record['thread_key']}/pending/{event}/memory-editor.json"
    return event, record, key


async def test_old_cache_recovers_against_current_files_preserving_original_turn(chat, monkeypatch):
    event, record, key = await start(chat, monkeypatch)
    old = await chat.memory_backend.read(chat.pid)
    await chat.memory_backend.commit(
        chat.pid,
        old,
        {"open.md": OPEN},
        source="editor",
        event_id="other-event",
    )
    latest = await chat.memory_backend.read(chat.pid)
    original = {
        "user": "Original request",
        "reply": "Original reply",
        "date": "2026-10-01",
        "speaker": "alice@example.com",
        "success": True,
        "recent_messages": [],
    }
    chat.store.put(
        key,
        json.dumps(
            {
                "base": old.model_dump(),
                "context": {
                    **original,
                    "files": {},
                    "existing_paths": [],
                    "thread": {"summary": "", "entries": [], "covered": ["stale"]},
                },
                "source": "editor",
                "covered": ["stale"],
            }
        ).encode(),
    )

    async def edit(_key, _model, context):
        assert all(context[k] == v for k, v in original.items())
        assert context["files"]["open.md"] == OPEN
        assert context["thread"]["covered"] == []
        return {**compact_edit(), "open.md": OPEN + "Original request is still pending.\n"}

    editor = AsyncMock(side_effect=edit)
    monkeypatch.setattr(memory_tasks, "edit", editor)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    editor.assert_awaited_once()
    cached = json.loads(chat.store.get(key)[0])
    assert cached["base"]["head_oid"] == latest.head_oid
    assert cached["editor_original_head"] == old.head_oid
    saved = await chat.memory_backend.read(chat.pid)
    assert saved.files["open.md"].text.startswith(OPEN)
    assert not any(p.startswith("_unmerged/") for p in saved.files)
    rows = thread_memory.log_entries(
        {p: f.text for p, f in saved.files.items()}, record["thread_key"]
    )
    assert [r.event_id for r in rows] == [event]
    assert (await chat.repository.get_connector_state("claude-event:" + event))[
        "memory_editor_done"
    ]
    assert (
        json.loads(chat.store.get(key.replace("memory-editor.json", "receipt.json"))[0])["event_id"]
        == event
    )


async def test_unseen_topic_feedback_is_durable_and_repairs_next_attempt(chat, monkeypatch):
    event, _record, key = await start(chat, monkeypatch)
    base = await chat.memory_backend.read(chat.pid)
    await chat.memory_backend.commit(
        chat.pid,
        base,
        {
            "open.md": OPEN,
            "PROJECT.md": "Guide " * 200,
            "results/topic.md": "type: result\nsource: Alice\nstatus: current\n\n" + "t" * 500,
        },
        source="editor",
        event_id="other-event",
    )
    # The initial cache is made after this update; the guide leaves too little room for topic.
    chat.settings.memory_editor_chars = 1400
    calls = []

    async def edit(_key, _model, context):
        calls.append(json.loads(json.dumps(context)))
        if len(calls) == 1:
            assert "results/topic.md" not in context["files"]
            raise memory_tasks.EditorError("unseen_file", ("results/topic.md",))
        assert context["editor_feedback"] == {"reason": "unseen_file"}
        assert "results/topic.md" in context["files"]
        return compact_edit()

    monkeypatch.setattr(memory_tasks, "edit", edit)
    with pytest.raises(memory_tasks.EditorError, match="unseen_file"):
        await memory_tasks.process(chat, event, chat.store)
    assert json.loads(chat.store.get(key)[0])["editor_required"] == ["results/topic.md"]
    await memory_tasks.process(chat, event, chat.store)
    assert len(calls) == 2


async def test_model_calls_are_durably_bounded_and_cooldown_does_not_report_again(
    chat, monkeypatch
):
    from mica.agents.analysis import observability

    event, _record, key = await start(chat, monkeypatch)
    report = AsyncMock()
    monkeypatch.setattr(observability, "report", report)
    editor = AsyncMock(side_effect=memory_tasks.EditorError("invalid_schema"))
    monkeypatch.setattr(memory_tasks, "edit", editor)
    for _ in range(memory_tasks.EDITOR_ATTEMPTS):
        with pytest.raises(memory_tasks.EditorError):
            await memory_tasks.process(chat, event, chat.store)
    with pytest.raises(memory_tasks.EditorDeferred):
        await memory_tasks.process(chat, event, chat.store)
    assert editor.await_count == report.await_count == 3
    cached = json.loads(chat.store.get(key)[0])
    assert cached["editor_attempts"] == 3 and "changes" not in cached
    assert not (await chat.repository.get_connector_state("claude-event:" + event)).get(
        "memory_editor_done"
    )
    cached["editor_window"] = 0
    chat.store.put(key, json.dumps(cached).encode())
    editor.side_effect = None
    editor.return_value = compact_edit()
    await memory_tasks.process(chat, event, chat.store)
    assert editor.await_count == 4


async def test_context_limit_preserves_source_without_calling_model(chat, monkeypatch, caplog):
    event, _record, key = await start(chat, monkeypatch)
    base = await chat.memory_backend.read(chat.pid)
    await chat.memory_backend.commit(
        chat.pid,
        base,
        {"open.md": OPEN + "PRIVATE" * 300},
        source="editor",
        event_id="other",
    )
    chat.settings.memory_editor_chars = 1000
    editor = AsyncMock()
    monkeypatch.setattr(memory_tasks, "edit", editor)
    with pytest.raises(memory_tasks.EditorError, match="context_limit"):
        await memory_tasks.process(chat, event, chat.store)
    with pytest.raises(memory_tasks.EditorDeferred):
        await memory_tasks.process(chat, event, chat.store)
    editor.assert_not_awaited()
    assert "reason=context_limit" in caplog.text and "PRIVATE" not in caplog.text
    assert "changes" not in json.loads(chat.store.get(key)[0])
