# ruff: noqa: F811
"""Golden behavior captured from 711a15f before adding script return."""

import json
from pathlib import Path

from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_slack import bot, event, send  # noqa: F401

from mica.agents.analysis.config import Settings
from mica.agents.analysis.runner import Context, build_options

FIXTURES = Path(__file__).parent / "fixtures/return-scripts"


def golden(name, value):
    path = FIXTURES / name
    assert value == path.read_text()


def test_off_prompt_and_hooks():
    ctx = Context(Path("/workspace"), Path("/data"), project_md="Fixture project")
    for knowledge in (True,):
        opts = build_options(ctx, Settings(_env_file=None))
        golden(f"prompt-{knowledge}.txt", opts.system_prompt["append"])
        hooks = {
            k: [{"matcher": h.matcher, "hooks": [f.__name__ for f in h.hooks]} for h in v]
            for k, v in opts.hooks.items()
        }
        golden(f"hooks-{knowledge}.json", json.dumps(hooks, indent=2))


async def test_off_delivery_and_provenance(bot):
    await send(bot, event("plot"))
    work = bot.sessions.get("C123", "1.0")[0].workdir
    data = {
        "reply": bot.last_reply,
        "messages": [c.kwargs for c in bot.client.chat_postMessage.call_args_list],
        "uploads": [
            {**u, "file": str(Path(u["file"]).relative_to(work))}
            for u in bot.client.files_upload_v2.call_args.kwargs["file_uploads"]
        ],
        "thread_files": bot.delivered,
        "provenance": {k: bot.storage.get(k)[0].decode() for k in bot.storage.keys("projects/")},
    }
    golden("delivery.json", json.dumps(data, indent=2, sort_keys=True))


async def test_off_request_manifest(bridge, monkeypatch):
    from mica.agents.analysis import batch

    monkeypatch.setattr(batch.time, "time", lambda: 1700000000.0)
    eid = await bridge.accept(payload())
    assert await bridge.process(eid, 0) == "running"
    raw = bridge.store.get(f"tasks/slack-analysis/events/{eid}/request.json")[0].decode()
    value = json.loads(raw)
    for key in ("project_id", "access_signature"):
        raw = raw.replace(value[key], "<" + key + ">")
    value = json.loads(raw)
    assert value["event_id"] == eid
    assert value["recovery"]["attempt"] == 0
    assert value["recovery"]["request_deadline"] == 1700000000 + bridge.settings.max_run_seconds
    assert value["memory"]["head_oid"] == bridge.github.head
    value["memory"]["head_oid"] = "<memory_head>"
    for field in ("event_id", "recovery", "resume", "turn_start"):
        value.pop(field)
    assert value == json.loads((FIXTURES / "request.json").read_text())
