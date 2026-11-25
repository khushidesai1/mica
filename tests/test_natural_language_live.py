"""Opt-in paid phrasing evaluation; every Mica action uses local fixture storage."""

# ruff: noqa: F811
import asyncio
import json
import os
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401

from mica.agents.analysis import access, commands, conversation

CASES = [
    ("projects", "projects"),
    ("Which projects can I see?", "projects"),
    ("List the projects I'm collaborating on", "projects"),
    ("use brain", "use"),
    ("Switch to brain and list its datasets", "use"),
    ("brief", "brief"),
    ("Show me the saved project guide", "brief"),
    ("memory", "memory"),
    ("What can you do?", "help"),
    ("help", "help"),
    ("Include scripts with my results from now on", "scripts"),
    ("Stop attaching code to my replies", "scripts"),
    ("Who owns this project?", "owners"),
    ("Which reference repositories are available for this project?", "repos"),
    ("Give <@U2> access to this project", "share"),
    ("Make bob@example.com a co-owner of liver", "add-owner"),
    ("Rename liver to liver-study", "rename"),
    ("Create kidney using gs://lab-data/kidney/", "init"),
    ("Is liver currently allowed to write files?", "write status"),
    ("Import a Claude Code session and then compare its conclusions", "import session"),
]


@pytest.mark.live
async def test_natural_language_phrasings(bridge, monkeypatch):
    if os.environ.get("MICA_RUN_LIVE_TESTS") != "1" or not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("Requires MICA_RUN_LIVE_TESTS=1 and ANTHROPIC_API_KEY; uses API credits")
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    bridge.settings.slack_streaming = False
    bridge.settings.conversation_max_calls = 4
    from analysis_helpers import seed_project

    seed_project(bridge.store, name="brain", channel="C2", uri="gs://lab-data/beta/")
    rows = []
    semaphore = asyncio.Semaphore(4)
    async with AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=0) as sdk:

        async def evaluate(index, text, expected):
            async with semaphore:
                eid = await bridge.accept(payload(f"{index + 10}.0", text=text))
                record = await bridge.repository.get_connector_state("claude-event:" + eid)
                grant = access.resolve(bridge.app, bridge.settings, bridge.store, record)
                calls = []
                errors = []

                class Recorder:
                    def __init__(self):
                        self.beta = self.messages = self

                    async def create(self, **kwargs):
                        errors.extend(
                            b["content"]
                            for m in kwargs["messages"]
                            if isinstance(m.get("content"), list)
                            for b in m["content"]
                            if b.get("is_error")
                        )
                        response = await sdk.beta.messages.create(**kwargs)
                        calls.extend(
                            b.model_dump() for b in response.content if b.type == "tool_use"
                        )
                        return response

                result = await conversation.run(
                    bridge,
                    eid,
                    record,
                    bridge.store,
                    grant,
                    {
                        "request": text,
                        "current_project": {"id": bridge.pid, "name": "liver"},
                        "project_md": "Liver project. Species: human.",
                        "data_uri": "gs://lab-data/liver/",
                    },
                    [],
                    client=Recorder(),
                )
                operations = [
                    c["input"].get("operation") for c in calls if c["name"] == "propose_command"
                ]
                policy = commands.spec(expected)
                terminal = (
                    policy.handoff or policy.approval or policy.exact_approval or expected == "use"
                )
                passed = expected in operations and result.route == (
                    "command" if terminal else "reply"
                )
                rows.append(
                    {
                        "request": text,
                        "expected": expected,
                        "operations": operations,
                        "route": result.route,
                        "passed": passed,
                        "tools": calls,
                        "errors": errors,
                        "reply": result.reply,
                    }
                )

        await asyncio.gather(*(evaluate(i, *case) for i, case in enumerate(CASES)))
    report = {
        "model": bridge.settings.model,
        "passed": sum(r["passed"] for r in rows),
        "total": len(rows),
        "live_delivery": False,
        "remote_smoke_paths": [],
        "cases": rows,
    }
    if path := os.environ.get("MICA_NATURAL_EVAL_REPORT"):
        Path(path).write_text(json.dumps(report, indent=2) + "\n")
    assert all(row["passed"] for row in rows), report
    bridge.test_client.create_job.assert_not_called()
