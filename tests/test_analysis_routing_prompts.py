"""Reviewed prompt snapshot for the coordinated routing/memory release."""
import json
from pathlib import Path

from pistachio.agents.analysis import direct, knowledge
from pistachio.agents.analysis.config import Settings
from pistachio.agents.analysis.prompts import build_system_append
from pistachio.agents.analysis.runner import Context


def prompt_snapshot():
    settings = Settings(_env_file=None)
    ctx = Context(Path("/workspace"), Path("/data"), project_md="Fixture shared guide")
    return {
        "router": direct.SYSTEM + direct.WORKER_POLICY + "\n" + knowledge.ROUTER_RULES,
        "route_schema": direct.Decision.model_json_schema(),
        "worker": build_system_append(ctx, settings),
    }


def test_routing_and_memory_prompt_snapshot():
    expected = Path(__file__).parent / "fixtures/compute/prompts-routing-memory.json"
    assert prompt_snapshot() == json.loads(expected.read_text())
