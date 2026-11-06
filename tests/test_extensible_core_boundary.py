"""Method and provider details belong in registries, skills and source adapters."""

from pathlib import Path

import pytest
from test_analysis_batch import bridge  # noqa: F401

from mica.agents.analysis import job_lifecycle, transfer_state
from mica.agents.analysis.transfers import Transfers


@pytest.mark.parametrize("module", [
    "batch", "conversation", "tiers", "runner", "recovery_tools",
    "email_delivery", "failure_investigation", "ambient",
])
def test_core_modules_do_not_name_incident_specific_methods_or_providers(module):
    source = (Path(__file__).parents[1] / "src/mica/agents/analysis" / (module + ".py"))
    content = source.read_text().casefold()
    for name in ("cellpose", "resolvi", "atomx"):
        assert name not in content, f"{module}: obtain {name} metadata from its registry or adapter"


def test_transfer_values_accept_only_known_legacy_forms():
    assert transfer_state.is_handoff("transfer") and transfer_state.is_handoff("atomx")
    assert not transfer_state.is_handoff({"atomx": True})
    assert not transfer_state.is_handoff("arbitrary")
    expected = ("atomx-" + "a" * 24, "transfer-" + "a" * 24)
    assert transfer_state.mail_stages(expected[0]) == expected
    assert transfer_state.mail_stages(expected[1]) == expected
    assert transfer_state.mail_stages("atomx-../private") == ()


async def test_new_transfer_task_names_are_generic(bridge):  # noqa: F811
    controller = Transfers(bridge, bridge.store)
    await controller.schedule({"id": "a" * 32, "tick": 3}, delay=5)
    args = job_lifecycle.enqueue.call_args.args
    assert args[1] == "transfer-" + "a" * 32 + "-3"
    assert args[2].endswith("/transfers/" + "a" * 32 + "/3")
    assert args[3] == 5
