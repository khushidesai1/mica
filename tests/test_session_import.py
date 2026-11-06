"""Session imports preserve provenance and make retryable, authorized progress."""

# ruff: noqa: F811
import hashlib
import json
import subprocess
import tracemalloc
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import BackgroundTasks
from test_ambient import background  # noqa: F401
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import (
    access,
    ambient_delivery,
    commands,
    job_lifecycle,
    memory_checks,
    memory_tasks,
    memory_workspace,
    project_memory,
    session_import,
    session_render,
    session_review,
    session_sources,
)
from mica.agents.analysis.config import Settings
from mica.agents.analysis.storage import LocalStorage


def record(content, role="assistant"):
    return {
        "type": role,
        "sessionId": "synthetic-session",
        "cwd": "/analysis",
        "message": {"role": role, "content": content},
    }


def session(path):
    rows = [
        {"type": "file-history-snapshot", "snapshot": "NOISE SNAPSHOT"},
        {"type": "queue-operation", "request": "NOISE REQUEST"},
        record("Use the high group cutoff", "user"),
        record(
            [
                {
                    "type": "tool_use",
                    "id": "w1",
                    "name": "Write",
                    "input": {
                        "file_path": "/analysis/model.py",
                        "content": "model = 'age + stage'\n",
                    },
                }
            ]
        ),
        record(
            [
                {
                    "type": "tool_use",
                    "id": "b1",
                    "name": "Bash",
                    "input": {"command": "python model.py"},
                }
            ]
        ),
        record(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "b1",
                    "content": "Adjusted for age and stage\nHR 5.3; n=43\n"
                    "High group: threshold >=28%",
                }
            ],
            "user",
        ),
        record(
            [
                {
                    "type": "tool_use",
                    "id": "w2",
                    "name": "Write",
                    "input": {"file_path": "/analysis/model.py", "content": "model = 'age'\n"},
                }
            ]
        ),
        record(
            [
                {
                    "type": "tool_use",
                    "id": "b2",
                    "name": "Bash",
                    "input": {
                        "command": "cat > helper.py <<'EOF'\nprint(43)\nEOF\npython helper.py"
                    },
                }
            ]
        ),
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\nnot json\n")
    return path


def test_renderer_keeps_versions_qualifiers_and_stable_anchors(tmp_path, monkeypatch):
    path = session(tmp_path / "session.jsonl")
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("No execution")))
    stats = {}
    rendered = list(session_render.render(path, stats))
    text = "".join(part for _, part in rendered)
    assert "NOISE" not in text and stats["malformed"] == 1
    assert "Adjusted for age and stage" in text and "HR 5.3; n=43" in text
    assert "model = 'age + stage'" in text and "model = 'age'" in text
    assert "Referenced script /analysis/model.py: version #t000000004_000000" in text
    assert "Result for #t000000005_000000" in text
    assert "print(43)" in text
    assert rendered == list(session_render.render(path, {}))


def test_long_result_context_and_table_header():
    text = "Table | Estimate\n--- | ---\n" + "blank\n" * 3000
    text += "adjusted for age and stage\ncohort excludes controls\nHR 5.3\nunits are patients\n"
    rendered = session_render.result_text(text)
    assert "adjusted for age and stage" in rendered and "units are patients" in rendered
    assert len(rendered) < len(text)


def test_renderer_slices_join_without_loss(tmp_path, monkeypatch, capsys):
    path = session(tmp_path / "session.jsonl")
    with path.open("a") as stream:
        stream.write(json.dumps(record("A definition. " * 4000)) + "\n")
    complete = "".join(text for _, text in session_render.render(path, {}))
    parts, start = [], session_render.START
    while start is not None:
        monkeypatch.setattr(
            "sys.argv", ["render", str(path), "--from", start, "--max-chars", "5000"]
        )
        session_render.main()
        out = capsys.readouterr()
        parts.append(out.out)
        next_anchor = json.loads(out.err)["next_anchor"]
        assert next_anchor is None or next_anchor > start
        start = next_anchor
    assert "".join(parts) == complete


