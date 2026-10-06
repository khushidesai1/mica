"""Conversation-cache isolation, race recovery, reset, and guide-cleanup regressions."""

# ruff: noqa: F811
import json
from unittest.mock import AsyncMock

import pytest
from analysis_helpers import seed_project
from pydantic import ValidationError
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_isolation import message

from pistachio.agents.analysis import access, briefs, direct
from pistachio.agents.analysis import conversation_memory as cm
from pistachio.agents.analysis import user_memory as um
from pistachio.agents.analysis.commands import Command, parse
from pistachio.agents.analysis.prompts import build_system_append
from pistachio.agents.analysis.registry import Registry, read_brief
from pistachio.agents.analysis.runner import Context
from pistachio.agents.analysis.storage import Conflict, LocalStorage

GUIDE = "# Liver\nData: gs://lab-data/liver/\n- processed/: QC-filtered files."
OBSERVATION = "User hypothesis: staining may explain the cluster; not confirmed."


def grant_for(store, pid, *, email="alice@example.com", private=True, channel="T1:D1"):
    doc = Registry(store).load()
    return access.Grant(email, private, {}, doc, pid, doc.can_use(pid, email), channel)


def put_summary(store, grant, thread, summary, event="seed"):
    memory, generation = cm.load(store, grant, thread)
    cm.save(store, grant, thread, memory, generation, event, summary)


def checkpoint(chat, record):
    return json.loads(chat.store.get(access.checkpoint_key(record["thread_key"]))[0])


@pytest.fixture
def editor(chat, monkeypatch):
    chat.settings.memory_auto_edit = True
    chat.decision.return_value = direct.Decision(
        route="reply", reply="That could be worth checking."
    )
    model = AsyncMock(return_value=um.Edits(conversation_summary=OBSERVATION))
    monkeypatch.setattr(um, "edit", model)
    monkeypatch.setattr(um, "post", AsyncMock())
    return model


async def test_observation_updates_silent_cache_not_guide(chat, editor):
    before = read_brief(chat.store, chat.pid)
    record = await message(chat, "Maybe staining explains that cluster", "1.0")
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    summary, _ = cm.load(chat.store, grant, record["thread_key"])
    assert summary.summary == OBSERVATION
    assert checkpoint(chat, record)["context"]["project_md"] == before[0]
    assert read_brief(chat.store, chat.pid) == before
    assert "save brief" not in record["notice_text"].lower()
    assert not um.load(chat.store, grant.email).notes
    um.post.assert_not_awaited()
    await um.after_turn(chat, "replay", record, chat.store, "Retry")
    assert editor.await_count == 1


async def test_person_project_cross_thread_injection_and_worker_snapshot(chat, editor):
    await message(chat, "Maybe staining explains this", "1.0")
    chat.settings.memory_auto_edit = False
    await message(chat, "Continue the comparison", "2.0")
    assert chat.decision.call_args.args[2]["conversation_memory"] == OBSERVATION
    chat.decision.return_value = direct.Decision(route="compute", reply="Computing")
    record = await message(chat, "Plot it", "3.0")
    requests = [
        json.loads(chat.store.get(k)[0])
        for k in chat.store.keys("tasks/slack-analysis/events")
        if k.endswith("request.json")
    ]
    request = requests[-1]
    assert request["conversation_memory"] == OBSERVATION
    assert request["conversation_epoch"] == {"epoch": 0, "owner_epoch": 0}
    state = checkpoint(chat, record)
    ctx = Context(
        workdir=chat.settings.work_root, data_path=chat.settings.data_mount_root, project_md=GUIDE
    )
    prompt = build_system_append(
        ctx, chat.settings, "Prefer PNG", "Liver", request["conversation_memory"]
    )
    assert "# Conversation memory" in prompt and OBSERVATION in prompt
    assert "# Personal preferences" in prompt and "Prefer PNG" in prompt
    assert "conversation_memory" not in state["context"]
    assert not any("memory" in f.get("name", "") for f in state.get("thread_files", []))


