"""Models select complete snapshot entries, within a shared deadline and read budget."""

import asyncio
import json

import httpx
import pytest
from test_memory_editor import mock_model, model_response, snapshot

from mica.agents.analysis import memory_reads, memory_tasks


def read_response(paths, call_id="read-1"):
    return {"content": [{
        "type": "tool_use", "id": call_id, "name": "read_memory_files",
        "input": {"paths": paths},
    }], "stop_reason": "tool_use"}


async def test_correction_reads_target_without_shared_words(monkeypatch):
    original = "# Feature filter\nExclude cells with fewer than 200 detected genes."
    updated = original.replace("200", "250")
    base = snapshot({"INDEX.md": "- [results/x.md](results/x.md): Feature filter\n",
                     "results/x.md": original})
    context = {
        "user": "Correction: make that lower bound 250.",
        "files": {"INDEX.md": base.files["INDEX.md"].text},
        "existing_paths": list(base.files),
    }
    requests = []
    mock_model(monkeypatch, [read_response(["results/x.md"]),
                            model_response({"results/x.md": updated})], requests)
    changes = await memory_tasks.edit("test", "test", context, snapshot=base)
    assert set(changes) == {"results/x.md", "_thread"}
    assert changes["results/x.md"].endswith(updated)
    assert "results/x.md" not in context["files"]
    assert json.loads(requests[1]["messages"][-1]["content"][0]["content"]) == {
        "results/x.md": original,
    }


@pytest.mark.parametrize("budget,reason", [(10, "context_limit"), (32000, "unavailable")])
async def test_failed_read_does_not_authorize_replacement(monkeypatch, budget, reason):
    base = snapshot({"results/x.md": "SECRET TAIL " * 100}) if reason == "context_limit" else None
    requests = []
    mock_model(monkeypatch, [read_response(["results/x.md"]),
                            model_response({"results/x.md": "replacement"})], requests)
    with pytest.raises(memory_tasks.EditorError, match="unseen_file"):
        await memory_tasks.edit("test", "test", {
            "files": {}, "existing_paths": ["results/x.md"],
        }, snapshot=base, budget=budget)
    result = json.loads(requests[1]["messages"][-1]["content"][0]["content"])
    assert result == {"results/x.md": {"error": reason}}
    assert "SECRET TAIL" not in json.dumps(requests)


async def test_two_read_rounds_then_forced_final_with_cumulative_budget(monkeypatch):
    base = snapshot({"data/a.md": "first", "data/b.md": "second"})
    requests = []
    mock_model(monkeypatch, [read_response(["data/a.md"]),
                            read_response(["data/b.md"], "read-2"), model_response()], requests)
    await memory_tasks.edit("test", "test", {"files": {}}, snapshot=base,
                            budget=memory_reads.chars({"data/a.md": "first"}) + 2)
    assert len(requests) == 3
    assert [r["tool_choice"]["type"] for r in requests] == ["any", "any", "tool"]
    assert [t["name"] for t in requests[2]["tools"]] == ["edit_project_memory"]
    result = json.loads(requests[2]["messages"][-1]["content"][0]["content"])
    assert result == {"data/b.md": {"error": "context_limit"}}


async def test_read_schema_caps_paths_and_rejects_extra_round(monkeypatch):
    requests = []
    mock_model(monkeypatch, [read_response(["data/a.md"] * 9), read_response(["data/a.md"]),
                            read_response(["data/a.md"])], requests)
    with pytest.raises(memory_tasks.EditorError, match="incomplete_output"):
        await memory_tasks.edit("test", "test", {"files": {}})
    result = json.loads(requests[1]["messages"][-1]["content"][0]["content"])
    assert result == {"error": "invalid_paths", "max_paths": 8}
    assert requests[0]["tools"][1]["input_schema"]["properties"]["paths"]["maxItems"] == 8


async def test_one_deadline_covers_every_request(monkeypatch):
    requests = []
    factory = httpx.AsyncClient

    async def respond(request):
        requests.append(request)
        await asyncio.sleep(0.04)
        return httpx.Response(200, json=read_response(["data/a.md"]))

    monkeypatch.setattr(memory_reads.httpx, "AsyncClient", lambda **kwargs: factory(
        transport=httpx.MockTransport(respond),
    ))
    with pytest.raises(TimeoutError):
        await memory_reads.run(
            "test", "test", {"files": {}}, system="test",
            final_tool={"name": "finish"}, files={"data/a.md": "test"},
            remaining_chars=1000, timeout=0.06,
        )
    assert len(requests) == 2
