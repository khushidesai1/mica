"""Offline API/worker gate for project coordination; no paid model or scientific work."""

import asyncio
import tempfile
import time
from pathlib import Path

from mica.agents.analysis import backchannel
from mica.agents.analysis.backchannel_worker import Client
from mica.agents.analysis.config import Settings
from mica.agents.analysis.storage import LocalStorage


async def check():
    with tempfile.TemporaryDirectory(prefix="mica-backchannel-") as temp:
        work = Path(temp)
        store = LocalStorage(work / "cloud")
        message = backchannel.Call(action="publish", topic="method", kind="recipe",
                                   text="Synthetic method v1; seed 7; evidence fixture-1.")
        receipt = backchannel.publish(store, "synthetic", "investigator", "event-a", 0, message)
        assert receipt == backchannel.publish(
            store, "synthetic", "investigator", "event-a", 0, message
        )
        assert not backchannel.read(store, "other", backchannel.Call())["messages"]

        async def transport(args):
            return backchannel.read(store, "synthetic", backchannel.Call.model_validate(args))

        client = Client(transport, "consumer")
        assert receipt["id"] in await client.poll()
        assert await client.poll(force=True) == ""
        try:
            from mica.agents.analysis.recovery_tools import RecoveryManager
            from mica.agents.analysis.resources import Sampler
            from mica.agents.analysis.runner import Context, build_options
        except ModuleNotFoundError as exc:
            if exc.name != "claude_agent_sdk":
                raise
            from mica.api.slack_analysis import BackchannelRequest

            assert BackchannelRequest(attempt=0, call=message).call.kind == "recipe"
        else:
            settings = Settings(_env_file=None)
            compute = RecoveryManager(settings, {
                "attempt": 0, "tier": "standard", "request_deadline": time.time() + 60,
                "provisioning": "STANDARD",
            }, Sampler(work), transport)
            compute.backchannel = Client(transport, "consumer")
            options = build_options(Context(work, work / "data"), settings,
                                    compute_manager=compute)
            assert "mcp__pistachio__project_backchannel" in options.allowed_tools
            guard = options.hooks["PreToolUse"][0].hooks[0]
            result = await guard({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                  "tool_input": {"command": "true"}}, None, {})
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert receipt["id"] in result["hookSpecificOutput"]["permissionDecisionReason"]
    print("PROJECT_BACKCHANNEL_CONTRACT_PASSED", flush=True)


asyncio.run(check())