def test_renderer_35mb_has_bounded_memory(tmp_path):
    path = tmp_path / "large.jsonl"
    row = json.dumps(record("Long scientific context " * 400)) + "\n"
    with path.open("w") as stream:
        for _ in range(35_000_000 // len(row) + 1):
            stream.write(row)
    tracemalloc.start()
    stats = {}
    count = sum(1 for _ in session_render.render(path, stats))
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert path.stat().st_size >= 35_000_000 and count > 3000
    assert peak < 12_000_000


def test_editor_whole_files_with_budget():
    snapshot = project_memory.Snapshot(
        head_oid="",
        files={
            "PROJECT.md": project_memory.File(oid="", text="guide"),
            "results/model.md": project_memory.File(
                oid="", text="model " * 5000 + "IMPORTANT TAIL"
            ),
            "data/small.md": project_memory.File(oid="", text="small data"),
        },
    )
    selected = memory_tasks.editor_files(snapshot, "model", 40000)
    assert selected["results/model.md"].endswith("IMPORTANT TAIL")
    selected = memory_tasks.editor_files(snapshot, "model", 100)
    assert "results/model.md" not in selected
    assert selected["PROJECT.md"] == "guide"


@pytest.mark.parametrize(
    "path,allowed",
    [("results/unseen.md", False), ("results/new.md", True), ("results/shown.md", True)],
)
async def test_editor_rejects_unseen_replacements_but_allows_new_topics(monkeypatch, path, allowed):
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "edit_project_memory",
                                "input": {
                                    "changes": {
                                        path: "## Definitions\nUnit: patients. n=43; cutoff 28%."
                                    }
                                },
                            }
                        ]
                    },
                )
            )
        )

    monkeypatch.setattr(memory_tasks.httpx, "AsyncClient", factory)
    context = {
        "files": {"results/shown.md": "old result"},
        "existing_paths": ["results/unseen.md", "results/shown.md"],
    }
    if allowed:
        assert path in await memory_tasks.edit("test", "model", context)
    else:
        with pytest.raises(ValueError, match="not shown"):
            await memory_tasks.edit("test", "model", context)


def test_checks_have_independent_conditions_and_two_repairs(tmp_path):
    settings = Settings(_env_file=None, memory_auto_edit=False)
    root = tmp_path / "knowledge/results"
    root.mkdir(parents=True)
    (root / "new.md").write_text("n=43")
    check = memory_checks.Checks(tmp_path, settings, "", project_memory.Snapshot(head_oid=""), [])
    assert set(check.reasons()) == {"results", "import"}
    assert check.check() and check.check() and not check.check()
    (root / "new.md").write_text("## Definitions\nUnit: patients; n=43")
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "import-review.json").write_text('{"highlights": {}}')
    assert not check.reasons()


def test_review_proposal_transport_and_missing_review(tmp_path):
    settings = Settings(_env_file=None)
    (tmp_path / "knowledge").mkdir()
    (tmp_path / "notes").mkdir()
    raw = {"next_anchor": "t000000001_000000", "saved": [], "highlights": {"h1": "not_found"}}
    (tmp_path / "notes/import-review.json").write_text(json.dumps(raw))
    base = project_memory.Snapshot(head_oid="")
    pending = memory_workspace.decode_pending(
        memory_workspace.pending(tmp_path, base, settings, import_pass=True), settings
    )
    assert pending.review.highlights == {"h1": "not_found"}
    assert not memory_workspace.decode_pending(
        memory_workspace.pending(tmp_path, base, settings), settings
    ).review
    (tmp_path / "notes/import-review.json").write_text("invalid")
    assert not memory_workspace.decode_pending(
        memory_workspace.pending(tmp_path, base, settings, import_pass=True), settings
    ).review


