# ruff: noqa: F811
"""Simple information retrieval stays in the API with scoped, bounded evidence."""

import hashlib
from unittest.mock import AsyncMock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import attachments, direct, reference_repos, thread_history
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


async def test_repository_readme_reads_source_directly_and_reuses_delivered_result(chat, source):  # noqa: F811
    snapshot, fetch = source
    documentation = "# Preprocessing\n" + "Use filter_cells(min_counts=100).\n" * 140
    chat.decision.side_effect = [
        direct.Decision(route="read", reference_repos=[snapshot.repo], reply="Reading source."),
        direct.Decision(route="reply", reply=documentation),
    ]
    eid = await chat.accept(payload(text="Read nympheas source and write a basic QC README."))
    assert await chat.process(eid, 0) == "complete"
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once_with(chat.settings, snapshot.repo)
    supplied = chat.decision.call_args.args[2]["information"]["references"][0]
    assert supplied["commit"] == snapshot.commit and supplied["files"] == snapshot.files
    assert supplied["requested_files_complete"]
    assert "Code was not executed" in supplied["policy"]
    assert chat.decision.await_count == 2
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["direct_decision"]["reply"] == documentation
    delivered = [
        call.kwargs["text"]
        for call in chat.test_slack.chat_postMessage.call_args_list
        if call.kwargs["text"].startswith("Use filter_cells")
    ]
    assert delivered and all(len(text) <= 3500 for text in delivered)
    assert "information" not in record  # Source bytes never go in the Firestore receipt.
    chat.test_client.create_job.assert_not_called()
    assert not any(k.startswith("claude-slot") for k in chat.repository.event_claims)


