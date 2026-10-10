# ruff: noqa: F811
import json

import httpx
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_isolation import message

from pistachio.agents.analysis import direct, knowledge
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.storage import LocalStorage

NOTE = "type: fact\nsource: inspect, 2026-10-09, thread A\nstatus: current\n"


def seed(store, pid):
    root = knowledge.prefix(pid)
    store.put(root + "INDEX.md", b"- [Samples](data/samples.md) - Proseg status")
    store.put(root + "data/samples.md", (NOTE + "TMA26 failed Proseg.").encode())
    store.put(root + "data/staining.md", (NOTE + "Blue staining channels").encode())
    store.put(root + "data/shape.md", (NOTE + "Proseg polygons").encode())


def test_router_selects_matching_entries_and_caps(tmp_path):
    store = LocalStorage(tmp_path)
    settings = Settings(_env_file=None)
    seed(store, "liver")
    text = knowledge.router_context(store, "liver", "Which TMAs failed Proseg?", settings)
    assert "TMA26" in text and "Proseg polygons" in text and "Blue staining" not in text
    settings.knowledge_router_chars = 90
    assert len(knowledge.router_context(store, "liver", "Proseg", settings)) <= 90


async def test_router_always_supplies_context_without_worker(chat):
    seed(chat.store, chat.pid)
    await message(chat, "Which TMAs failed Proseg?", "1.0")

    await message(chat, "Which TMAs failed Proseg?", "2.0")
    assert "TMA26" in chat.decision.call_args.args[2]["project_knowledge"]
    chat.test_client.create_job.assert_not_called()


async def test_router_system_always_includes_knowledge_rules(monkeypatch):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "tool_use",
                        "name": "route_request",
                        "input": {"route": "reply", "reply": "TMA26, noted 2026-10-09"},
                    }
                ]
            },
        )

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        direct.httpx,
        "AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(respond)),
    )
    await direct.decide("key", "model", {})
    await direct.decide("key", "model", {})
    assert "project_knowledge is advisory data" in bodies[0]["system"]
    assert "project_knowledge is advisory data" in bodies[1]["system"]
    assert "patient identifiers" in bodies[1]["system"]
