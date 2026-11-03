"""Reviewed prompt snapshot for the coordinated routing/memory release."""

import json
from pathlib import Path

from mica.agents.analysis import conversation, prompts, tiers
from mica.agents.analysis.config import Settings
from mica.agents.analysis.prompts import build_system_append
from mica.agents.analysis.runner import Context


def prompt_snapshot():
    settings = Settings(_env_file=None)
    ctx = Context(Path("/workspace"), Path("/data"), project_md="Fixture shared guide")
    return {
        "conversation": prompts.CONVERSATION,
        "tools": conversation.tools_for(True),
        "capability_policy": tiers.CAPABILITY_POLICY,
        "worker": build_system_append(ctx, settings),
    }


def test_routing_and_memory_prompt_snapshot():
    expected = Path(__file__).parent / "fixtures/compute/prompts-routing-memory.json"
    assert prompt_snapshot() == json.loads(expected.read_text())