async def test_scope_isolation_and_channel_summary_never_edits_personal_memory(chat, editor):
    await message(chat, "Private hypothesis", "1.0")
    chat.settings.memory_auto_edit = False
    await message(chat, "share liver <@U2>", "2.0")
    await message(chat, "switch liver", "3.0", user="U2", channel="D2")
    await message(chat, "What do we know?", "4.0", user="U2", channel="D2")
    assert not chat.decision.call_args.args[2]["conversation_memory"]
    seed_project(chat.store, name="other", channel="D1")
    await message(chat, "What do we know?", "5.0")
    assert not chat.decision.call_args.args[2]["conversation_memory"]
    seed_project(chat.store, name="channel", channel="C1", publish=True)
    chat.settings.memory_auto_edit = True
    editor.return_value = um.Edits(
        conversation_summary="Channel-only observation",
        edits=[um.Edit(op="note_add", text="Never store this personal edit")],
    )
    record = await message(chat, "Channel question", "6.0", channel="C1")
    assert editor.call_args.args[2]["private"] is False
    assert not editor.call_args.args[2]["blocks"] and not editor.call_args.args[2]["notes"]
    assert not um.load(chat.store, "alice@example.com").notes
    chat.settings.memory_auto_edit = False
    await message(chat, "Follow-up", "7.0", channel="C1", thread="6.0")
    assert chat.decision.call_args.args[2]["conversation_memory"] == "Channel-only observation"
    await message(chat, "Other thread", "8.0", channel="C1")
    assert not chat.decision.call_args.args[2]["conversation_memory"]
    visible = await message(chat, "memory context", "9.0", channel="C1", thread="6.0")
    assert "Channel-only observation" in visible["notice_text"]
    assert "memory" not in checkpoint(chat, record)["context"]


async def test_concurrent_summary_regenerates_without_losing_other_thread(chat, editor):
    seen = []

    async def edit(_key, _model, context):
        seen.append(context["conversation_summary"])
        if len(seen) == 1:
            grant = grant_for(chat.store, chat.pid)
            put_summary(chat.store, grant, "another-thread", "Earlier thread: decision A", "other")
            return um.Edits(conversation_summary="This thread: decision B")
        return um.Edits(
            conversation_summary=context["conversation_summary"] + "\nThis thread: decision B"
        )

    editor.side_effect = edit
    record = await message(chat, "Decision B", "1.0")
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    final, _ = cm.load(chat.store, grant, record["thread_key"])
    assert seen == ["", "Earlier thread: decision A"]
    assert "decision A" in final.summary and "decision B" in final.summary
    assert final.applied_events.count("other") == 1
    assert len(final.applied_events) == 2


async def test_conflicts_retry_twice_then_keep_latest_and_complete_reply(chat, editor, monkeypatch):
    save = cm.save

    # Avoid recursively intercepting the competing writer.
    def conflict(*args):
        storage, grant, thread = args[:3]
        memory, generation = cm.load(storage, grant, thread)
        save(storage, grant, thread, memory, generation, "competitor", "Concurrent update")
        raise Conflict("changed")

    monkeypatch.setattr(cm, "save", conflict)
    record = await message(chat, "Observe", "1.0")
    assert record["status"] == "complete" and editor.await_count == 3
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert cm.load(chat.store, grant, record["thread_key"])[0].summary == "Concurrent update"


@pytest.mark.parametrize("channel", ["D1", "C1"])
async def test_clear_bypasses_running_turn_and_rejects_old_queue_and_worker(chat, editor, channel):
    if channel == "C1":
        seed_project(chat.store, name="public", channel=channel, publish=True)
    first = await message(chat, "Observation", "1.0", channel=channel)
    chat.settings.memory_auto_edit = False
    chat.decision.return_value = direct.Decision(route="compute", reply="Computing")
    running = await message(chat, "Make a plot", "2.0", channel=channel, thread="1.0")
    request_key = next(
        k for k in chat.store.keys("tasks/slack-analysis/events") if k.endswith("request.json")
    )
    request = json.loads(chat.store.get(request_key)[0])
    grant = access.resolve(chat.app, chat.settings, chat.store, running)
    state = grant.state
    state["context"]["session_id"] = "old-session"
    chat.store.put(access.checkpoint_key(running["thread_key"]), json.dumps(state).encode())
    assert access.worker_permitted(chat.store, request, state, settings=chat.settings)
    queued_id = await chat.accept(
        payload(
            "3.0",
            "1.0",
            "Old queued request",
            channel=channel,
            channel_type="im" if channel == "D1" else "channel",
        )
    )
    cleared = await message(chat, "forget context", "4.0", channel=channel, thread="1.0")
    assert cleared["status"] == "complete" and "cleared" in cleared["notice_text"]
    assert not access.worker_permitted(chat.store, request, state, settings=chat.settings)
    # Simulate completion of the older worker so its queued successor can be considered.
    running["status"] = "failed"
    run_id = next(
        k
        for k, v in chat.repository.connector_state.items()
        if k.startswith("claude-event:") and v["payload"]["event"]["ts"] == "2.0"
    )
    await chat.repository.set_connector_state(run_id, running)
    await chat.process(queued_id, 1)
    queued = await chat.repository.get_connector_state("claude-event:" + queued_id)
    assert "Memory changed" in queued["notice_text"]
    chat.decision.return_value = direct.Decision(route="reply", reply="Fresh.")
    fresh = await message(chat, "Start fresh", "5.0", channel=channel, thread="1.0")
    supplied = chat.decision.call_args.args[2]
    assert not supplied["conversation_memory"] and not supplied["recent_messages"]
    assert checkpoint(chat, fresh)["context"]["session_id"] is None
    assert checkpoint(chat, fresh)["context"]["workdir"] != state["context"]["workdir"]
    assert checkpoint(chat, fresh)["thread_files"] == []
    assert read_brief(chat.store, grant.project_id)[0]
    assert first["status"] == "complete"


