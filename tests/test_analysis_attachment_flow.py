"""Exercise upload → direct reply → worker → direct follow-up, entirely offline."""

# ruff: noqa: F811
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from google.api_core.exceptions import NotFound
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import (  # noqa: F401
    attached,
    attachment_message,
    chat,
    thread_state,
    upload_http,
)

from mica.agents.analysis import batch_worker, conversation
from mica.agents.analysis.runner import TurnResult


@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("note", ["missing", "stale", "updated"])
async def test_direct_compute_direct_files_survive(
    chat, upload_http, tmp_path, monkeypatch, expired, note
):  # noqa: F811
    from test_analysis_attachments import PNG

    home = tmp_path / "worker"
    home.mkdir()
    chat.settings.work_root = home / "work"
    chat.settings.state_db = home / "state.db"
    from pydantic import SecretStr

    chat.settings.anthropic_api_key = SecretStr("fake")
    chat.settings.slack_bot_token = SecretStr("fake")
    first = await attachment_message(chat, [attached("data.csv")], "Here is my table")
    assert await chat.process(first, 0) == "complete"
    first_record, state = await thread_state(chat, first)
    original = state["thread_files"][0]
    chat.decision.return_value = conversation.Decision(route="compute", reply="Making the plot")
    compute = await chat.accept(payload("2.0", "1.0", "plot B against A"))
    assert await chat.process(compute, 0) == "running"
    _, before = await thread_state(chat, compute)
    methods_path = Path(before["context"]["workdir"]) / "notes/methods.md"
    methods_text = "Panel 1 denominator: AtoMx export cells. " + "x" * 2200
    if note == "stale":
        methods_path.parent.mkdir(parents=True, exist_ok=True)
        methods_path.write_text(methods_text)
    calls = []

    class Runner:
        knowledge_reads = 0

        async def run_turn(self, ctx, prompt, status):
            assert (ctx.workdir / original["path"]).read_bytes() == b"a,b\n1,2"
            assert "attached earlier by <@U1>" in prompt
            calls.append(prompt)
            path = ctx.workdir / "outputs/plot.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(PNG)
            if note == "updated":
                methods_path.parent.mkdir(parents=True, exist_ok=True)
                methods_path.write_text(methods_text)
            return TurnResult("session", "I plotted B against A.", [path])

    class Blob:
        def __init__(self, key):
            self.key = key

        def download_to_filename(self, path):
            try:
                chat.store.download(self.key, Path(path))
            except FileNotFoundError as exc:
                raise NotFound("expired") from exc

    chat.store.bucket = SimpleNamespace(blob=Blob)
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.get.return_value = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://test")
    )
    monkeypatch.setattr(batch_worker, "Settings", lambda **kwargs: chat.settings)
    monkeypatch.setattr(batch_worker, "GCSStorage", lambda bucket: chat.store)
    monkeypatch.setattr(batch_worker, "check", lambda settings: None)
    monkeypatch.setattr(batch_worker, "Runner", lambda *args, **kwargs: Runner())
    monkeypatch.setattr(batch_worker, "AsyncWebClient", lambda **kwargs: chat.test_slack)
    monkeypatch.setattr(batch_worker.id_token, "fetch_id_token", lambda *args: "identity")
    monkeypatch.setattr(batch_worker.httpx, "AsyncClient", lambda **kwargs: http)
    monkeypatch.setenv("MICA_CONTROL_PLANE_URL", "https://test")
    await batch_worker.main(compute)
    assert calls
    _, saved = await thread_state(chat, compute)
    assert len(saved["thread_files"]) == 2
    output = saved["thread_files"][1]
    assert output["source"] == "agent" and output["kind"] == "image"
    assert output["key"].endswith("/turn-1/outputs/plot.png")
    assert saved["messages"][-1]["outputs"] == ["plot.png"]
    assert saved["messages"][-1]["methods"] == (methods_text[:2000] if note == "updated" else "")
    assert "Panel 1 denominator" not in str(chat.test_slack.mock_calls)
    if expired:
        chat.store.path(saved["archive"]).unlink()
        shutil.rmtree(home)
        from mica.agents.analysis.attachments import materialize

        files = materialize(chat.store, Path(saved["context"]["workdir"]), saved["thread_files"])
        assert len(files) == 2
        assert (Path(saved["context"]["workdir"]) / output["path"]).read_bytes() == PNG
    for key in chat.test_states:
        chat.test_states[key] = "SUCCEEDED"
    assert await chat.process(compute, 1) == "complete"
    chat.decision.return_value = conversation.Decision(
        route="reply", reply="That plot is available."
    )
    followup = await chat.accept(payload("3.0", "1.0", "Which files are in this thread?"))
    assert await chat.process(followup, 0) == "complete"
    listing = chat.decision.call_args.args[5]["attachments"]
    assert [f["id"] for f in listing] == [original["id"], output["id"]]
    assert all(not f["shown"] and not f["current"] for f in listing)
