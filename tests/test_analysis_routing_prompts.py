"""Reviewed prompt snapshot for the coordinated routing/memory release."""
import json
from pathlib import Path

from mica.agents.analysis import command_intent, direct, project_memory
from mica.agents.analysis.config import Settings
from mica.agents.analysis.prompts import build_system_append
from mica.agents.analysis.runner import Context


def prompt_snapshot():
    settings = Settings(_env_file=None)
    ctx = Context(Path("/workspace"), Path("/data"), project_md="Fixture shared guide")
    return {
        "command_intent": command_intent.SYSTEM,
        "command_schema": command_intent.Intent.model_json_schema(),
        "router": direct.SYSTEM + direct.WORKER_POLICY + "\n" + project_memory.ROUTER_RULES,
        "route_schema": direct.Decision.model_json_schema(),
        "worker": build_system_append(ctx, settings),
    }


def test_routing_and_memory_prompt_snapshot():
    expected = Path(__file__).parent / "fixtures/compute/prompts-routing-memory.json"
    assert prompt_snapshot() == json.loads(expected.read_text())
