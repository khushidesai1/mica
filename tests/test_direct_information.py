"""Information tools retain scoped, bounded evidence in the conversation loop."""

# ruff: noqa: F811
import hashlib
import json
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import CONVERSATION_RUN, bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_conversation_tools import Client, reply, tool

from mica.agents.analysis import attachments, conversation, reference_repos, thread_history
from mica.agents.analysis.registry import Registry


@pytest.fixture
def source(chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        reference_repos, "shared_repos", AsyncMock(return_value={"azizilab/nympheas"})
    )
    snapshot = reference_repos.RepoSnapshot(
        repo="azizilab/nympheas",
        commit="a" * 40,
        files={"nympheas/qc.py": "def filter_cells(min_counts=100):\n    pass\n"},
    )
    fetch = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(reference_repos, "fetch", fetch)
    return snapshot, fetch


def test_source_view_is_bounded_and_supports_targeted_followup():
    snapshot = reference_repos.RepoSnapshot(
        repo="azizilab/nympheas",
        commit="a" * 40,
        files={"a.py": "a" * 240_000, "b.py": "b" * 240_000},
        skipped=3,
    )
    view = reference_repos.prompt_view(snapshot, [], 384 * 1024)
    assert set(view["files"]) == {"a.py"}
    assert view["omitted_paths"] == ["b.py"] and view["snapshot_skipped"] == 3
    assert not view["requested_files_complete"]
    followup = reference_repos.prompt_view(snapshot, ["b.py", "unknown.py"], 384 * 1024)
    assert set(followup["files"]) == {"b.py"}
    assert followup["unavailable_paths"] == ["unknown.py"]
    assert followup["commit"] == view["commit"]


def test_text_reads_only_registered_ids_without_binary_decoding(tmp_path):
    from mica.agents.analysis.storage import LocalStorage

    storage = LocalStorage(tmp_path)
    data = b"threshold=100\n" * 6000
    storage.put("registered.txt", data)
    file = attachments.ThreadFile(
        id="known",
        source="user",
        name="settings.txt",
        path="inputs/settings.txt",
        kind="text",
        media_type="text/plain",
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        key="registered.txt",
        event_id="e",
    )
    value = attachments.router_text(storage, [file], ["known", "foreign"])
    assert len(value[0]["text"].encode()) == 64 * 1024 and not value[0]["complete"]
    assert value[1] == {"id": "foreign", "unavailable": True}
    binary = file.model_copy(update={"id": "binary", "media_type": "application/x-hdf5"})
    assert attachments.router_text(storage, [binary], ["binary"])[0]["unavailable"]
    assert attachments.router_text(storage, [file], ["known"], 0)[0]["context_limit"]


def scripted(monkeypatch, *frames):
    client = Client(*frames)
    monkeypatch.setattr(conversation, "run", CONVERSATION_RUN)
    monkeypatch.setattr(conversation, "AsyncAnthropic", lambda **kw: client)
    return client


def result(client, call=1):
    return json.loads(client.calls[call]["messages"][-1]["content"][0]["content"][0]["text"])