async def test_clearing_during_editor_prevents_resurrection(chat, editor):
    async def clear_while_editing(*_args):
        grant = grant_for(chat.store, chat.pid)
        cm.command(Command("forget context"), grant, chat.store, "any-dm-thread", "clear")
        return um.Edits(conversation_summary=OBSERVATION)

    editor.side_effect = clear_while_editing
    record = await message(chat, "Observation", "1.0")
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert not cm.load(chat.store, grant, record["thread_key"])[0].summary
    assert record["status"] == "complete"


async def test_forget_all_retires_caches_for_every_project(chat, editor):
    await message(chat, "Observation", "1.0")
    pid2 = seed_project(chat.store, name="other", channel="D1")
    await message(chat, "Other observation", "2.0")
    await message(chat, "forget all", "3.0")
    for pid in [chat.pid, pid2]:
        grant = grant_for(chat.store, pid)
        assert not cm.load(chat.store, grant, "new-thread")[0].summary
    # An old snapshot cannot be committed after the global forget.
    with pytest.raises(PermissionError):
        cm.save(chat.store, grant, "new-thread", cm.Memory(), None, "stale", OBSERVATION)


async def test_revocation_during_editor_discards_proposal(chat, editor):
    await message(chat, "share liver <@U2>", "0.1")
    await message(chat, "switch liver", "0.2", user="U2", channel="D2")

    async def revoke(*_args):
        Registry(chat.store).update(
            "revoke",
            "alice@example.com",
            "remove",
            lambda d: d.share(chat.pid, "alice@example.com", "bob@example.com", remove=True),
        )
        return um.Edits(conversation_summary=OBSERVATION)

    editor.side_effect = revoke
    record = await message(chat, "Observation", "1.0", user="U2", channel="D2")
    grant = access.resolve(chat.app, chat.settings, chat.store, record)
    assert not grant.allowed
    with pytest.raises(PermissionError):
        cm.load(chat.store, grant, record["thread_key"])
    assert record["status"] == "complete"


def test_size_validation_cas_and_bounded_receipts(tmp_path):
    store = LocalStorage(tmp_path)
    pid = seed_project(store)
    grant = grant_for(store, pid)
    with pytest.raises(ValidationError):
        um.Edits(conversation_summary="x" * 6001)
    with pytest.raises(ValueError):
        cm.save(store, grant, "a", cm.Memory(), None, "bad", "password=abc123")
    for i in range(260):
        put_summary(store, grant, "a", ("x" * 6000) if i == 0 else "Corrected observation", str(i))
    summary, generation = cm.load(store, grant, "b")
    assert len(summary.applied_events) == 256
    assert summary.summary == "Corrected observation" and summary.epoch == 0
    cm.command(Command("forget context"), grant, store, "b", "clear")
    with pytest.raises(Conflict):
        cm.save(store, grant, "b", summary, generation, "stale", "Old observation")
    cm.command(Command("forget context"), grant, store, "b", "clear")
    assert cm.load(store, grant, "b")[0].epoch == 1


