# ruff: noqa: F811
"""Prompt -> editor -> GitHub -> new router/worker context, without network."""

import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct, job_lifecycle, memory_tasks, project_memory

CONVENTION = (
    "type: user\nsource: alice@example.com, 2026-10-10\nstatus: current\n\n"
    "Alice says this project always uses viridis for heatmaps.\n"
)


@pytest.fixture
def memory_chat(chat, monkeypatch):  # noqa: F811
    github, backend = chat.github, chat.memory_backend
    chat.settings.memory_auto_edit = True
    monkeypatch.setattr(job_lifecycle, "enqueue", AsyncMock())
    chat.decision.return_value = direct.Decision(route="reply", reply="I'll use viridis.")
    chat.github, chat.memory_backend = github, backend
    return chat


async def test_prompt_persists_and_new_thread_and_worker_receive_it(memory_chat, monkeypatch):
    chat = memory_chat
    editor = AsyncMock(return_value={"conventions.md": CONVENTION})
    monkeypatch.setattr(memory_tasks, "edit", editor)
    first = await chat.accept(payload(text="We always use viridis for heatmaps"))
    assert await chat.process(first, 0) == "complete"
    await memory_tasks.process(chat, first, chat.store)
    await memory_tasks.process(chat, first, chat.store)
    editor.assert_awaited_once()
    saved = await chat.memory_backend.read(chat.pid)
    assert saved.files["conventions.md"].text == CONVENTION
    assert "alice@example.com" in editor.call_args.args[2]["speaker"]
    second = await chat.accept(payload("2.0", text="What colormap do we use for heatmaps?"))
    assert await chat.process(second, 0) == "complete"
    assert "viridis" in chat.decision.call_args.args[2]["project_memory"]
    chat.decision.return_value = direct.Decision(route="compute", reply="Plotting.")
    third = await chat.accept(payload("3.0", text="Plot a small heatmap"))
    assert await chat.process(third, 0) == "running"
    manifest = json.loads(chat.store.get(f"tasks/slack-analysis/events/{third}/request.json")[0])
    assert manifest["memory"]["head_oid"] == saved.head_oid
    assert manifest["memory"]["files"]["conventions.md"]["text"] == CONVENTION
    assert "token" not in json.dumps(manifest).lower()
    assert not any("retired/" in key for key in chat.store.keys("projects/"))


async def test_worker_proposal_is_the_only_writer(memory_chat, monkeypatch):
    chat = memory_chat
    chat.decision.return_value = direct.Decision(route="compute", reply="Plotting.")
    event_id = await chat.accept(payload(text="Plot a small heatmap"))
    await chat.process(event_id, 0)
    key = f"tasks/slack-analysis/events/{event_id}/"
    manifest = json.loads(chat.store.get(key + "request.json")[0])
    result = "type: result\nsource: verified analysis\nstatus: current\n\nCount: 12.\n"
    chat.store.put(
        key + "memory-proposal.json",
        json.dumps(
            {
                "base_head": manifest["memory"]["head_oid"],
                "changes": {"results/tma101.md": result},
            }
        ).encode(),
    )
    record = await chat.repository.get_connector_state("claude-event:" + event_id)
    record.update(status="complete", memory_reply="Done.")
    await chat.repository.set_connector_state("claude-event:" + event_id, record)
    editor = AsyncMock()
    monkeypatch.setattr(memory_tasks, "edit", editor)
    await memory_tasks.process(chat, event_id, chat.store)
    editor.assert_not_awaited()
    saved = await chat.memory_backend.read(chat.pid)
    assert saved.files["results/tma101.md"].text == result


async def test_github_outage_keeps_reply_but_retries_memory_task(memory_chat):
    chat = memory_chat
    chat.github.fail = 502
    event_id = await chat.accept(payload(text="We always use viridis for heatmaps"))
    assert await chat.process(event_id, 0) == "complete"
    with pytest.raises(RuntimeError, match="unavailable"):
        await memory_tasks.process(chat, event_id, chat.store)


async def test_worker_workspace_round_trip(tmp_path):
    from pistachio.agents.analysis import memory_workspace
    from pistachio.agents.analysis.config import Settings

    base = project_memory.Snapshot(
        head_oid="a" * 40,
        files={
            "conventions.md": project_memory.File(text=CONVENTION, oid="b" * 40),
        },
    )
    settings = Settings(_env_file=None)
    memory_workspace.materialize(tmp_path, base, settings)
    target = tmp_path / "knowledge/results/tma101.md"
    target.parent.mkdir()
    target.write_text(CONVENTION)
    proposal = memory_workspace.proposal(tmp_path, base, settings)
    assert proposal.changes == {"results/tma101.md": CONVENTION}
    assert proposal.base_head == base.head_oid


