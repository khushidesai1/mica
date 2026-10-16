"""Cold process handoff through the worker, SDK adapter and final delivery path."""

import json
import shutil
import time
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from test_analysis_runner import result

from mica.agents.analysis import batch_worker, runner
from mica.agents.analysis.config import Settings
from mica.agents.analysis.recovery import event_prefix
from mica.agents.analysis.runner import Context
from mica.agents.analysis.storage import LocalStorage


@pytest.mark.parametrize(
    "stop_reason", [None, "access_unverifiable", "access_revoked", "cancelled"]
)
async def test_cold_resize_suppresses_intermediate_delivery_and_preserves_outputs(
    tmp_path, monkeypatch, stop_reason
):
    home = tmp_path / "worker"
    home.mkdir()
    settings = Settings(
        _env_file=None,
        storage="local",
        anthropic_api_key="fake",
        slack_bot_token="fake",
        work_root=home / "work",
        state_db=home / "state.db",
    )
    store = LocalStorage(tmp_path / "gcs")

    class Blob:
        def __init__(self, key):
            self.key = key

        def download_to_filename(self, target):
            shutil.copyfile(store.path(self.key), target)

    store.bucket = SimpleNamespace(blob=Blob)
    slack = AsyncMock()
    slack.chat_postMessage.return_value = {"ts": "9.0"}
    slack.files_upload_v2.return_value = {"files": []}
    slack.users_info.return_value = {"user": {"real_name": "Test"}}
    http = AsyncMock()
    http.__aenter__.return_value = http
    http.get.return_value = httpx.Response(
        200, json={}, request=httpx.Request("GET", "https://test")
    )
    http.post.return_value = httpx.Response(
        200, json={"status": "checkpoint"}, request=httpx.Request("POST", "https://test")
    )
    monkeypatch.setattr(batch_worker, "Settings", lambda **kw: settings)
    monkeypatch.setattr(batch_worker, "GCSStorage", lambda _: store)
    monkeypatch.setattr(batch_worker, "check", lambda _: None)
    monkeypatch.setattr(batch_worker, "worker_permitted", lambda *a, **kw: True)
    monkeypatch.setattr(batch_worker, "AsyncWebClient", lambda **kw: slack)
    monkeypatch.setattr(batch_worker.id_token, "fetch_id_token", lambda *a: "fake")
    monkeypatch.setattr(batch_worker.httpx, "AsyncClient", lambda **kw: http)
    monkeypatch.setenv("MICA_CONTROL_PLANE_URL", "https://test")
    ctx = Context(home / "work/test", home / "data", "test", "Brief", thread_ts="1.0")
    ctx.workdir.mkdir(parents=True)
    ctx.data_path.mkdir()
    pointer = "projects/_threads/thread/checkpoint.json"
    initial = json.dumps(
        {"context": asdict(ctx), "turn": 6, "is_init": False}, default=str
    ).encode()
    store.put(pointer, initial)
    eid = "a" * 32
    prefix = event_prefix(eid)
    request = {
        "thread_key": "thread",
        "private": False,
        "principal": "test@example.com",
        "project_id": "test",
        "notice_ts": "9.0",
        "payload": {
            "event": {
                "channel": "D1",
                "user": "U1",
                "ts": "2.0",
                "thread_ts": "1.0",
                "text": "Create two reports",
            }
        },
        "recovery": {
            "attempt": 0,
            "tier": "standard",
            "provisioning": "SPOT",
            "request_deadline": time.time() + 3600,
        },
        "turn_start": json.loads(initial),
    }
    prompts = []

    async def stream(prompt, options, manager):
        prompts.append(prompt)
        if stop_reason:
            manager.request_stop(stop_reason)
            yield result(total_cost_usd=0.1, result="Interrupted; not a completed result")
            return
        work = Path(options.cwd)
        if manager.attempt == 0:
            (work / "outputs/first.txt").write_text("first result")
            (work / "knowledge/PROJECT.md").write_text("# Verified shared guide")
            (work / "scratch/temporary.txt").write_text("not retained")
            (work / "checkpoints/ingest").mkdir()
            (work / "checkpoints/ingest/complete.txt").write_text("closed stage")
            await manager.save_stage(
                {"path": "checkpoints/ingest", "fingerprint": "synthetic-input-v1"}
            )
            await manager.call(
                {
                    "cause": "capacity",
                    "memory_gib": 100,
                    "evidence": "Measured working arrays",
                    "handoff": "First done; create second.",
                }
            )
        else:
            assert options.resume == "session-1"
            assert (work / "knowledge/PROJECT.md").read_text() == "# Verified shared guide"
            assert (work / "outputs/first.txt").read_text() == "first result"
            assert not (work / "scratch/temporary.txt").exists()
            assert (work / "checkpoints/ingest/complete.txt").read_text() == "closed stage"
            (work / "outputs/second.txt").write_text("second result")
        yield result(total_cost_usd=0.1 * (manager.attempt + 1), result="Both results verified.")

    monkeypatch.setattr(runner, "recovery_query", stream)
    store.put(f"{prefix}/request.json", json.dumps(request).encode())
    await batch_worker.main(eid)
    assert store.get(pointer)[0] == initial
    slack.files_upload_v2.assert_not_called()
    slack.chat_postMessage.assert_not_called()
    handoff = json.loads(store.get(f"{prefix}/attempt-a0.json")[0])
    if stop_reason:
        assert handoff["outcome"] == "stopped"
        assert handoff["stop_reason"] == stop_reason
        assert not list(store.keys("projects/test/knowledge/"))
        return
    pending = json.loads(store.get(handoff["pending_memory"]["key"])[0])
    assert pending["changes"] == {"PROJECT.md": "# Verified shared guide"}
    assert not list(store.keys("projects/test/knowledge/"))
    assert handoff["outcome"] == "handoff" and handoff["state"]["turn"] == 7
    assert handoff["stages"]["stages"][0]["fingerprint"] == "synthetic-input-v1"
    shutil.rmtree(home)
    home.mkdir()
    request["recovery"].update(attempt=1, tier="large", provisioning="STANDARD")
    request["resume"] = {**handoff, "handoff": "First done; create second."}
    store.put(f"{prefix}/request.json", json.dumps(request).encode())
    await batch_worker.main(eid)
    final = json.loads(store.get(pointer)[0])
    assert final["turn"] == 7 and len(final["messages"]) == 1
    assert final["messages"][0]["id"] == eid
    assert json.loads(store.get(f"{prefix}/attempt-a1.json")[0])["outcome"] == "complete"
    assert json.loads(store.get(f"{prefix}/attempt-a1.json")[0])["model_cost"] == 0.1
    assert slack.files_upload_v2.call_count == 1
    assert {
        Path(row["file"]).name for row in slack.files_upload_v2.call_args.kwargs["file_uploads"]
    } == {"first.txt", "second.txt"}
    assert slack.chat_postMessage.call_count == 1
    assert "First done; create second." in prompts[1]
