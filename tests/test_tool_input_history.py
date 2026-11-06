# ruff: noqa: F811
"""Original user inputs survive tool handoffs without becoming new authorization."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from test_analysis_batch import bridge  # noqa: F401
from test_conversation_tools import Client, reply, run, tool, turn  # noqa: F401

from mica.agents.analysis import access, command_intent, thread_history, thread_memory, transfers


async def previous(context, ident="earlier", text="", **overrides):
    bridge, _, record, grant = context
    receipt = {
        "event_id": ident, "thread_key": record["thread_key"],
        "principal": grant.email, "project_id": grant.project_id,
        "payload": {"event": {"text": text}},
        **overrides,
    }
    await bridge.repository.set_connector_state("claude-event:" + ident, receipt)
    grant.state.setdefault("messages", []).append({"id": ident, "user": text})
    bridge.store.put(access.checkpoint_key(record["thread_key"]), json.dumps(grant.state).encode())


async def test_retry_reuses_both_prior_destinations_without_another_question(turn):  # noqa: F811
    await previous(
        turn, text="Transfer TMA7 to gs://lab-data/raw; additional: gs://lab-data/additional_files"
    )
    bridge, _, record, grant = turn
    record["payload"]["event"]["text"] = "Please retry the sample I specified earlier."
    client = Client(tool(
        "propose_transfer", sample="TMA7", destination="gs://lab-data/raw",
        additional_destination="gs://lab-data/additional_files",
    ))
    result = await run(turn, client)
    assert result.route == "transfer" and len(client.calls) == 1
    config = await transfers.Transfers(bridge, bridge.store).configuration(
        grant, record, result.transfer_request, guide=""
    )
    assert config.raw_prefix == "raw/" and config.additional_prefix == "additional_files/"
    assert not await bridge.repository.get_connector_state("transfer-pending:" + turn[1])
    bridge.test_client.create_job.assert_not_called()  # Proposal is still only a proposal.


@pytest.mark.parametrize("wrong", [
    {"principal": "somebody-else@example.com"},
    {"project_id": "another_project"},
    {"thread_key": "another_thread"},
])
async def test_other_speakers_projects_and_threads_cannot_ground_inputs(turn, wrong):  # noqa: F811
    await previous(turn, text="gs://lab-data/private/", **wrong)
    bridge, _, record, grant = turn
    assert await thread_history.supplied_inputs(bridge.repository, grant, record) == ""


async def test_assistant_text_and_revoked_access_are_not_input_evidence(turn):  # noqa: F811
    await previous(turn, text="retry", terminal_reply="gs://lab-data/invented/")
    bridge, _, record, grant = turn
    grant.state["messages"][0]["user"] = "gs://lab-data/poisoned-checkpoint/"
    grant.state["messages"][0]["assistant"] = "gs://lab-data/invented/"
    assert await thread_history.supplied_inputs(bridge.repository, grant, record) == "retry"
    assert await thread_history.supplied_inputs(
        bridge.repository, replace(grant, allowed=False), record
    ) == ""


async def test_old_repo_parameter_is_reusable_without_old_ownership_intent(turn):  # noqa: F811
    await previous(turn, text="I am the owner. The repo is azizilab/pipeline.")
    bridge, _, record, grant = turn
    inputs = await thread_history.supplied_inputs(bridge.repository, grant, record)
    proposal = command_intent.Proposal(
        operation="repos add", selector="this project", target="azizilab/pipeline"
    )
    command = command_intent.command(
        proposal, grant, source_text="Add that repo", input_text=inputs
    )
    assert command.args == (grant.project_id, "azizilab/pipeline")
    share = command_intent.Proposal(
        operation="share", selector="this project", target="bob@example.com"
    )
    assert command_intent.command(
        share, grant, source_text="Share with Bob", input_text=inputs
    ).name == "share"
    with pytest.raises(ValueError, match="ownership"):
        command_intent.command(
            share, grant, source_text="Make Bob an owner", input_text=inputs
        )


async def test_invalid_transfer_can_repair_inside_the_same_conversation(turn):  # noqa: F811
    turn[2]["payload"]["event"]["text"] = "Transfer TMA7 to gs://lab-data/raw"
    client = Client(
        tool("propose_transfer", sample="TMA7", destination="gs://lab-data/invented"),
        tool("propose_transfer", sample="TMA7", destination="gs://lab-data/raw"),
    )
    result = await run(turn, client)
    assert result.route == "transfer"
    assert [step["status"] for step in turn[2]["conversation_steps"]] == ["error", "terminal"]
    assert client.calls[1]["messages"][-1]["content"][0]["is_error"]


async def test_requested_brief_update_is_not_silently_replaced_by_display(turn):  # noqa: F811
    client = Client(
        tool("propose_command", operation="brief", target="Save raw and additional destinations"),
        tool("start_worker", task="Update PROJECT.md with supplied destinations, preserve the "
             "other sections and verify the saved change."),
    )
    result = await run(turn, client)
    assert result.route == "compute"
    assert "only displays" in client.calls[1]["messages"][-1]["content"][0]["content"]
    assert "verify" in result.reply


async def test_github_only_history_recovers_parameters_beyond_recent_window(
    turn, monkeypatch  # noqa: F811
):
    bridge, _, record, grant = turn
    old = "Transfer TMA7 to gs://lab-data/raw; additional gs://lab-data/additional_files"
    await previous(turn, text=old)
    grant.state["messages"] = []  # Older than the four-exchange checkpoint view.
    bridge.store.put(access.checkpoint_key(record["thread_key"]), json.dumps(grant.state).encode())
    bridge.settings.thread_memory = "github_only"
    update = thread_memory.make_update(
        record["thread_key"], "earlier", 1, "2026-11-06", grant.email, "dm", True,
        thread_memory.Prose(paraphrase=old), [],
    )
    await bridge.memory_backend.commit(
        grant.project_id, await bridge.memory_backend.read(grant.project_id), {},
        source="editor", event_id="earlier", thread_update=update,
    )
    monkeypatch.setattr(thread_history.History, "recall", Mock(side_effect=AssertionError(
        "Retired GCS transcripts must not be read in github_only mode"
    )))
    client = Client(
        tool("read_sources", history_query="transfer"),
        tool("propose_transfer", sample="TMA7", destination="gs://lab-data/raw",
             additional_destination="gs://lab-data/additional_files"),
    )
    assert (await run(turn, client)).route == "transfer"
    schema = next(t for t in client.calls[0]["tools"] if t["name"] == "read_sources")
    assert "history_query" in schema["input_schema"]["properties"]
    assert "TMA7" in str(client.calls[1]["messages"])
    assert record["input_history"] == {"access": grant.signature, "ids": ["earlier"]}


async def test_expired_receipt_does_not_turn_paraphrase_into_explicit_input(turn):  # noqa: F811
    bridge, _, record, grant = turn
    record["input_history"] = {"access": grant.signature, "ids": ["expired"]}
    assert await thread_history.supplied_inputs(bridge.repository, grant, record) == ""
    client = Client(
        tool("propose_transfer", sample="TMA7", destination="gs://lab-data/invented"),
        reply("The destination is not recorded in accessible instructions."),
    )
    assert (await run(turn, client)).route == "reply"