async def test_cleanup_is_review_only_preserves_original_and_survives_retry(chat):
    original, generation = read_brief(chat.store, chat.pid)
    chat.decision.return_value = direct.Decision(
        route="reply", reply="Guide draft ready.", draft=GUIDE, brief_cleanup=["research TODOs"]
    )
    record = await message(chat, "clean brief", "1.0")
    state = checkpoint(chat, record)
    assert chat.store.get(state["brief_cleanup"]["original"])[0].decode() == original
    assert state["brief_policy_version"] == briefs.VERSION
    assert state["context"]["brief_generation"] == generation
    assert read_brief(chat.store, chat.pid)[0] == original
    assert "Use brief to review" in record["notice_text"]
    # A delivery retry after the checkpoint write must retain the original backup reference.
    event_id = next(
        k.removeprefix("claude-event:")
        for k, v in chat.repository.connector_state.items()
        if k.startswith("claude-event:") and v["payload"]["event"]["ts"] == "1.0"
    )
    await direct.handle(chat, event_id, record, chat.store)
    assert (
        checkpoint(chat, record)["brief_cleanup"]["original"] == state["brief_cleanup"]["original"]
    )
    await message(chat, "save brief", "2.0", thread="1.0")
    assert read_brief(chat.store, chat.pid)[0] == GUIDE
    assert briefs.reviewed(chat.store, chat.pid, GUIDE)
    chat.decision.return_value = direct.Decision(route="reply", reply="Okay.")
    await message(chat, "New thread", "3.0")
    assert not chat.decision.call_args.args[2]["legacy_brief"]


async def test_needed_file_correction_still_uses_draft_save_flow(chat):
    chat.decision.return_value = direct.Decision(
        route="reply",
        reply="Updated the file guide. Say save brief to share it.",
        draft=GUIDE,
        brief_cleanup=[],
    )
    await message(chat, "processed/ contains the QC-filtered data; use it for analysis", "1.0")
    assert read_brief(chat.store, chat.pid)[0] != GUIDE
    await message(chat, "save brief", "2.0", thread="1.0")
    assert read_brief(chat.store, chat.pid)[0] == GUIDE


@pytest.mark.parametrize("text", ["memory context", "forget context", "clean brief"])
def test_commands_are_explicit(text):
    assert parse(text).name == text


async def test_full_guide_review_is_not_truncated(chat, monkeypatch):
    guide = GUIDE + "\n" + ("- file: verified purpose\n" * 320)
    chat.decision.return_value = direct.Decision(route="reply", reply="Draft ready", draft=guide)
    await message(chat, "Update the file guide", "1.0")
    post = AsyncMock()
    monkeypatch.setattr(direct, "post", post)
    record = await message(chat, "brief", "2.0", thread="1.0")
    shown = record["notice_text"] + "\n" + "\n".join(call.args[3] for call in post.call_args_list)
    assert shown.count("- file: verified purpose") == 320
    assert all(len(call.args[3]) <= 3500 for call in post.call_args_list)
    assert post.await_count == 2


@pytest.mark.parametrize("status,cancelled", [("failed", False), ("complete", True)])
async def test_unsuccessful_turns_skip_memory_editor(chat, editor, status, cancelled):
    chat.settings.memory_auto_edit = False
    record = await message(chat, "Observation", "1.0")
    record.update(status=status, cancel_requested=cancelled)
    chat.settings.memory_auto_edit = True
    await um.after_turn(chat, "failed", record, chat.store, "Partial output")
    editor.assert_not_awaited()


async def test_editor_failure_keeps_previous_cache(chat, editor):
    grant = grant_for(chat.store, chat.pid)
    put_summary(chat.store, grant, "earlier", "Existing decision")
    editor.side_effect = RuntimeError("model unavailable")
    record = await message(chat, "Another observation", "1.0")
    assert record["status"] == "complete"
    assert cm.load(chat.store, grant, record["thread_key"])[0].summary == "Existing decision"


async def test_compute_cleanup_keeps_a_review_notice(chat, monkeypatch):
    post = AsyncMock()
    monkeypatch.setattr(direct, "post", post)
    chat.decision.return_value = direct.Decision(
        route="compute", reply="Computing.", draft=GUIDE, brief_cleanup=["hypotheses"]
    )
    record = await message(chat, "Plot the samples", "1.0")
    assert record["status"] == "running" and record["brief_cleanup_notified"]
    assert "Use brief to review" in post.call_args.args[3]
    assert checkpoint(chat, record)["brief_cleanup"]["removed"] == ["hypotheses"]
