"""Large controller manifests retain the same authorization checks as small requests."""

# ruff: noqa: F811
import json

import pytest
from analysis_helpers import memory_message as message
from test_analysis_batch import bridge  # noqa: F401
from test_analysis_direct import chat  # noqa: F401

from mica.agents.analysis import access, conversation, recovery
from mica.agents.analysis.registry import Registry


@pytest.mark.parametrize("memory_bytes", [300_000, 3_000_000])
async def test_large_project_snapshot_does_not_revoke_worker_access(chat, memory_bytes):
    await message(chat, "share liver <@U2>", "10.0")
    await message(chat, "use liver", "11.0", user="U2", channel="D2")
    chat.decision.return_value = conversation.Decision(route="compute", reply="Reading source.")
    record = await message(chat, "Read source", "12.0", user="U2", channel="D2")
    event = record["job"].rsplit("claude-", 1)[-1]
    key = f"{recovery.event_prefix(event)}/request.json"
    request = json.loads(chat.store.get(key)[0])
    state = json.loads(chat.store.get(access.checkpoint_key(request["thread_key"]))[0])
    # Unicode source expands under the controller's JSON encoding. Each memory file
    # stays within the recipe limit, and the total is within the default 4 MiB budget.
    content = "é" * (memory_bytes // 2)
    request["memory"]["files"] = {
        f"recipes/step-{index}.md": {
            "text": "type: recipe\nsource: offline fixture\nstatus: current\n\n"
            + content[start : start + 30_000],
            "oid": "a" * 40,
        }
        for index, start in enumerate(range(0, len(content), 30_000))
    }
    raw = json.dumps(request).encode()
    assert len(raw) > 256 * 1024
    chat.store.put(key, raw)
    assert recovery.read_json(chat.store, key) is None  # Small record limit is unchanged.
    assert access.worker_permitted(chat.store, request, state, settings=chat.settings)
    assert not access.worker_permitted(
        chat.store, {**request, "access_signature": "changed"}, state, settings=chat.settings
    )

    # A newer worker supersedes this attempt even when the manifest contains large inputs.
    replacement = {**request, "recovery": {**request["recovery"], "attempt": 1}}
    chat.store.put(key, json.dumps(replacement).encode())
    assert not access.worker_permitted(chat.store, request, state, settings=chat.settings)
    chat.store.put(key, raw)

    cancel = f"{recovery.event_prefix(event)}/memory-cancelled.json"
    chat.store.put(cancel, b"{}")
    assert not access.worker_permitted(chat.store, request, state, settings=chat.settings)
    chat.store.delete_if(cancel, chat.store.get(cancel)[1])

    Registry(chat.store).update(
        "revoke", "alice@example.com", "unshare",
        lambda doc: doc.share(chat.pid, "alice@example.com", "bob@example.com", remove=True),
    )
    assert not access.worker_permitted(chat.store, request, state, settings=chat.settings)
