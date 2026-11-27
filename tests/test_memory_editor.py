"""Bounded editing, repair feedback, and recovery of previously rejected save inputs."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_memory_tasks import compact_edit

from mica.agents.analysis import (
    job_lifecycle,
    memory_reads,
    memory_tasks,
    project_memory,
    thread_memory,
)

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
        return httpx.Response(200, json=body.pop(0) if isinstance(body, list) else body)

    monkeypatch.setattr(
        memory_reads.httpx,
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
    selected = memory_tasks.editor_files(snapshot(files), 12000)
    assert selected["open.md"] == files["open.md"]
    assert "PROJECT.md" not in selected
    selected = memory_tasks.editor_files(snapshot(files), 32000)
    assert selected == files
    with pytest.raises(memory_tasks.EditorError, match="context_limit"):
        memory_tasks.editor_files(snapshot(files), 1000)


def test_repair_includes_required_topic_before_optional_core():
    files = {
        "open.md": OPEN,
        "PROJECT.md": "p" * 3000,
        "results/topic.md": "t" * 2000,
        "lessons.md": "l" * 2000,
    }
    selected = memory_tasks.editor_files(
        snapshot(files),
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
        (model_response(stop_reason="max_tokens"), "output_limit"),
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
    assert [tool["name"] for tool in requests[0]["tools"]] == ["edit_project_memory"]
    assert requests[0]["tool_choice"]["type"] == "tool"


async def test_large_full_file_replacement_keeps_unrelated_memory(monkeypatch):
    # The writer sees complete files, including a large accumulated pending-work file.
    original = OPEN + "An unrelated attributed fact must survive.\n" * 750
    replacement = original + "2026-11-10: Alice requested clearer segmentation figures.\n"
    requests = []
    mock_model(monkeypatch, model_response({"open.md": replacement}), requests)
    result = await memory_tasks.edit("test", "test", {
        "files": {"open.md": original}, "existing_paths": ["open.md"],
        "thread": {"summary": "Earlier context"},
    })
    assert original in result["open.md"]
    assert "clearer segmentation figures" in result["open.md"]
    assert json.loads(result["_thread"])["paraphrase"] == "A pending request."
    assert requests[0]["max_tokens"] == 16384


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

    async def edit(_key, _model, context, **kwargs):
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

    async def edit(_key, _model, context, **kwargs):
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


async def test_oversized_utf8_proposal_repairs_before_any_commit(chat, monkeypatch):
    event, record, key = await start(chat, monkeypatch)
    oversized = OPEN + "é" * 9000
    assert len(oversized) < 16384 < len(oversized.encode())
    calls = []

    async def edit(_key, _model, context, **kwargs):
        calls.append(context.copy())
        assert context["file_limit_bytes"] == 16384
        if len(calls) == 1:
            return {**compact_edit(), "open.md": oversized}
        assert context["editor_feedback"] == {"reason": "file_limit"}
        return {**compact_edit(), "open.md": OPEN + "Pending request.\n"}

    monkeypatch.setattr(memory_tasks, "edit", edit)
    commit = AsyncMock(wraps=chat.memory_backend.commit)
    monkeypatch.setattr(chat.memory_backend, "commit", commit)
    with pytest.raises(memory_tasks.EditorError, match="file_limit"):
        await memory_tasks.process(chat, event, chat.store)
    commit.assert_not_awaited()
    cached = json.loads(chat.store.get(key)[0])
    assert "changes" not in cached and cached["editor_feedback"]["reason"] == "file_limit"
    await memory_tasks.process(chat, event, chat.store)
    commit.assert_awaited_once()
    assert (await chat.repository.get_connector_state("claude-event:" + event))[
        "memory_editor_done"
    ]


async def test_cached_oversized_proposal_rebases_after_partial_thread_save(chat, monkeypatch):
    event, record, key = await start(chat, monkeypatch)
    base = await chat.memory_backend.read(chat.pid)
    await chat.memory_backend.commit(
        chat.pid, base, {"open.md": OPEN}, source="editor", event_id="other-event",
    )
    validate = memory_tasks.validate_changes
    monkeypatch.setattr(memory_tasks, "validate_changes", lambda *args: None)
    monkeypatch.setattr(memory_tasks, "edit", AsyncMock(return_value={
        **compact_edit(), "open.md": OPEN + "x" * 20000,
    }))
    # Reproduce the old release: thread persisted, project replacement rejected.
    with pytest.raises(RuntimeError, match="rejected"):
        await memory_tasks.process(chat, event, chat.store)
    original = json.loads(chat.store.get(key)[0])
    latest = await chat.memory_backend.read(chat.pid)
    monkeypatch.setattr(memory_tasks, "validate_changes", validate)

    async def edit(_key, _model, context, **kwargs):
        assert context["files"]["open.md"] == OPEN
        assert context["user"] == original["context"]["user"]
        assert context["editor_feedback"] == {"reason": "file_limit"}
        return {
            "_thread": thread_memory.Prose(paraphrase="Different model wording").model_dump_json(),
            "open.md": OPEN + "Original request remains pending.\n",
        }

    monkeypatch.setattr(memory_tasks, "edit", edit)
    await memory_tasks.process(chat, event, chat.store)
    cached = json.loads(chat.store.get(key)[0])
    assert cached["base"]["head_oid"] == latest.head_oid
    assert cached["thread_prose"] == original["thread_prose"]
    saved = await chat.memory_backend.read(chat.pid)
    assert "Original request remains pending." in saved.files["open.md"].text
    rows = thread_memory.log_entries(
        {p: f.text for p, f in saved.files.items()}, record["thread_key"]
    )
    assert [r.event_id for r in rows] == [event]
    assert not any(p.startswith("_unmerged/") for p in saved.files)


@pytest.mark.parametrize("already_covered", [False, True])
async def test_conflict_repairs_only_unsaved_edits_preserving_thread(
    chat, monkeypatch, already_covered,
):
    from mica.agents.analysis import memory_archive

    event, record, key = await start(chat, monkeypatch)
    base = await chat.memory_backend.read(chat.pid)
    await chat.memory_backend.commit(
        chat.pid, base, {"open.md": OPEN}, source="editor", event_id="setup",
    )
    lesson = "type: lesson\nsource: Alice\nstatus: current\n\nAccepted separate lesson.\n"
    pending = "Original request remains pending.\n"
    newer = OPEN + "Newer unrelated work.\n" + (pending if already_covered else "")

    async def first_edit(*args, **kwargs):
        latest = await chat.memory_backend.read(chat.pid)
        await chat.memory_backend.commit(
            chat.pid, latest, {"open.md": newer}, source="editor", event_id="concurrent",
        )
        return {**compact_edit(), "open.md": OPEN + pending, "lessons.md": lesson}

    monkeypatch.setattr(memory_tasks, "edit", first_edit)
    with pytest.raises(RuntimeError, match="conflicted"):
        await memory_tasks.process(chat, event, chat.store)
    cached = json.loads(chat.store.get(key)[0])
    assert cached["editor_repair_paths"] == ["open.md"]
    assert cached["editor_conflicts"] == {"open.md": OPEN + pending}
    assert "changes" not in cached and "saved" not in cached
    original_thread = cached["thread_prose"]
    prior = await chat.memory_backend.read(chat.pid)
    lesson_oid = prior.files["lessons.md"].oid
    conflict = next(p for p in prior.files if p.startswith("_unmerged/"))
    original_entry = thread_memory.log_entries(
        {p: f.text for p, f in prior.files.items()}, record["thread_key"],
    )[0].digest()

    async def repair(_key, _model, context, **kwargs):
        assert context["files"]["open.md"] == newer
        assert context["repair_paths"] == ["open.md"]
        assert context["editor_feedback"] == {"reason": "conflict"}
        return {
            "_thread": thread_memory.Prose(paraphrase="Changed wording").model_dump_json(),
            **({} if already_covered else {"open.md": newer + pending}),
        }

    editor = AsyncMock(side_effect=repair)
    monkeypatch.setattr(memory_tasks, "edit", editor)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    editor.assert_awaited_once()
    saved = await chat.memory_backend.read(chat.pid)
    assert saved.files["open.md"].text == newer + ("" if already_covered else pending)
    assert saved.files["lessons.md"].oid == lesson_oid
    assert conflict not in saved.files
    assert memory_archive.archive_path(conflict, OPEN + pending) in saved.files
    assert json.loads(chat.store.get(key)[0])["thread_prose"] == original_thread
    done = await chat.repository.get_connector_state("claude-event:" + event)
    assert done["memory_editor_done"] and "memory_pending" not in done
    assert done["memory_receipt"]["entry_sha256"] == original_entry


async def test_conflict_repair_cannot_rewrite_already_accepted_files(monkeypatch):
    requests = []
    mock_model(monkeypatch, model_response({"lessons.md": OPEN}), requests)
    with pytest.raises(memory_tasks.EditorError, match="unsupported_path"):
        await memory_tasks.edit("test", "test", {
            "files": {"open.md": OPEN, "lessons.md": OPEN}, "repair_paths": ["open.md"],
        })
    schema = requests[0]["tools"][0]["input_schema"]
    assert schema["properties"]["changes"]["propertyNames"] == {"enum": ["open.md"]}