def source_file(store, path, identifier="a" * 32):
    data = path.read_bytes()
    store.put("temporary/session.jsonl", data)
    return (
        session_sources.Source(
            id=identifier,
            project="project-one",
            uploader="alice@example.com",
            filename=path.name,
            date="2026-11-06",
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        .file()
        .model_copy(update={"key": "temporary/session.jsonl"})
    )


def test_durable_source_integrity_metadata_last_and_scope(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path / "storage")
    path = session(tmp_path / "session.jsonl")
    file = source_file(store, path)
    writes = []
    put = store.put_if

    def checked(key, raw, generation):
        writes.append(key)
        assert store.path(key.replace("meta.json", "session.jsonl")).exists()
        return put(key, raw, generation)

    monkeypatch.setattr(store, "put_if", checked)
    settings = Settings(_env_file=None)
    source = session_sources.persist(
        store, file, "a" * 32, "project-one", "alice@example.com", {}, settings
    )
    assert writes[-1] == source.prefix + "meta.json"
    assert session_sources.catalog(store, "project-two") == []
    assert session_sources.catalog(store, "project-one") == [source]
    work = tmp_path / "work"
    work.mkdir()
    session_sources.materialize(store, work, "project-one", [source.model_dump()])
    assert (work / source.file().path).read_bytes() == path.read_bytes()
    with pytest.raises(ValueError, match="Cross-project"):
        session_sources.materialize(store, work, "project-two", [source.model_dump()])
    too_large = source.model_copy(update={"size": 2_000_000})
    settings.session_project_mb = 1
    assert not session_sources.select([too_large], settings)
    with pytest.raises(ValueError, match="cap"):
        session_sources.select([too_large], settings, source.id)


def test_wrong_content_with_jsonl_suffix_is_rejected(tmp_path):
    path = tmp_path / "wrong.jsonl"
    path.write_text('{"random": "data"}\n')
    store = LocalStorage(tmp_path / "storage")
    with pytest.raises(ValueError, match="session records"):
        session_sources.persist(
            store,
            source_file(store, path),
            "a" * 32,
            "project-one",
            "alice@example.com",
            {},
            Settings(_env_file=None),
        )
    assert not session_sources.catalog(store, "project-one")


@pytest.fixture
def importer(background, monkeypatch):
    background.settings.session_import_projects = background.pid
    background.settings.memory_auto_edit = True
    monkeypatch.setattr(session_import, "AsyncWebClient", lambda **kw: background.test_slack)
    background.test_slack.views_open.return_value = {"view": {"id": "V1"}}
    return background


async def open_import(importer):
    incoming = payload(text="import session")
    event = await importer.accept(incoming)
    await importer.process(event, 0)
    identifier = hashlib.sha256(("session-import:" + event).encode()).hexdigest()[:32]
    return await importer.repository.get_connector_state(session_import.key(identifier))


def test_upload_modal_respects_slack_input_constraints():
    view = session_import.modal({"id": "a" * 32})
    # Slack rejects the entire views.open request above this API limit, before upload.
    inputs = [b["element"] for b in view["blocks"] if b["type"] == "input"]
    file_input = next(element for element in inputs if element["type"] == "file_input")
    # JSONL isn't a Slack filetype enum; filtering would reject the whole modal.
    assert "filetypes" not in file_input
    assert file_input["max_files"] == 1
    for element in inputs:
        if element["type"] == "plain_text_input":
            assert 1 <= element["max_length"] <= 3000


def action(state, verb="open"):
    return {
        "type": "block_actions",
        "team": {"id": state["team"]},
        "user": {"id": state["user"]},
        "channel": {"id": state["channel"]},
        "message": {"ts": state["card_ts"]},
        "trigger_id": "trigger",
        "actions": [{"action_id": "session_import:" + verb, "value": state["id"]}],
    }


async def test_command_modal_ack_does_not_download_and_duplicate_submit_is_noop(
    importer, monkeypatch
):
    assert commands.parse("import session").name == "import session"
    state = await open_import(importer)
    assert state["status"] == "awaiting_upload"
    bg = BackgroundTasks()
    await session_import.handle(importer, importer.store, action(state), bg)
    download = Mock(side_effect=AssertionError("No download in ack"))
    monkeypatch.setattr(attachments_for_test(), "ingest", download)
    submission = {
        "type": "view_submission",
        "team": {"id": "T1"},
        "user": {"id": "U1"},
        "view": {
            "id": "V1",
            "private_metadata": state["id"],
            "state": {
                "values": {
                    "file": {"value": {"files": [{"id": "F1", "name": "session.jsonl"}]}},
                    "highlights": {"value": {"value": "Model\nCutoff"}},
                    "request": {"value": {"value": "Explain the cutoff"}},
                }
            },
        },
    }
    assert await session_import.handle(importer, importer.store, submission, bg) == {}
    assert await session_import.handle(importer, importer.store, submission, bg) == {}
    saved = await importer.repository.get_connector_state(session_import.key(state["id"]))
    assert saved["status"] == "received" and saved["highlights"] == {"h1": "Model", "h2": "Cutoff"}
    assert len(bg.tasks) == 1
    download.assert_not_called()


def attachments_for_test():
    from mica.agents.analysis import attachments

    return attachments


async def test_import_gates_surface_project_and_actor(importer):
    record = {"payload": payload(text="import session"), "thread_key": "a" * 32}
    grant = access.resolve(importer.app, importer.settings, importer.store, record)
    importer.settings.session_import_projects = ""
    assert "aren't enabled" in await session_import.start(importer, "a" * 32, record, grant)
    importer.settings.session_import_projects = importer.pid
    state = await open_import(importer)
    wrong = action(state)
    wrong["user"]["id"] = "U2"
    with pytest.raises(PermissionError):
        await session_import.handle(importer, importer.store, wrong, BackgroundTasks())
    record["surface"] = "email"
    assert "only in Slack" in await session_import.start(importer, "a" * 32, record, grant)


async def ready_state(importer):
    state = await open_import(importer)
    state.update(
        status="ready", import_due=0, request="Explain the cutoff", highlights={"h1": "Cutoff"}
    )
    await session_import.save(importer, state)
    return state


async def finish_pass(importer, state, next_anchor, *, failed=False, valid=True):
    record = await importer.repository.get_connector_state("claude-event:" + state["active"])
    record.update(
        status="failed" if failed else "complete",
        memory_editor_done=True,
        import_review={
            "valid": valid,
            "next_anchor": next_anchor,
            "commit": "c" * 40,
            "paths": {"results/model.md": "saved"},
            "highlights": {"h1": ["results/model.md"]},
        },
    )
    await importer.repository.set_connector_state("claude-event:" + state["active"], record)
    await session_import.process(importer, state["id"])
    return await importer.repository.get_connector_state(session_import.key(state["id"]))


async def test_two_passes_then_separate_request_and_retry(importer):
    state = await ready_state(importer)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    assert state["status"] == "running" and state["attempts"] == 1
    first = state["active"]
    state = await finish_pass(importer, state, "t000000010_000000")
    assert state["status"] == "running" and state["attempts"] == 2 and state["active"] != first
    state = await finish_pass(importer, state, None)
    assert state["status"] == "done" and state["request_event"]
    request = await importer.repository.get_connector_state(
        "claude-event:" + state["request_event"]
    )
    assert not request.get("ambient")
    before = json.dumps(state, sort_keys=True)
    await session_import.process(importer, state["id"])
    request["status"] = "failed"
    await importer.record_terminal(state["request_event"], request)
    assert (
        json.dumps(
            await importer.repository.get_connector_state(session_import.key(state["id"])),
            sort_keys=True,
        )
        == before
    )
    assert len([m for m in importer.sent if "Session import done" in m["text"]]) == 1


async def test_budget_retry_halves_slice_then_continue_resumes(importer):
    state = await ready_state(importer)
    state["request"] = ""
    await session_import.save(importer, state)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    for _ in range(2):
        importer.store.put(
            f"tasks/slack-analysis/events/{state['active']}/import-outcome.json",
            b'{"error":"error_max_budget_usd"}',
        )
        state = await finish_pass(importer, state, None, failed=True)
    assert state["status"] == "incomplete" and state["attempts"] == 2
    assert state["slice_chars"] == importer.settings.session_import_slice_chars // 2
    assert state["anchor"] == session_render.START
    state["source"] = {}  # Uploaded source exists; continuation does not re-ingest.
    await session_import.save(importer, state)
    await session_import.handle(
        importer, importer.store, action(state, "continue"), BackgroundTasks()
    )
    saved = await importer.repository.get_connector_state(session_import.key(state["id"]))
    assert (
        saved["status"] == "ready"
        and saved["limit"] == 2 + importer.settings.session_import_max_passes
    )
    assert saved["anchor"] == state["anchor"]


async def test_missing_review_stops_and_keeps_committed_paths(importer):
    state = await ready_state(importer)
    state["request"] = ""
    await session_import.save(importer, state)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    state = await finish_pass(importer, state, None, valid=False)
    assert state["status"] == "incomplete"
    assert "results/model.md: saved" in state["card_text"]


async def test_review_reads_exact_commit_and_classifies_paths():
    paths = ["results/new.md", "results/old.md", "results/conflict.md", "results/bad.md"]
    snapshot = project_memory.Snapshot(
        head_oid="", files={p: project_memory.File(oid="", text="existing") for p in paths[:3]}
    )
    conflict = "_unmerged/12345678/results/conflict.md"
    snapshot.files[conflict] = project_memory.File(oid="", text="new conflict")
    backend = SimpleNamespace(read=AsyncMock(return_value=snapshot))
    saved = project_memory.Saved(
        commit_oid="a" * 40,
        added=[paths[0]],
        unmerged=[conflict],
        rejected={paths[3]: "storage_full"},
    )
    cached = {
        "review": {
            "saved": [{"path": p, "summary": "detail"} for p in paths],
            "highlights": {"h1": [paths[0]], "h3": "not_found"},
        }
    }
    result = await session_import.verified_review(
        backend, "project-one", saved, cached, ["h1", "h2"]
    )
    backend.read.assert_awaited_once_with("project-one", head_oid="a" * 40)
    assert result["paths"][paths[0]] == "saved"
    assert result["paths"][paths[1]] == "already in memory"
    assert result["paths"][paths[2]].startswith("saved for review")
    assert result["paths"][paths[3]] == "not saved: storage_full"
    assert result["highlights"]["h2"] == "not checked" and "h3" not in result["highlights"]


async def test_lost_progress_ack_is_reconciled_before_accept(importer, monkeypatch):
    state = await ready_state(importer)
    real = importer.test_slack.chat_postMessage.side_effect
    uncertain = True

    async def lost(**kwargs):
        nonlocal uncertain
        response = await real(**kwargs)
        if uncertain:
            uncertain = False
            raise TimeoutError("acknowledgement lost")
        return response

    importer.test_slack.chat_postMessage.side_effect = lost
    with pytest.raises(TimeoutError):
        await session_import.process(importer, state["id"])
    await session_import.process(importer, state["id"])
    saved = await importer.repository.get_connector_state(session_import.key(state["id"]))
    assert saved["attempts"] == 1
    assert len([m for m in importer.sent if "Importing part 1" in m["text"]]) == 1
    await importer.repository.patch_connector_state(
        session_import.key(state["id"]), {"import_due": 0}
    )
    assert await session_import.maintenance(importer) == 1
    assert "session-import" in job_lifecycle.enqueue.call_args.args[1]


async def test_attachment_intake_rechecks_access_and_bounds_size(importer, tmp_path, monkeypatch):
    from mica.agents.analysis import attachments

    path = session(tmp_path / "upload.jsonl")
    staged = source_file(importer.store, path)
    importer.test_slack.files_info.return_value = {"file": {"id": "F1", "name": "upload.jsonl"}}
    ingest = Mock(return_value=[staged])
    monkeypatch.setattr(attachments, "ingest", ingest)
    incoming = payload(text="import session")
    incoming["event"]["files"] = [{"id": "F1", "name": "upload.jsonl"}]
    event = await importer.accept(incoming)
    assert await importer.process(event, 0) == "complete"
    identifier = hashlib.sha256(("session-import:" + event).encode()).hexdigest()[:32]
    state = await importer.repository.get_connector_state(session_import.key(identifier))
    assert state["status"] == "received"
    importer.settings.session_import_projects = ""
    with pytest.raises(PermissionError, match="enabled"):
        await session_import.intake(importer, state)
    ingest.assert_not_called()
    importer.settings.session_import_projects = importer.pid
    importer.settings.attachment_max_mb = 200
    await session_import.intake(importer, state)
    assert ingest.call_args.args[-1].attachment_max_mb == 100
    assert state["status"] == "ready" and state["source"]["sha256"] == staged.sha256
    # A retry of intake or the same bytes in another upload does not start another import.
    await session_import.save(importer, state)
    other = {**state, "id": "b" * 32, "status": "received"}
    await session_import.intake(importer, other)
    assert other["status"] == "duplicate" and other["duplicate"] == identifier


async def test_unfiltered_picker_still_rejects_wrong_filename_before_download(
    importer, monkeypatch
):
    from mica.agents.analysis import attachments

    state = await open_import(importer)
    state.update(status="received", file={"id": "F1"})
    importer.test_slack.files_info.return_value = {"file": {"id": "F1", "name": "session.txt"}}
    ingest = Mock(side_effect=AssertionError("Invalid files must not be downloaded"))
    monkeypatch.setattr(attachments, "ingest", ingest)
    with pytest.raises(ValueError, match="Choose a .jsonl"):
        await session_import.intake(importer, state)
    ingest.assert_not_called()


async def test_pass_cap_never_starts_an_extra_worker(importer):
    state = await ready_state(importer)
    state.update(limit=1, request="")
    await session_import.save(importer, state)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    state = await finish_pass(importer, state, "t000000010_000000")
    assert state["status"] == "incomplete" and state["attempts"] == 1
    assert state["anchor"] == "t000000010_000000"
    assert len([m for m in importer.sent if "Importing part" in m["text"]]) == 1


async def test_import_origin_bypasses_router_and_ambient_delivery(importer, tmp_path, monkeypatch):
    state = await ready_state(importer)
    source = session_sources.persist(
        importer.store,
        source_file(importer.store, session(tmp_path / "source.jsonl")),
        state["id"],
        importer.pid,
        state["uploader"],
        {},
        importer.settings,
    )
    state["source"] = source.model_dump()
    await session_import.save(importer, state)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    importer.decision.reset_mock()
    assert await importer.process(state["active"], 0) == "running", (
        await importer.repository.get_connector_state("claude-event:" + state["active"])
    ).get("notice_text")
    importer.decision.assert_not_awaited()
    saved = await importer.repository.get_connector_state("claude-event:" + state["active"])
    assert saved["direct_decision"]["worker"]["tier"] == "standard"
    manifest = json.loads(
        importer.store.get(f"tasks/slack-analysis/events/{state['active']}/request.json")[0]
    )
    assert manifest["project_sessions"][0]["id"] == state["id"]
    from mica.agents.analysis.registry import REGISTRY_KEY, Registry

    checkpoint = json.loads(importer.store.get(access.checkpoint_key(saved["thread_key"]))[0])
    doc = Registry(importer.store).load()
    doc.projects[importer.pid].owner = "other@example.com"
    doc.projects[importer.pid].members.add("alice@example.com")
    importer.store.put(REGISTRY_KEY, doc.model_dump_json().encode())
    grant = access.resolve(importer.app, importer.settings, importer.store, saved)
    manifest["access_signature"] = grant.signature
    assert access.worker_permitted(importer.store, manifest, checkpoint, settings=importer.settings)
    manifest["write_grant"] = {"id": "not-authorized-for-import"}
    assert not access.worker_permitted(
        importer.store, manifest, checkpoint, settings=importer.settings
    )
    terminal = AsyncMock()
    monkeypatch.setattr(ambient_delivery, "terminal", terminal)
    saved.update(status="failed", terminal_recorded=True)
    await importer.record_terminal(state["active"], saved)
    terminal.assert_not_awaited()
    saved["ambient"].pop("kind")
    await importer.record_terminal(state["active"], saved)
    terminal.assert_awaited_once()


async def test_import_partial_memory_save_finishes_and_receipt_repairs_projection(
    importer, tmp_path, monkeypatch
):
    from mica.agents.analysis import observability, thread_memory

    state = await ready_state(importer)
    source = session_sources.persist(
        importer.store,
        source_file(importer.store, session(tmp_path / "source.jsonl")),
        state["id"],
        importer.pid,
        state["uploader"],
        {},
        importer.settings,
    )
    state["source"] = source.model_dump()
    await session_import.save(importer, state)
    await session_import.process(importer, state["id"])
    state = await importer.repository.get_connector_state(session_import.key(state["id"]))
    event = state["active"]
    await importer.process(event, 0)
    manifest = json.loads(
        importer.store.get(f"tasks/slack-analysis/events/{event}/request.json")[0]
    )
    path = "results/model.md"
    text = ("type: result\nsource: synthetic session\nstatus: current\n\n"
            "## Definitions\nPatients n=43. Cutoff 28%.")
    proposal = memory_workspace.Proposal(
        base_head=manifest["memory"]["head_oid"],
        changes={path: text},
        thread=thread_memory.Prose(
            paraphrase="Imported one synthetic slice.", summary="Cutoff defined."
        ),
        review=session_review.Review(
            saved=[session_review.Item(path=path, summary="Kept cutoff")], highlights={"h1": [path]}
        ),
    )
    importer.store.put(
        f"projects/_threads/{manifest['thread_key']}/pending/{event}/memory-proposal.json",
        proposal.model_dump_json().encode(),
    )
    record = await importer.repository.get_connector_state("claude-event:" + event)
    record.update(status="complete", memory_reply="Pass completed.")
    await importer.repository.set_connector_state("claude-event:" + event, record)
    commit = importer.memory_backend.commit

    async def partial(*args, **kwargs):
        saved = await commit(*args, **kwargs)
        saved.rejected["results/rejected.md"] = "storage_full"
        return saved

    monkeypatch.setattr(importer.memory_backend, "commit", partial)
    report = AsyncMock()
    monkeypatch.setattr(observability, "report", report)
    await memory_tasks.process(importer, event, importer.store)
    saved = await importer.repository.get_connector_state("claude-event:" + event)
    assert saved["memory_editor_done"] and saved["import_review"]["paths"][path] == "saved"
    assert saved["import_review"]["rejected"] == {"results/rejected.md": "storage_full"}
    report.assert_awaited()
    saved.pop("memory_editor_done")
    saved.pop("import_review")
    await importer.repository.set_connector_state("claude-event:" + event, saved)
    await memory_tasks.enqueue(importer, event, saved)
    repaired = await importer.repository.get_connector_state("claude-event:" + event)
    assert repaired["memory_editor_done"] and repaired["import_review"]["paths"][path] == "saved"
