"""Offline registry fixtures shared by Slack integration tests."""

from mica.agents.analysis.registry import Registry


def seed_project(
    store,
    name="liver",
    owner="alice@example.com",
    channel="D1",
    uri="gs://lab-data/liver/",
    brief=b"# Project\nSpecies: TODO",
    publish=False,
):
    registry = Registry(store)

    def change(doc):
        pid = doc.create(owner, name, uri)
        if publish:
            doc.publish(pid, owner, "T1", channel)
        if channel.startswith("D") or publish:
            doc.bind("T1", channel, owner, pid, private=channel.startswith("D"), force=True)
        return pid

    pid = registry.update(f"seed:{owner}:{name}:{channel}", owner, "seed", change)
    if brief is not None:
        if hasattr(store, "memory_github"):
            store.memory_github.seed({f"projects/{pid}/PROJECT.md": brief.decode()})
        else:
            store.put(f"projects/{pid}/PROJECT.md", brief)
    return pid


async def memory_message(chat, *args, **kwargs):
    from unittest.mock import AsyncMock, patch

    from test_analysis_isolation import message

    from mica.agents.analysis import memory_tasks

    with patch("mica.agents.analysis.job_lifecycle.enqueue", AsyncMock()) as enqueue:
        record = await message(chat, *args, **kwargs)
    for call in enqueue.call_args_list:
        path = call.args[2]
        if "/memory/" in path:
            event_id = path.rsplit("/", 1)[-1]
            await memory_tasks.process(chat, event_id, chat.store)
            record = await chat.repository.get_connector_state("claude-event:" + event_id)
    return record
