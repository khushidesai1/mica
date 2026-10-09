"""Anonymous calibration history and its non-fatal integration paths."""
# ruff: noqa: F811

import json
from collections import Counter
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest
from test_analysis_batch import bridge, payload  # noqa: F401
from test_analysis_compute import (  # noqa: F401
    EVENT,
    NOW,
    compute,
    compute_bridge,
    estimate_and_request,
)
from test_analysis_direct import chat  # noqa: F401

from pistachio.agents.analysis import compute_batch, compute_control
from pistachio.agents.analysis.compute_tools import ComputeManager

PREFIX = "tasks/slack-analysis/calibration/"


def row(category="analysis", hours=1):
    return {
        "category": category,
        "label": "analysis",
        "tier": "standard",
        "provisioning": "SPOT",
        "estimated": {"hours_high": 3, "longest_step_minutes": 15, "peak_gib": 20},
        "actual": {"hours": hours, "longest_step_minutes": 12, "peak_gib": 18},
    }


def seed(store):
    for i in range(8):
        for index, category in enumerate(
            ("analysis", "training", "full_segmentation", "full_ingest")
        ):
            store.put(
                f"{PREFIX}20261009T00000{i}-" + f"{i * 4 + index:032x}-a0.json",
                json.dumps(row(category, i + 1)).encode(),
            )


@pytest.mark.parametrize("quoted,resources", [(True, True), (False, True), (True, False)])
async def test_history_is_anonymous_once_per_approved_attempt(
    compute, compute_bridge, quoted, resources
):
    if quoted:
        _, response = estimate_and_request(compute, hours=0.2)
        assert response["status"] == "continue"
    prefix = compute_batch.event_prefix(EVENT)
    compute_bridge.store.put(
        f"{prefix}/attempt-a0.json",
        json.dumps(
            {
                "event_id": EVENT,
                "attempt": 0,
                "model_cost": 0.2,
                "outcome": "complete",
            }
        ).encode(),
    )
    if resources:
        compute_bridge.store.put(
            f"{prefix}/resources-a0.json",
            json.dumps(
                {
                    "finished_at": NOW + 7200,
                    "wall_s": 7200,
                    "peak_gib": 18,
                    "commands": [{"wall_s": 720, "peak_gib": 18, "command": "private command"}],
                    "user": "private user",
                    "path": "private sample path",
                }
            ).encode(),
        )
    await compute_batch.terminal(compute_bridge, EVENT, compute.record, Mock(), "SUCCEEDED")
    keys = compute_bridge.store.keys(PREFIX)
    assert len(keys) == (1 if quoted and resources else 0)
    await compute_batch.terminal(compute_bridge, EVENT, compute.record, Mock(), "SUCCEEDED")
    assert compute_bridge.store.keys(PREFIX) == keys
    if keys:
        data = json.loads(compute_bridge.store.get(keys[0])[0])
        assert set(data) == {"category", "label", "tier", "provisioning", "estimated", "actual"}
        assert set(data["estimated"]) == {"hours_high", "longest_step_minutes", "peak_gib"}
        assert data["actual"] == {"hours": 2, "longest_step_minutes": 12, "peak_gib": 18}
        assert "private" not in json.dumps(data)
        assert "basis" not in json.dumps(data)


