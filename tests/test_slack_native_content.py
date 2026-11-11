"""Native Markdown and project controls retain delivery and authorization boundaries."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from analysis_helpers import seed_project
from fastapi import BackgroundTasks
from slack_sdk.errors import SlackApiError
from test_analysis_batch import bridge, payload  # noqa: F401

from mica.agents.analysis import access, approvals, direct, project_picker
from mica.agents.analysis.registry import Registry, RegistryDoc
from mica.agents.analysis.slack_format import answer_blocks, chunks, neutralize, to_mrkdwn
from mica.agents.analysis.transport import SlackTransport
from mica.api import slack_analysis


def test_markdown_neutralizes_angle_forms_without_rewriting_markdown():
    text = '<!here> <!channel> <@U123> <#C123> <https://bad.test|good> [source](https://good.test)'
    block = answer_blocks(text)[0]
    assert block == {"type": "markdown", "text": neutralize(text)}
    assert "<" not in block["text"] and "[source](https://good.test)" in block["text"]
    assert answer_blocks("**Bold**\n| A | B |\n| --- | --- |\n| 1 | 2 |")[0]["type"] == "markdown"


@pytest.mark.parametrize("unit", [
    "```python\n" + "print('hello')\n" * 270 + "```",
    "| Name | Value |\n| --- | --- |\n" + "| Sample | 123 |\n" * 250,
], ids=["fence", "table"])
def test_markdown_chunks_keep_fences_and_tables_whole(unit):
    unit = unit.rstrip("\n")
    result = list(chunks("Intro\n" + unit + "\nDone"))
    assert unit in result
    assert "\n".join(result) == "Intro\n" + unit + "\nDone"
    assert all(len(answer_blocks(part)[0]["text"]) <= 12000 for part in result)


@pytest.mark.parametrize("text", [
    "```python\n" + "value = '<@U123>'\n" * 2000 + "```",
    "| A | B |\n| --- | --- |\n" + "| & | <@U123> |\n" * 2000,
    "```" + "x" * 15000,
    "|" + "x" * 15000 + "| B |\n| --- | --- |\n| 1 | 2 |",
    "<" * 12000,
], ids=["large-fence", "large-table", "large-fence-header", "large-table-header", "escaping"])
def test_oversize_markdown_obeys_escaped_slack_ceiling(text):
    result = list(chunks(text))
    assert result
    assert all(len(answer_blocks(part)[0]["text"]) <= 12000 for part in result)


async def test_invalid_markdown_uses_existing_mrkdwn_fallback():
    client = AsyncMock()
    client.chat_postMessage.side_effect = [
        SlackApiError("unsupported", {"error": "invalid_blocks"}), {"ts": "1"},
    ]
    text = "**Bold** <!here> [source](https://example.org)"
    await SlackTransport(client).post(text=neutralize(text), blocks=answer_blocks(text))
    sent = client.chat_postMessage.call_args.kwargs
    assert sent["blocks"] == [] and sent["text"] == to_mrkdwn(text)
    assert "<!here>" not in sent["text"]


def test_home_visibility_default_and_most_recent_limit():
    doc = RegistryDoc()
    owner = "alice@example.com"
    ids = [doc.create(owner, f"project-{i:03}", f"gs://lab-data/{i}/") for i in range(105)]
    secret = doc.create("bob@example.com", "secret", "gs://lab-data/secret/")
    view = project_picker.home(doc, owner, ids[0], {pid: i for i, pid in enumerate(ids)})
    select = view["blocks"][1]["elements"][0]
    assert len(select["options"]) == 100
    assert select["initial_option"]["value"] == ids[0]
    assert select["options"][0]["value"] == ids[-1]
    assert "secret" not in str(view) and secret not in str(view)
    assert "Existing threads keep their project" in str(view)
    assert "100 most recently used" in str(view)
    assert "Owner" in str(view) and "gs://lab-data/104/" in str(view)


def test_same_named_projects_have_distinct_picker_labels():
    doc = RegistryDoc()
    alice, bob = "alice@example.com", "bob@example.com"
    first = doc.create(alice, "liver", "gs://lab-data/alice/")
    second = doc.create(bob, "liver", "gs://lab-data/bob/")
    doc.share(second, bob, alice)
    view = project_picker.home(doc, alice, first, {})
    options = view["blocks"][1]["elements"][0]["options"]
    labels = {option["value"]: option["text"]["text"] for option in options}
    assert set(labels) == {first, second}
    assert labels[first] != labels[second]
    assert all("liver" in label and len(label) <= 75 for label in labels.values())
    assert second in str(view)


@pytest.fixture
def picker_client(bridge, monkeypatch):
    client = bridge.test_slack
    monkeypatch.setattr(project_picker, "AsyncWebClient", lambda **kw: client)
    return client


async def test_prompts_and_home_refresh_only_when_changed(bridge, picker_client):
    await project_picker.refresh(bridge, "T1", "U1", "D1", tab="home")
    view = picker_client.views_publish.call_args.kwargs
    assert view["user_id"] == "U1" and view["view"]["type"] == "home"
    method, = picker_client.api_call.call_args.args
    sent = picker_client.api_call.call_args.kwargs["json"]
    assert method == "assistant.threads.setSuggestedPrompts"
    assert sent["title"] == "Project: liver"
    assert len(sent["prompts"]) == 4 and "liver" in sent["prompts"][0]["message"]
    assert "thread_ts" not in sent
    await project_picker.refresh(bridge, "T1", "U1", "D1", tab="home")
    assert picker_client.api_call.await_count == picker_client.views_publish.await_count == 1


async def test_no_project_prompts_and_unmapped_user(bridge, picker_client):
    await project_picker.refresh(bridge, "T1", "U2", "D2")
    sent = picker_client.api_call.call_args.kwargs["json"]
    assert sent["title"] == "No project selected"
    assert sent["prompts"] == [{"title": "Get started", "message": "What can you help me do?"}]
    picker_client.reset_mock()
    await project_picker.refresh(bridge, "T1", "UUNKNOWN", "D3", tab="home")
    picker_client.api_call.assert_not_called()
    picker_client.views_publish.assert_not_called()


async def test_home_event_acks_slack_errors_without_work(bridge, picker_client, monkeypatch):
    picker_client.api_call.side_effect = RuntimeError("static prompts configured")
    monkeypatch.setattr(slack_analysis, "BatchBridge", lambda *a: bridge)
    bridge.accept = AsyncMock(side_effect=AssertionError("must not queue a model turn"))
    result = await slack_analysis.receive(
        SimpleNamespace(settings=bridge.app, repository=bridge.repository),
        {"type": "event_callback", "team_id": "T1", "event": {
            "type": "app_home_opened", "tab": "home", "user": "U1", "channel": "D1",
        }},
    )
    assert result == {"ok": True}
    picker_client.views_publish.assert_awaited_once()
    bridge.accept.assert_not_called()


def selection(pid, stamp="123.456"):
    return {"type": "block_actions", "team": {"id": "T1"}, "user": {"id": "U1"},
            "view": {"type": "home"}, "actions": [{
                "action_id": project_picker.PREFIX + "select", "action_ts": stamp,
                "selected_option": {"value": pid},
            }]}


async def test_picker_rechecks_access_and_duplicate_click_cannot_restore_old_default(
    bridge, picker_client,
):
    registry = Registry(bridge.store)
    original = registry.load().defaults["alice@example.com"]
    second = seed_project(bridge.store, name="kidney", channel="D2")
    await project_picker.refresh(bridge, "T1", "U1", "D1", tab="home")
    click = selection(original)
    await approvals.handle(bridge, bridge.store, click, BackgroundTasks())
    assert registry.load().defaults["alice@example.com"] == original
    assert picker_client.api_call.call_args.kwargs["json"]["title"] == "Project: liver"
    await approvals.handle(bridge, bridge.store, selection(second, "124.456"), BackgroundTasks())
    await approvals.handle(bridge, bridge.store, click, BackgroundTasks())
    assert registry.load().defaults["alice@example.com"] == second
    hidden = seed_project(bridge.store, name="hidden", owner="bob@example.com", channel="D3")
    with pytest.raises(ValueError, match="no longer available"):
        await project_picker.handle(bridge, bridge.store, selection(hidden), BackgroundTasks())
    assert registry.load().defaults["alice@example.com"] == second
    assert "hidden" not in str(picker_client.views_publish.call_args)


async def test_typed_use_refreshes_surfaces_but_keeps_existing_thread(bridge, picker_client):
    event_id = await bridge.accept(payload(text="use liver"))
    record = await bridge.repository.get_connector_state("claude-event:" + event_id)
    old = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    second = seed_project(bridge.store, name="kidney", channel="D2")
    await project_picker.refresh(bridge, "T1", "U1", "D1", tab="home")
    from mica.agents.analysis.commands import Command

    await direct.dispatch_command(
        bridge, event_id, record, bridge.store, Command("use", ("liver",)), old,
    )
    assert picker_client.api_call.call_args.kwargs["json"]["title"] == "Project: liver"
    assert Registry(bridge.store).load().defaults[old.email] == old.project_id
    state = {"context": {"project_slug": second}, "messages": [
        {"id": "old", "assistant": "Previous"},
    ]}
    bridge.store.put(access.checkpoint_key(record["thread_key"]), json.dumps(state).encode())
    # Existing context is resolved independently of the default selected in Home.
    assert Registry(bridge.store).load().active_for(old.email, "T1", "D1", private=True) != second
    resolved = access.resolve(bridge.app, bridge.settings, bridge.store, record)
    assert resolved.project_id == second
