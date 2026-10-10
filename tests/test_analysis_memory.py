# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from analysis_helpers import memory_message as message
from analysis_helpers import seed_project
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import access, direct
from pistachio.agents.analysis import user_memory as mem
from pistachio.agents.analysis.commands import Command
from pistachio.agents.analysis.identity import user_key
from pistachio.agents.analysis.registry import Registry, RegistryDoc
from pistachio.agents.analysis.storage import LocalStorage


def setup(tmp_path):
    store = LocalStorage(tmp_path)
    pid = seed_project(store)
    doc = Registry(store).load()
    return store, pid, doc


def test_user_memory_partition_access_filter_and_budget(tmp_path):
    store, pid, doc = setup(tmp_path)
    doc.share(pid, "alice@example.com", "bob@example.com")
    for i in range(12):
        mem.apply(
            store,
            "bob@example.com",
            str(i),
            mem.Edits(edits=[mem.Edit(op="note_add", text=f"plot viridis {i} " + "x" * 560)]),
            project_id=pid,
            expected_epoch=0,
            doc=doc,
        )
    assert mem.load(store, "alice@example.com").notes == []
    bob = mem.load(store, "bob@example.com")
    rendered = mem.render(bob, doc, "bob@example.com", "viridis")
    assert len(rendered) <= 2500
    assert 1 <= rendered.count("[from") <= 5
    doc.share(pid, "alice@example.com", "bob@example.com", remove=True)
    assert not mem.render(bob, doc, "bob@example.com", "viridis")
    assert not mem.view(bob, doc, "bob@example.com").notes
    assert mem.key("Alice@Example.com ") == mem.key("alice@example.com")


def test_validation_receipts_stable_numbers_and_epoch(tmp_path):
    store, pid, doc = setup(tmp_path)
    email = "alice@example.com"
    edits = mem.Edits(
        edits=[
            mem.Edit(op="note_add", text="viridis"),
            mem.Edit(op="note_add", text="sk-secretcredential"),
            mem.Edit(op="block_append", block="preferences", text="PNG"),
        ]
    )
    a = mem.apply(store, email, "a", edits, project_id=pid, expected_epoch=0, doc=doc)
    assert mem.apply(store, email, "a", edits, project_id=pid, expected_epoch=0, doc=doc) == a
    m = mem.load(store, email)
    assert len(m.notes) == 1 and m.next_seq == 2 and m.blocks["preferences"].text == "PNG"
    grant = access.Grant(email, True, {}, doc, pid, True, "T1:D1")
    mem.command(Command("forget", ("1",)), grant, store, "delete")
    mem.apply(store, email, "a", edits, project_id=pid, expected_epoch=0, doc=doc)
    assert not mem.load(store, email).notes
    # In-flight stale editors cannot resurrect forgotten information.
    mem.apply(
        store,
        email,
        "stale",
        mem.Edits(edits=[mem.Edit(op="note_add", text="forgotten")]),
        expected_epoch=0,
        doc=doc,
    )
    assert not mem.load(store, email).notes
    mem.command(Command("remember", ("new",)), grant, store, "new")
    mem.command(Command("forget", ("1",)), grant, store, "delete")
    assert [(n.seq, n.text) for n in mem.load(store, email).notes] == [(2, "new")]
    mem.apply(
        store,
        email,
        "bad-replace",
        mem.Edits(
            edits=[mem.Edit(op="block_replace", block="preferences", old="missing", text="SVG")]
        ),
        expected_epoch=1,
        doc=doc,
    )
    assert mem.load(store, email).blocks["preferences"].text == "PNG"


async def test_memory_dm_only_cross_project_and_init_draft_separation(chat, monkeypatch):
    await message(chat, "remember I prefer viridis", "1.0")
    await message(chat, "What do I prefer?", "2.0")
    assert "viridis" in chat.decision.call_args.args[2]["user_memory"]
    seed_project(chat.store, name="other", channel="D1")
    await message(chat, "What should I use?", "3.0")
    assert "viridis" in chat.decision.call_args.args[2]["user_memory"]
    seed_project(chat.store, name="bob", owner="bob@example.com", channel="D2")
    await message(chat, "What should I use?", "4.0", user="U2", channel="D2")
    assert "viridis" not in chat.decision.call_args.args[2]["user_memory"]
    seed_project(chat.store, name="channel", channel="C1", publish=True)
    await message(chat, "Plot conventions?", "5.0", channel="C1")
    assert "user_memory" not in chat.decision.call_args.args[2]
    before = chat.decision.await_count
    record = await message(chat, "memory", "6.0", channel="C1")
    assert "DM" in record["notice_text"] and chat.decision.await_count == before
    monkeypatch.setattr(direct, "inventory", lambda *a: {})
    record = await message(chat, "init newproj gs://lab-data/new/ --force", "7.0")
    assert "user_memory" not in chat.decision.call_args.args[2]
    assert "viridis" in record["notice_text"]
    state = json.loads(chat.store.get(access.checkpoint_key(record["thread_key"]))[0])
    assert "viridis" not in state["context"]["project_md"]


