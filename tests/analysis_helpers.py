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