async def test_dm_fact_is_shared_with_channel_but_not_another_project(memory_chat, monkeypatch):
    from analysis_helpers import memory_message as message

    chat = memory_chat
    monkeypatch.setattr(
        memory_tasks, "edit", AsyncMock(return_value={"conventions.md": CONVENTION})
    )
    await message(chat, "We always use viridis for heatmaps", "1.0")
    monkeypatch.setattr(memory_tasks, "edit", AsyncMock(return_value={}))
    await message(chat, "publish liver <#C1>", "2.0")
    await message(chat, "use liver", "3.0", channel="C1")
    await message(chat, "What colormap?", "4.0", channel="C1")
    assert "viridis" in chat.decision.call_args.args[2]["project_memory"]
    from analysis_helpers import seed_project

    seed_project(chat.store, name="other", channel="D2")
    await message(chat, "What colormap?", "5.0", channel="D2")
    assert "viridis" not in chat.decision.call_args.args[2]["project_memory"]


async def test_edits_other_init_and_share_keep_older_thread_state(memory_chat, monkeypatch):
    from analysis_helpers import memory_message as message
    from test_analysis_direct import seed_earlier_output, thread_state

    chat = memory_chat
    monkeypatch.setattr(
        memory_tasks, "edit", AsyncMock(return_value={"conventions.md": CONVENTION})
    )
    monkeypatch.setattr(direct, "inventory", lambda *args, **kwargs: {"entries": []})
    output = await seed_earlier_output(chat)
    first = next(
        k.split(":", 1)[1] for k in chat.repository.connector_state if k.startswith("claude-event:")
    )
    record, state = await thread_state(chat, first)
    key = f"projects/_threads/{record['thread_key']}/checkpoint.json"
    state["context"]["session_id"] = "keep-session"
    # Mimic a path from the old epoch-based scheme: it must stay usable.
    state["context"]["workdir"] += "/legacy-hash"
    chat.store.put(key, json.dumps(state).encode())
    await memory_tasks.process(chat, first, chat.store)
    chat.decision.return_value = direct.Decision(
        route="reply", reply="Ready.", guide="# Other guide"
    )
    await message(chat, "init other gs://lab-data/other/ --force", "2.0")
    await message(chat, "share other <@U2>", "3.0")
    chat.decision.return_value = direct.Decision(route="reply", reply="We use viridis.")
    await message(chat, "Which colormap?", "4.0", thread="1.0")
    after = json.loads(chat.store.get(key)[0])
    assert after["context"]["workdir"] == state["context"]["workdir"]
    assert after["context"]["session_id"] == "keep-session"
    assert after["thread_files"] == state["thread_files"]
    assert after["thread_files"][0]["id"] == output.id
    assert after["messages"][:-1] == state["messages"]
    assert not any("retired/" in k for k in chat.store.keys("projects/"))
    assert all(
        "Memory changed" not in str(c) and "Project access changed" not in str(c)
        for c in chat.test_slack.chat_update.call_args_list
    )


async def test_worker_result_materializes_in_next_workspace(memory_chat, monkeypatch, tmp_path):
    await test_worker_proposal_is_the_only_writer(memory_chat, monkeypatch)
    from pistachio.agents.analysis import memory_workspace

    saved = await memory_chat.memory_backend.read(memory_chat.pid)
    memory_workspace.materialize(tmp_path, saved, memory_chat.settings)
    assert "Count: 12" in (tmp_path / "knowledge/results/tma101.md").read_text()


async def test_notice_retry_keeps_commit_receipt_and_deduplicates(memory_chat, monkeypatch):
    chat = memory_chat
    monkeypatch.setattr(
        memory_tasks, "edit", AsyncMock(return_value={"conventions.md": CONVENTION})
    )
    event = await chat.accept(payload(text="We use viridis"))
    await chat.process(event, 0)
    original = chat.store.put
    lost = False

    def put(key, data):
        nonlocal lost
        if key.endswith("memory-editor.json") and b'"saved"' in data and not lost:
            lost = True
            raise TimeoutError("lost acknowledgment")
        return original(key, data)

    monkeypatch.setattr(chat.store, "put", put)
    with pytest.raises(TimeoutError):
        await memory_tasks.process(chat, event, chat.store)
    commits = len(chat.github.commits)
    await memory_tasks.process(chat, event, chat.store)
    await memory_tasks.process(chat, event, chat.store)
    assert len(chat.github.commits) == commits
    notices = [
        c
        for c in chat.test_slack.chat_postMessage.call_args_list
        if "Remembered for this project" in c.kwargs.get("text", "")
    ]
    assert len(notices) == 1 and "conventions.md" in notices[0].kwargs["text"]


async def test_memory_lists_saved_convention_before_first_index(memory_chat, monkeypatch):
    from analysis_helpers import memory_message as message

    chat = memory_chat
    monkeypatch.setattr(
        memory_tasks, "edit", AsyncMock(return_value={"conventions.md": CONVENTION})
    )
    await message(chat, "We use viridis for heatmaps", "1.0")
    shown = await message(chat, "memory", "2.0")
    assert "conventions.md" in shown["notice_text"]
    shown = await message(chat, "memory conventions.md", "3.0")
    assert "viridis" in shown["notice_text"] and "alice@example.com" in shown["notice_text"]