async def test_forget_resets_transcript_draft_workspace_and_queued_worker(chat):
    await message(chat, "remember viridis", "1.0")
    old = await message(chat, "What do I prefer?", "2.0")
    pointer = access.checkpoint_key(old["thread_key"])
    state = json.loads(chat.store.get(pointer)[0])
    old_path = state["context"]["workdir"]
    state["turn"] = 7
    state["archive"] = "old-memory.tar.gz"
    state["thread_files"] = [{"id": "private-file"}]
    state["context"]["session_id"] = "old-session"
    state["context"]["project_md"] = "memory-derived-private-draft"
    chat.store.put(pointer, json.dumps(state).encode())
    grant = access.resolve(chat.app, chat.settings, chat.store, old)
    request = dict(
        protocol=2,
        principal=grant.email,
        private=True,
        project_id=grant.project_id,
        payload=old["payload"],
        access_signature=grant.signature,
    )
    assert access.worker_permitted(chat.store, request, state)
    await message(chat, "forget all", "3.0")
    assert not access.worker_permitted(chat.store, request, state)
    await message(chat, "Continue", "4.0", thread="2.0")
    supplied = chat.decision.call_args.args[2]
    assert supplied["recent_messages"] == [] and not supplied["user_memory"]
    assert "memory-derived" not in supplied["brief"]
    fresh = json.loads(chat.store.get(pointer)[0])
    assert fresh["context"]["workdir"] != old_path
    assert fresh["thread_files"] == []
    retired_key = chat.store.keys(f"projects/_threads/{old['thread_key']}/retired/")[0]
    assert json.loads(chat.store.get(retired_key)[0])["thread_files"] == [{"id": "private-file"}]
    assert fresh["turn"] == 7  # Subsequent runs cannot overwrite old output provenance.
    assert chat.store.keys(f"projects/_threads/{old['thread_key']}/retired/")
    assert not fresh.get("archive") and fresh["context"]["session_id"] is None


