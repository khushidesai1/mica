"""Project controls stay in chat and preserve each analysis thread's context."""

import json

import httpx
import pytest
from analysis_helpers import seed_project
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_direct import chat  # noqa: F401
from test_analysis_isolation import message

from mica.agents.analysis import direct
from mica.agents.analysis.access import checkpoint_key
from mica.agents.analysis.commands import Command, parse
from mica.agents.analysis.registry import Registry


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("switch", Command("projects")),
        ("<@BOT> SWITCH liver", Command("use", ("liver",))),
        ("switch p_0123456789abcdef --force", Command("use", ("p_0123456789abcdef",), True)),
        ("use liver", Command("use", ("liver",))),
        ("<@BOT> rename liver liver-atlas", Command("rename", ("liver", "liver-atlas"))),
        ("rename p_0123456789abcdef brain", Command("rename", ("p_0123456789abcdef", "brain"))),
        ("help all", Command("help all")),
        ("switch to log scale", None),
        ("help me switch projects", None),
    ],
)
def test_project_command_grammar(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize("text", ["rename", "rename liver", "rename liver to brain"])
def test_incomplete_rename_has_usage_error(text):
    with pytest.raises(ValueError, match="rename <name or ID> <new-name>"):
        parse(text)


async def test_rename_preserves_channel_threads_and_project_content(chat):  # noqa: F811
    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "publish liver <#C1>", "11.0")
    await message(chat, "switch liver", "12.0", channel="C1")
    first = await message(chat, "Describe this project", "13.0", channel="C1")
    pointer = checkpoint_key(first["thread_key"])
    checkpoint = chat.store.get(pointer)[0]
    before = Registry(chat.store).load()
    brief = (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"]
    chat.store.put(f"projects/{chat.pid}/runs/saved/outputs/result.txt", b"saved result")
    chat.decision.reset_mock()

    reply = await message(chat, "rename liver liver-atlas", "14.0")
    assert "Project renamed to liver-atlas" in reply["notice_text"]
    after = Registry(chat.store).load()
    assert after.projects[chat.pid].model_dump() == {
        **before.projects[chat.pid].model_dump(),
        "name": "liver-atlas",
        "name_revision": before.projects[chat.pid].name_revision + 1,
    }
    assert after.bindings == before.bindings
    assert chat.store.get(pointer)[0] == checkpoint
    renamed_memory = await chat.memory_backend.read(chat.pid)
    assert renamed_memory._root == "projects/liver-atlas"
    assert renamed_memory.files["PROJECT.md"].text.startswith("# Project brief: liver-atlas\n")
    assert brief.text.split("\n", 1)[-1] in renamed_memory.files["PROJECT.md"].text
    saved_output = f"projects/{chat.pid}/runs/saved/outputs/result.txt"
    assert chat.store.get(saved_output)[0] == b"saved result"
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()

    projects = await message(chat, "switch", "15.0", user="U2", channel="D2")
    assert "switch liver-atlas" in projects["notice_text"]
    await message(chat, "Continue this project", "16.0", channel="C1", thread="13.0")
    assert chat.decision.call_args.args[2]["project_name"] == "liver-atlas"
    assert json.loads(chat.store.get(pointer)[0])["context"]["project_slug"] == chat.pid
    renamed = await message(chat, f"rename {chat.pid} liver-final", "17.0")
    assert "Project renamed to liver-final" in renamed["notice_text"]


async def test_rename_requires_owner_dm_and_unique_valid_name(chat):  # noqa: F811
    await message(chat, "share liver <@U2>", "10.0")
    seed_project(chat.store, name="brain", channel="D3")
    before = Registry(chat.store).load()
    attempts = [
        ("rename liver renamed", {"user": "U2", "channel": "D2"}, "owner"),
        ("rename liver renamed", {"channel": "C1"}, "DM"),
        ("rename liver brain", {}, "already have"),
        ("rename liver ../bad", {}, "lowercase"),
        ("rename liver MixedCase", {}, "lowercase"),
        ("rename liver ab", {}, "3–40"),
        ("rename liver " + "x" * 41, {}, "3–40"),
        ("rename liver", {}, "rename <name or ID>"),
    ]
    for i, (text, kwargs, reason) in enumerate(attempts, start=11):
        reply = await message(chat, text, f"{i}.0", **kwargs)
        assert reason in reply["notice_text"]
        assert Registry(chat.store).load() == before
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_rename_retry_after_registry_write_survives_old_name_reuse(chat, monkeypatch):  # noqa: F811
    original = direct.finish

    async def fail(*args, **kwargs):
        raise RuntimeError("lost response after rename")

    monkeypatch.setattr(direct, "finish", fail)
    with pytest.raises(RuntimeError, match="lost response"):
        await message(chat, "rename liver liver-atlas", "10.0")
    assert Registry(chat.store).load().projects[chat.pid].name == "liver-atlas"
    replacement = seed_project(chat.store, name="liver", channel="D3")
    monkeypatch.setattr(direct, "finish", original)
    reply = await message(chat, "rename liver liver-atlas", "10.0")
    assert reply["status"] == "complete"
    after = Registry(chat.store).load()
    assert after.projects[replacement].name == "liver"
    assert after.projects[chat.pid].name == "liver-atlas"
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_unbound_dm_controls_and_structured_help(chat):  # noqa: F811
    for ts, text in [("10.0", "hello"), ("11.0", "help"), ("12.0", "switch")]:
        reply = await message(chat, text, ts, user="U2", channel="D2")
        assert "`init <name> gs://bucket/folder/`" in reply["notice_text"]
        assert "`switch`" in reply["notice_text"]
        assert "`switch <name or ID>`" in reply["notice_text"]
        assert "`rename <name or ID> <new-name>`" in reply["notice_text"]
        if text != "help":
            assert len(reply["notice_text"]) < 320
        else:
            assert "• `share <project> @user`" in reply["notice_text"]
            assert "• `write grant" in reply["notice_text"]
            assert "help all" not in reply["notice_text"]
        assert "liver" not in reply["notice_text"]
    full = await message(chat, "help all", "13.0")
    assert "• `share <project> @user`" in full["notice_text"]
    assert "• `memory`" in full["notice_text"]
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_rename_retries_memory_failure_before_acknowledging(chat, monkeypatch):  # noqa: F811
    original = chat.memory_backend.rename_project

    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError("GitHub unavailable")

    monkeypatch.setattr(chat.memory_backend, "rename_project", unavailable)
    with pytest.raises(RuntimeError, match="synchronization needs retry"):
        await message(chat, "rename liver liver-atlas", "10.0")
    assert Registry(chat.store).load().projects[chat.pid].name_revision == 1
    assert (await chat.memory_backend.read(chat.pid))._root == f"projects/{chat.pid}"
    assert not any(
        "Project renamed" in call.kwargs.get("text", "")
        for call in chat.test_slack.chat_postMessage.call_args_list
    )
    monkeypatch.setattr(chat.memory_backend, "rename_project", original)
    reply = await message(chat, "rename liver liver-atlas", "10.0")
    assert reply["status"] == "complete"
    assert Registry(chat.store).load().projects[chat.pid].name_revision == 1
    assert (await chat.memory_backend.read(chat.pid))._root == "projects/liver-atlas"


async def test_switch_keeps_thread_context_and_routes_new_threads(chat):  # noqa: F811
    target = seed_project(
        chat.store, name="brain", channel="D3", uri="gs://lab-data/brain/", brief=b"Brain brief"
    )
    # Creating the second project also selects it across private surfaces.
    await message(chat, "switch liver", "9.0")
    first = await message(chat, "These are human liver samples", "10.0")
    pointer = checkpoint_key(first["thread_key"])
    original = chat.store.get(pointer)[0]
    switched = await message(chat, "switch brain", "11.0", thread="10.0")
    assert "New threads will use brain" in switched["notice_text"]
    assert "outside this thread" in switched["notice_text"]
    assert Registry(chat.store).load().bound("T1", "D1") == target
    assert chat.store.get(pointer)[0] == original
    projects = await message(chat, "switch", "12.0", thread="10.0")
    assert "brain (new threads)" in projects["notice_text"]
    assert "This thread uses liver." in projects["notice_text"]
    chat.decision.assert_awaited_once()

    fresh = await message(chat, "Describe the project", "13.0")
    context = chat.decision.call_args.args[2]
    assert context["project_name"] == "brain" and context["brief"] == "Brain brief"
    assert not context["recent_messages"]
    fresh_state = json.loads(chat.store.get(checkpoint_key(fresh["thread_key"]))[0])
    assert fresh_state["context"]["project_slug"] == target
    await message(chat, "What species are these?", "14.0", thread="10.0")
    assert chat.decision.call_args.args[2]["project_name"] == "liver"
    chat.test_client.create_job.assert_not_called()


async def test_switch_lists_only_accessible_projects_and_disambiguates_names(chat):  # noqa: F811
    shared = seed_project(chat.store, owner="bob@example.com", channel="D2")
    secret = seed_project(chat.store, name="private-brain", owner="bob@example.com", channel="D2")
    await message(chat, "share liver <@U1>", "10.0", user="U2", channel="D2")
    projects = await message(chat, "switch", "11.0")
    assert "`switch liver`" in projects["notice_text"]
    assert f"`switch {shared}`" in projects["notice_text"]
    assert "shared by bob@example.com" in projects["notice_text"]
    assert "private-brain" not in projects["notice_text"]
    assert secret not in projects["notice_text"]

    denied = await message(chat, f"switch {secret}", "12.0")
    assert "unavailable" in denied["notice_text"]
    assert Registry(chat.store).load().bound("T1", "D1") == chat.pid
    switched = await message(chat, f"switch {shared}", "13.0")
    assert switched["status"] == "complete"
    assert Registry(chat.store).load().bound("T1", "D1") == shared
    await message(chat, "switch liver", "14.0")
    assert Registry(chat.store).load().bound("T1", "D1") == chat.pid
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


async def test_switch_in_channel_keeps_publication_and_force_requirements(chat):  # noqa: F811
    projects = await message(chat, "switch", "10.0", channel="C1")
    assert "DM" in projects["notice_text"] and "liver" not in projects["notice_text"]
    denied = await message(chat, "switch liver", "11.0", channel="C1")
    assert "publish" in denied["notice_text"]
    await message(chat, "publish liver <#C1>", "12.0")
    await message(chat, "switch liver", "13.0", channel="C1")
    target = seed_project(chat.store, name="brain", channel="D3")
    await message(chat, "publish brain <#C1>", "14.0")
    denied = await message(chat, "switch brain", "15.0", channel="C1")
    assert "--force" in denied["notice_text"]
    assert Registry(chat.store).load().bound("T1", "C1") == chat.pid
    await message(chat, "switch brain --force", "16.0", channel="C1")
    assert Registry(chat.store).load().bound("T1", "C1") == target
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("suffix", ["", " --force"])
async def test_init_selects_new_project_and_offers_switch_back(chat, monkeypatch, suffix):  # noqa: F811
    monkeypatch.setattr(direct, "inventory", lambda *a, **kw: {"entries": []})
    old_thread = await message(chat, "Describe this project", "10.0")
    original_brief = (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"]
    checkpoint = checkpoint_key(old_thread["thread_key"])
    original_context = chat.store.get(checkpoint)[0]
    reply = await message(chat, "init brain gs://lab-data/brain/" + suffix, "11.0")
    assert reply["status"] == "complete"
    assert "Active project: brain. Switch back: `switch liver`." in reply["notice_text"]
    assert "`init <name> gs://bucket/folder/`" in reply["notice_text"]
    assert "`switch`" in reply["notice_text"]
    doc = Registry(chat.store).load()
    assert len(doc.projects) == 2
    target = doc.find("brain", "alice@example.com")
    assert doc.bound("T1", "D1") == target
    assert chat.store.get(checkpoint)[0] == original_context
    assert (await chat.memory_backend.read(chat.pid)).files["PROJECT.md"] == original_brief
    assert chat.decision.call_args.args[2]["project_name"] == "brain"

    await message(chat, "switch liver", "12.0", thread="11.0")
    assert Registry(chat.store).load().bound("T1", "D1") == chat.pid
    await message(chat, "Describe this project", "13.0")
    assert chat.decision.call_args.args[2]["project_name"] == "liver"
    chat.test_client.create_job.assert_not_called()


@pytest.mark.parametrize("switch_during_retry", [False, True])
async def test_init_switch_back_survives_interrupted_binding_write(
    chat,  # noqa: F811
    monkeypatch,
    switch_during_retry,
):
    monkeypatch.setattr(direct, "inventory", lambda *a, **kw: {"entries": []})
    original = Registry.update
    failed = False

    def update(self, *args, **kwargs):
        nonlocal failed
        result = original(self, *args, **kwargs)
        if not failed:
            failed = True
            raise RuntimeError("binding response lost")
        return result

    monkeypatch.setattr(Registry, "update", update)
    event = await chat.accept(payload(text="init brain gs://lab-data/brain/"))
    with pytest.raises(RuntimeError, match="binding response lost"):
        await chat.process(event, 0)
    if switch_during_retry:
        await message(chat, "switch liver", "2.0")
    assert await chat.process(event, 0) == "complete"
    reply = await chat.repository.get_connector_state("claude-event:" + event)
    doc = Registry(chat.store).load()
    if switch_during_retry:
        assert "Project ready: brain. Select it with `switch brain`." in reply["notice_text"]
        assert "Active project: brain" not in reply["notice_text"]
        assert doc.bound("T1", "D1") == chat.pid
    else:
        assert "Active project: brain. Switch back: `switch liver`." in reply["notice_text"]
        assert doc.bound("T1", "D1") == doc.find("brain", "alice@example.com")
    assert len(doc.projects) == 2
    chat.decision.assert_awaited_once()
    chat.test_client.create_job.assert_not_called()


async def test_init_switch_back_uses_id_when_new_project_shadows_shared_name(chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(direct, "inventory", lambda *a, **kw: {"entries": []})
    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "switch liver", "11.0", user="U2", channel="D2")
    reply = await message(chat, "init liver gs://lab-data/bob/", "12.0", user="U2", channel="D2")
    assert f"Switch back: `switch {chat.pid}`." in reply["notice_text"]
    await message(chat, f"switch {chat.pid}", "13.0", user="U2", channel="D2")
    assert Registry(chat.store).load().bound("T1", "D2") == chat.pid


async def test_init_reuses_selected_project_without_spurious_switch_back(chat, monkeypatch):  # noqa: F811
    monkeypatch.setattr(direct, "inventory", lambda *a, **kw: {"entries": []})
    reply = await message(chat, "init liver gs://lab-data/liver/", "10.0")
    assert reply["status"] == "complete"
    assert "Active project: liver." in reply["notice_text"]
    assert "Switch back" not in reply["notice_text"]
    assert len(Registry(chat.store).load().projects) == 1


async def test_invalid_init_does_not_change_selection(chat):  # noqa: F811
    for ts, uri in [("10.0", "gs://outside-project/"), ("11.0", "gs://lab-data/other/")]:
        reply = await message(chat, f"init liver {uri}", ts)
        assert reply["status"] == "failed"
        doc = Registry(chat.store).load()
        assert doc.bound("T1", "D1") == chat.pid
        assert len(doc.projects) == 1
    chat.decision.assert_not_called()
    chat.test_client.create_job.assert_not_called()