async def test_read_limit_does_not_fall_back_to_computation(chat, source):  # noqa: F811
    snapshot, fetch = source
    chat.decision.return_value = direct.Decision(
        route="read", reference_repos=[snapshot.repo], reply="Reading more."
    )
    eid = await chat.accept(payload(text="Explain the package"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()  # Follow-up reads retain the same pinned snapshot.
    assert chat.decision.await_count == 3
    assert chat.decision.call_args.args[2]["read_budget_remaining"] == 0
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert "read limit" in record["notice_text"]
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("change_at", ["before_read", "after_read"])
async def test_information_rechecks_project_access(chat, source, change_at):  # noqa: F811
    snapshot, fetch = source
    calls = 0

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == (1 if change_at == "before_read" else 2):

            def revoke(doc):
                doc.projects[chat.pid].access_revision += 1
                return chat.pid

            Registry(chat.store).update("changed", "alice@example.com", "change", revoke)
        return (
            direct.Decision(route="read", reference_repos=[snapshot.repo], reply="Read source")
            if calls == 1
            else direct.Decision(route="reply", reply="Do not deliver this source")
        )

    chat.decision.side_effect = decide
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    assert fetch.await_count == (0 if change_at == "before_read" else 1)
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["notice_text"] == "Project access changed. Send a new request."
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize(
    "repo, paths",
    [
        ("azizilab/private", {}),
        ("elsewhere/repo", {}),
        ("azizilab/nympheas", {"azizilab/nympheas": ["../secret.py"]}),
        ("azizilab/nympheas", {"azizilab/private": ["code.py"]}),
    ],
)
async def test_information_rejects_unavailable_repo_or_paths_before_fetch(
    chat, source, repo, paths
):  # noqa: F811
    _, fetch = source
    chat.decision.return_value = direct.Decision(
        route="read", reference_repos=[repo], reference_paths=paths, reply="Reading source"
    )
    eid = await chat.accept(payload(text="Explain a repo"))
    assert await chat.process(eid, 0) == "failed"
    fetch.assert_not_awaited()
    chat.test_client.create_job.assert_not_called()


async def test_source_unavailable_is_explicit_without_worker(chat, source):  # noqa: F811
    _, fetch = source
    fetch.side_effect = ValueError("no longer installed")
    chat.decision.side_effect = [
        direct.Decision(route="read", reference_repos=["azizilab/nympheas"], reply="Read source"),
        direct.Decision(route="reply", reply="That source is unavailable right now."),
    ]
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    assert chat.decision.call_args.args[2]["information"]["references"] == [
        {"repo": "azizilab/nympheas", "unavailable": True}
    ]
    chat.test_client.create_job.assert_not_called()


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


async def test_history_lookup_is_scoped_to_current_thread_and_project(chat):  # noqa: F811
    eid = await chat.accept(payload(text="What QC threshold did we use earlier?"))
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    history = thread_history.History(chat.store, record["thread_key"], chat.pid)
    history.record({"id": "prior", "user": "QC", "assistant": "min_counts=100"})
    thread_history.History(chat.store, record["thread_key"], "other").record(
        {"id": "prior", "user": "QC", "assistant": "foreign information"}
    )
    chat.decision.side_effect = [
        direct.Decision(route="read", history_event_id="prior", reply="Checking history"),
        direct.Decision(route="reply", reply="The earlier threshold was 100."),
    ]
    assert await chat.process(eid, 0) == "complete"
    supplied = chat.decision.call_args.args[2]["information"]["history"]
    assert "min_counts=100" in supplied["text"] and "foreign" not in supplied["text"]
    chat.test_client.create_job.assert_not_called()


async def test_targeted_second_read_uses_same_pin_and_previous_findings(chat, source):
    snapshot, fetch = source
    snapshot.files = {"a.py": "a" * 240_000, "b.py": "b" * 240_000}
    chat.decision.side_effect = [
        direct.Decision(route="read", reference_repos=[snapshot.repo], reply="Read the source"),
        direct.Decision(
            route="read",
            reference_repos=[snapshot.repo],
            reference_paths={snapshot.repo: ["b.py"]},
            reply="a.py at commit aaaa defines stage A.",
        ),
        direct.Decision(route="reply", reply="Here is the documented workflow from A and B."),
    ]
    eid = await chat.accept(payload(text="Explain this repository's workflow"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()
    first = chat.decision.call_args_list[1].args[2]["information"]["references"][0]
    final_context = chat.decision.call_args.args[2]
    second = final_context["information"]["references"][0]
    assert first["omitted_paths"] == ["b.py"] and set(second["files"]) == {"b.py"}
    assert first["commit"] == second["commit"]
    assert "stage A" in final_context["read_notes"]
    chat.test_client.create_job.assert_not_called()


async def test_removed_catalog_reference_is_not_delivered(chat, source, monkeypatch):
    snapshot, fetch = source
    catalog = {snapshot.repo}
    monkeypatch.setattr(
        reference_repos, "shared_repos", AsyncMock(side_effect=lambda *args: catalog)
    )
    calls = 0

    async def decide(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return direct.Decision(
                route="read", reference_repos=[snapshot.repo], reply="Read source"
            )
        catalog.clear()
        return direct.Decision(route="reply", reply="Must not deliver this source")

    chat.decision.side_effect = decide
    eid = await chat.accept(payload(text="Explain nympheas"))
    assert await chat.process(eid, 0) == "complete"
    fetch.assert_awaited_once()
    record = await chat.repository.get_connector_state("claude-event:" + eid)
    assert record["notice_text"] == "Reference repository access changed. Send a new request."
    chat.test_client.create_job.assert_not_called()


async def test_view_then_information_read_preserves_media_context(chat, source):
    from test_analysis_direct import seed_earlier_output

    output = await seed_earlier_output(chat)
    chat.decision.reset_mock()
    chat.decision.side_effect = [
        direct.Decision(route="view", view_ids=[output.id], reply="Looking at the result"),
        direct.Decision(
            route="read", reference_repos=["azizilab/nympheas"], reply="Check its method"
        ),
        direct.Decision(route="reply", reply="This plot uses the documented method."),
    ]
    eid = await chat.accept(payload("2.0", "1.0", "Explain this plot's method from the code"))
    assert await chat.process(eid, 0) == "complete"
    viewing, reading = chat.decision.call_args_list[1:]
    assert viewing.kwargs["media"] == reading.kwargs["media"]
    assert reading.kwargs["media"][1]["type"] == "image"
    chat.test_client.create_job.assert_not_called()