async def test_unaccepted_estimate_does_not_replace_measured_approved_quote(
    compute, compute_bridge
):
    from test_analysis_compute import estimate_call

    q, _ = estimate_and_request(compute, hours=0.2)
    compute_control.operate(
        EVENT, compute.record, estimate_call(3, "large"), compute.settings, compute.lookup, NOW
    )
    prefix = compute_batch.event_prefix(EVENT)
    compute_bridge.store.put(
        f"{prefix}/attempt-a0.json",
        json.dumps(
            {
                "event_id": EVENT,
                "attempt": 0,
                "model_cost": 0.2,
                "outcome": "complete",
            }
        ).encode(),
    )
    compute_bridge.store.put(
        f"{prefix}/resources-a0.json",
        json.dumps(
            {
                "finished_at": NOW + 600,
                "wall_s": 600,
                "peak_gib": 18,
                "commands": [{"wall_s": 300}],
            }
        ).encode(),
    )
    await compute_batch.terminal(compute_bridge, EVENT, compute.record, Mock(), "SUCCEEDED")
    data = json.loads(compute_bridge.store.get(compute_bridge.store.keys(PREFIX)[0])[0])
    assert data["estimated"]["hours_high"] == q["estimate"]["hours_high"]
    assert data["tier"] == "standard" and data["provisioning"] == "SPOT"


async def test_failed_history_write_cannot_fail_terminal(
    compute, compute_bridge, monkeypatch, caplog
):
    from pistachio.agents.analysis import calibration

    estimate_and_request(compute, hours=0.2)
    monkeypatch.setattr(calibration, "capture", Mock(side_effect=OSError("private details")))
    prefix = compute_batch.event_prefix(EVENT)
    compute_bridge.store.put(
        f"{prefix}/attempt-a0.json",
        json.dumps(
            {
                "event_id": EVENT,
                "attempt": 0,
                "model_cost": 0.2,
                "outcome": "complete",
            }
        ).encode(),
    )
    assert not await compute_batch.terminal(
        compute_bridge, EVENT, compute.record, Mock(), "SUCCEEDED"
    )
    assert "calibration" in caplog.text.lower() and "private details" not in caplog.text
    assert len(compute.record["attempt_ledger"]) == 1


def test_recent_limits_categories_and_strips_unsafe_rows(compute_bridge):
    from pistachio.agents.analysis import calibration

    seed(compute_bridge.store)
    bad = deepcopy(row())
    bad["reason"] = "private sample text"
    compute_bridge.store.put(
        f"{PREFIX}20261010T000000-" + "f" * 32 + "-a0.json", json.dumps(bad).encode()
    )
    recent = calibration.recent(compute_bridge.store)
    assert len(recent) == 12
    assert set(Counter(r["category"] for r in recent).values()) == {3}
    assert recent[0]["actual"]["hours"] == 8
    assert len(calibration.recent(compute_bridge.store, limit=2)) == 2
    assert len(calibration.lines(recent)) == 4
    assert "private" not in json.dumps(recent)


@pytest.mark.parametrize("populated", [False, True])
async def test_router_and_worker_share_compact_history(chat, compute, compute_bridge, populated):
    from types import SimpleNamespace

    if populated:
        seed(chat.store)
        seed(compute_bridge.store)
    chat.settings.on_demand_enabled = True
    event = await chat.accept(payload(text="What should I analyze?"))
    assert await chat.process(event, 0) == "complete"
    context = chat.decision.call_args.args[2]
    if populated:
        assert len(context["worker_history"]) == 4
    else:
        assert "worker_history" not in context
    await compute_batch.prepare_attempt(compute_bridge, EVENT, compute.record)
    manifest = compute_batch.worker_manifest(compute.record, compute.settings)["compute"]
    sampler = SimpleNamespace(cpus=8, memory_gib=64, sample=lambda: SimpleNamespace(free=50))
    prompt = ComputeManager(compute.settings, manifest, sampler, AsyncMock()).prompt()
    if populated:
        assert manifest["worker_history"] == context["worker_history"]
        assert all(prompt.count(line) == 1 for line in manifest["worker_history"])
    else:
        assert "worker_history" not in manifest


def test_history_read_failure_is_non_fatal(compute_bridge, monkeypatch, caplog):
    from pistachio.agents.analysis import calibration

    monkeypatch.setattr(compute_bridge.store, "keys", Mock(side_effect=OSError("private details")))
    assert calibration.recent(compute_bridge.store) == []
    assert "private details" not in caplog.text
