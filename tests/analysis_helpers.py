"""Offline registry fixtures shared by Slack integration tests."""

from pistachio.agents.analysis.registry import Registry, read_brief, write_brief


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
        write_brief(store, pid, brief, read_brief(store, pid)[1])
    return pid


async def memory_message(chat, *args, **kwargs):
    from unittest.mock import AsyncMock, patch

    from test_analysis_isolation import message

    from pistachio.agents.analysis import memory_tasks

    with patch("pistachio.agents.analysis.job_lifecycle.enqueue", AsyncMock()) as enqueue:
        record = await message(chat, *args, **kwargs)
    for call in enqueue.call_args_list:
        path = call.args[2]
        if "/memory/" in path:
            event_id = path.rsplit("/", 1)[-1]
            await memory_tasks.process(chat, event_id, chat.store)
            record = await chat.repository.get_connector_state("claude-event:" + event_id)
    return record
