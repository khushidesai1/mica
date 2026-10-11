"""Prompt -> editor -> GitHub -> new router/worker context, without network."""

import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import direct, job_lifecycle, memory_tasks, project_memory
from tests.fake_github import FakeGitHub

CONVENTION = (
    "type: user\nsource: alice@example.com, 2026-10-10\nstatus: current\n\n"
    "Alice says this project always uses viridis for heatmaps.\n"
)


@pytest.fixture
def memory_chat(chat, monkeypatch):  # noqa: F811
    github = FakeGitHub()
    chat.settings.memory_repo = "owner/memory"
    chat.settings.memory_auto_edit = True
    backend = project_memory.GitHubMemory(chat.settings, client=github.client)
    monkeypatch.setattr(project_memory, "GitHubMemory", lambda settings: backend)
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