async def test_repository_readme_reads_source_directly_and_reuses_delivered_result(
    chat, source, monkeypatch
):
    snapshot, fetch = source
    documentation = "# Preprocessing\n" + "Use filter_cells(min_counts=100).\n" * 140
    client = scripted(
        monkeypatch, tool("read_sources", repos=[snapshot.repo]), reply(documentation)
    )
    eid = await chat.accept(payload(text="Read nympheas source and write a basic QC README."))
    assert await chat.process(eid, 0) == "complete"
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once_with(chat.settings, snapshot.repo)
    supplied = result(client)["references"][0]
    assert supplied["commit"] == snapshot.commit and supplied["files"] == snapshot.files
    assert supplied["requested_files_complete"]
    assert "Code was not executed" in supplied["policy"]
    assert len(client.calls) == 2
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["direct_decision"]["reply"] == documentation.rstrip()
    delivered = [
        call.kwargs["text"]
        for call in chat.test_slack.chat_postMessage.call_args_list
        if call.kwargs["text"].startswith("Use filter_cells")
    ]
    assert delivered and all(len(text) <= 3500 for text in delivered)
    assert "information" not in record
    chat.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_read_limit_does_not_fall_back_to_computation(chat, source, monkeypatch):
    snapshot, fetch = source
    chat.settings.conversation_max_calls = 2
    client = scripted(
        monkeypatch,
        tool("read_sources", repos=[snapshot.repo]),
        tool("read_sources", repos=[snapshot.repo]),
        reply("The inspected source is partial; narrow the remaining question."),
    )
    eid = await chat.accept(payload(text="Explain the package"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()
    assert len(client.calls) == 3 and client.calls[-1]["tool_choice"] == {"type": "none"}
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("change_at", ["before_read", "after_read"])
async def test_information_rechecks_project_access(chat, source, monkeypatch, change_at):
    snapshot, fetch = source
    client = scripted(
        monkeypatch,
        tool("read_sources", repos=[snapshot.repo]),
        reply("Do not deliver this source"),
    )
    create = client.create

    async def changing(**kwargs):
        response = await create(**kwargs)
        if len(client.calls) == (1 if change_at == "before_read" else 2):

            def revoke(doc):
                doc.projects[chat.pid].access_revision += 1
                return chat.pid

            Registry(chat.store).update("changed", "alice@example.com", "change", revoke)
        return response

    client.create = changing
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    assert fetch.await_count == (0 if change_at == "before_read" else 1)
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["notice_text"] == "Project access changed. Send a new request."
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize(
    "repo, paths",
    [
        ("azizilab/private", []),
        ("elsewhere/repo", []),
        ("azizilab/nympheas", [{"repo": "azizilab/nympheas", "paths": ["../secret.py"]}]),
        ("azizilab/nympheas", [{"repo": "azizilab/private", "paths": ["code.py"]}]),
    ],
)
async def test_information_rejects_unavailable_repo_or_paths_before_fetch(
    chat, source, monkeypatch, repo, paths
):
    _, fetch = source
    client = scripted(
        monkeypatch, tool("read_sources", repos=[repo], paths=paths), reply("Source unavailable.")
    )
    eid = await chat.accept(payload(text="Explain a repo"))
    assert await chat.process(eid, 0) == "complete"
    assert client.calls[-1]["messages"][-1]["content"][0]["is_error"]
    fetch.assert_not_awaited()
    chat.test_client.create_job.assert_not_called()


async def test_source_unavailable_is_explicit_without_worker(chat, source, monkeypatch):
    _, fetch = source
    fetch.side_effect = ValueError("no longer installed")
    client = scripted(
        monkeypatch,
        tool("read_sources", repos=["azizilab/nympheas"]),
        reply("That source is unavailable right now."),
    )
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    assert result(client)["references"] == [{"repo": "azizilab/nympheas", "unavailable": True}]
    chat.test_client.create_job.assert_not_called()


async def test_history_lookup_is_scoped_to_current_thread_and_project(chat, monkeypatch):
    eid = await chat.accept(payload(text="What QC threshold did we use earlier?"))
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    history = thread_history.History(chat.store, record["thread_key"], chat.pid)
    history.record({"id": "prior", "user": "QC", "assistant": "min_counts=100"})
    thread_history.History(chat.store, record["thread_key"], "other").record(
        {"id": "prior", "user": "QC", "assistant": "foreign information"}
    )
    client = scripted(
        monkeypatch,
        tool("read_sources", history_event_id="prior"),
        reply("The earlier threshold was 100."),
    )
    assert await chat.process(eid, 0) == "complete"
    supplied = result(client)["history"]
    assert "min_counts=100" in supplied["text"] and "foreign" not in supplied["text"]
    chat.test_client.create_job.assert_not_called()


async def test_targeted_second_read_uses_same_pin_and_previous_findings(chat, source, monkeypatch):
    snapshot, fetch = source
    snapshot.files = {"a.py": "a" * 100_000, "b.py": "b" * 100_000, "c.py": "c" * 240_000}
    first = tool("read_sources", repos=[snapshot.repo])
    first[1].insert(0, {"type": "text", "text": "a.py defines stage A."})
    client = scripted(
        monkeypatch,
        first,
        tool(
            "read_sources",
            repos=[snapshot.repo],
            paths=[{"repo": snapshot.repo, "paths": ["b.py"]}],
        ),
        reply("Here is the documented workflow from A and B."),
    )
    eid = await chat.accept(payload(text="Explain this repository's workflow"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()
    first_result = result(client, 1)["references"][0]
    second = result(client, 2)["references"][0]
    assert first_result["omitted_paths"] == ["c.py"] and set(second["files"]) == {"b.py"}
    assert first_result["commit"] == second["commit"]
    assert "stage A" in json.dumps(client.calls[-1]["messages"])
    chat.test_client.create_job.assert_not_called()


async def test_removed_catalog_reference_is_not_delivered(chat, source, monkeypatch):
    snapshot, fetch = source
    catalog = {snapshot.repo}
    monkeypatch.setattr(
        reference_repos, "shared_repos", AsyncMock(side_effect=lambda *args: catalog)
    )
    client = scripted(
        monkeypatch,
        tool("read_sources", repos=[snapshot.repo]),
        reply("Must not deliver this source"),
    )
    create = client.create

    async def changing(**kwargs):
        response = await create(**kwargs)
        if len(client.calls) == 2:
            catalog.clear()
        return response

    client.create = changing
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["notice_text"] == "Reference repository access changed. Send a new request."
    chat.test_client.create_job.assert_not_called()


async def test_view_then_information_read_preserves_media_context(chat, source, monkeypatch):
    from test_analysis_direct import seed_earlier_output

    output = await seed_earlier_output(chat)
    client = scripted(
        monkeypatch,
        tool("view_files", ids=[output.id]),
        tool("read_sources", repos=["azizilab/nympheas"]),
        reply("This plot uses the documented method."),
    )
    eid = await chat.accept(payload("2.0", "1.0", "Explain this plot's method from the code"))
    assert await chat.process(eid, 0) == "complete"
    viewing = client.calls[1]["messages"][2]
    assert viewing == client.calls[2]["messages"][2]
    assert viewing["content"][0]["content"][1]["type"] == "image"
    chat.test_client.create_job.assert_not_called()