async def test_editor_runs_once_best_effort_and_filters_revoked_content(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    editor = AsyncMock(
        return_value=mem.Edits(edits=[mem.Edit(op="block_append", block="preferences", text="PNG")])
    )
    post = AsyncMock()
    monkeypatch.setattr(mem, "edit", editor)
    monkeypatch.setattr(mem, "post", post)
    record = await message(chat, "I always prefer PNG", "1.0")
    assert editor.await_count == 1 and post.await_count == 1
    assert "PNG" in mem.load(chat.store, "alice@example.com").blocks["preferences"].text
    event = next(iter(mem.load(chat.store, "alice@example.com").applied_events)).removeprefix(
        "auto:"
    )
    await mem.after_turn(chat, event, record, chat.store, "Updated.")
    assert editor.await_count == 1
    editor.side_effect = RuntimeError("failed")
    with pytest.raises(RuntimeError, match="failed"):
        await message(chat, "Another preference", "2.0")
    await message(chat, "memory", "3.0")
    assert editor.await_count == 2


def test_regrant_does_not_revive_old_access_signature():
    doc = RegistryDoc()
    pid = doc.create("alice@example.com", "liver", "gs://lab-data/liver/")
    doc.share(pid, "alice@example.com", "bob@example.com")
    grant = access.Grant("bob@example.com", True, {}, doc, pid, True, "T1:D2")
    previous = grant.signature
    doc.share(pid, "alice@example.com", "bob@example.com", remove=True)
    doc.share(pid, "alice@example.com", "bob@example.com")
    assert grant.signature != previous
    assert user_key("alice@example.com") != user_key("bob@example.com")


@pytest.mark.parametrize("change", ["replace", "forget", "init", "share"])
async def test_routine_changes_preserve_thread_and_inflight_worker(chat, change):
    await message(chat, "remember viridis", "1.0")
    first = await message(chat, "Continue the analysis", "2.0")
    pointer = access.checkpoint_key(first["thread_key"])
    state = json.loads(chat.store.get(pointer)[0])
    state["context"]["session_id"] = "keep-session"
    state["thread_files"] = [{"id": "keep-file"}]
    chat.store.put(pointer, json.dumps(state).encode())
    grant = access.resolve(chat.app, chat.settings, chat.store, first)
    request = dict(
        protocol=2,
        principal=grant.email,
        private=True,
        project_id=grant.project_id,
        payload=first["payload"],
        access_signature=grant.signature,
        thread_key=first["thread_key"],
        conversation_epoch=state["conversation_epoch"],
    )
    if change == "replace":
        memory = mem.load(chat.store, grant.email)
        memory.blocks["preferences"].text = "PNG"
        chat.store.put(mem.key(grant.email), memory.model_dump_json().encode())
        mem.apply(
            chat.store,
            grant.email,
            "replace",
            mem.Edits(
                edits=[mem.Edit(op="block_replace", block="preferences", old="PNG", text="SVG")]
            ),
            expected_epoch=0,
            doc=grant.doc,
        )
    elif change == "forget":
        mem.command(Command("forget", ("1",)), grant, chat.store, "forget-note")
    else:
        other = seed_project(chat.store, name="other", channel="D2", owner="bob@example.com")
        if change == "share":
            Registry(chat.store).update(
                "other-share",
                "bob@example.com",
                "share",
                lambda d: d.share(other, "bob@example.com", grant.email),
            )
        else:
            seed_project(chat.store, name="another", channel="D3")
    current = access.resolve(chat.app, chat.settings, chat.store, first)
    assert current.signature == grant.signature
    fresh = access.fresh_private_state(chat.store, current, chat.settings, first["thread_key"])
    assert fresh == state
    assert not chat.store.keys(f"projects/_threads/{first['thread_key']}/retired/")
    assert access.worker_permitted(chat.store, request, fresh, settings=chat.settings)


async def test_dm_manifest_contains_only_own_memory_channel_manifest_is_empty(chat):
    await message(chat, "remember PERSONAL-MEMORY", "1.0")
    chat.decision.return_value = direct.Decision(route="compute", reply="Computing")
    record = await message(chat, "Plot data", "2.0")
    manifests = chat.store.keys("tasks/slack-analysis/events")
    request = json.loads(
        chat.store.get(next(k for k in manifests if k.endswith("request.json")))[0]
    )
    assert "PERSONAL-MEMORY" in request["user_memory"]
    assert request["run_prefix"].startswith("projects/_users/" + user_key("alice@example.com"))
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert access.worker_permitted(chat.store, request, grant.state, settings=chat.settings)
    forged = json.loads(json.dumps(grant.state))
    forged["context"]["workdir"] = "/srv/pistachio/work/another-user"
    assert not access.worker_permitted(chat.store, request, forged, settings=chat.settings)
    seed_project(chat.store, name="channel", channel="C1", publish=True)
    await message(chat, "Plot data", "3.0", channel="C1")
    manifests = chat.store.keys("tasks/slack-analysis/events")
    requests = [json.loads(chat.store.get(k)[0]) for k in manifests if k.endswith("request.json")]
    public = next(r for r in requests if not r["private"])
    assert not public["user_memory"] and not public["run_prefix"]


async def test_editor_input_hides_revoked_project_blocks_and_notes(chat, monkeypatch):
    source = seed_project(chat.store, name="source", owner="bob@example.com", channel="D2")
    registry = Registry(chat.store)
    registry.update(
        "share-src",
        "bob@example.com",
        "share",
        lambda d: d.share(source, "bob@example.com", "alice@example.com"),
    )
    mem.apply(
        chat.store,
        "alice@example.com",
        "hidden",
        mem.Edits(
            edits=[
                mem.Edit(op="note_add", text="REVOKED-NOTE"),
                mem.Edit(op="block_append", block="profile", text="REVOKED-BLOCK"),
            ]
        ),
        project_id=source,
        expected_epoch=0,
        doc=registry.load(),
    )
    await message(chat, "Describe conventions", "1.0")
    assert "REVOKED-NOTE" in chat.decision.call_args.args[2]["user_memory"]
    registry.update(
        "unshare-src",
        "bob@example.com",
        "unshare",
        lambda d: d.share(source, "bob@example.com", "alice@example.com", remove=True),
    )
    editor = AsyncMock(return_value=mem.Edits())
    monkeypatch.setattr(mem, "edit", editor)
    chat.settings.memory_auto_edit = True
    await message(chat, "Continue", "2.0", thread="1.0")
    assert "REVOKED" not in json.dumps(chat.decision.call_args.args[2])
    assert "REVOKED" not in json.dumps(editor.call_args.args[2])
    record = await message(chat, "memory", "3.0")
    assert "content hidden" in record["notice_text"] and "REVOKED" not in record["notice_text"]
