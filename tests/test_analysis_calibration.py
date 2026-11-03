"""Measured history keeps failures, remains scoped and is optional."""

import json

from mica.agents.analysis import calibration, recovery
from mica.agents.analysis.storage import LocalStorage


def test_history_is_idempotent_scoped_and_accepts_missing_final_metrics(tmp_path):
    store = LocalStorage(tmp_path)
    event = "a" * 32
    state = {"attempt": 0, "tier": "standard", "project_id": "project", "started": 10}
    prefix = recovery.event_prefix(event)
    store.put(
        f"{prefix}/resources-a0.json",
        json.dumps(
            {
                "event_id": event,
                "attempt": 0,
                "oom_kills": 1,
                "peak_gib": 63,
                "wall_s": 120,
                "commands": [{"wall_s": 60}],
            }
        ).encode(),
    )
    calibration.capture(store, event, state, "oom")
    calibration.capture(store, event, state, "oom")
    calibration.capture(store, event, dict(state, attempt=1, tier="large"), "complete")
    store.put(f"{calibration.PREFIX}project/legacy.json", b'{"estimated": {}}')
    rows = calibration.recent(store, "project")
    assert len(rows) == 2
    original = next(r for r in rows if r["attempt"] == 0)
    assert "category" not in original
    saved = store.get(f"{calibration.PREFIX}project/{event}-a0.json")[0]
    assert "category" not in json.loads(saved)
    legacy = calibration.Row.model_validate({**original, "category": "full_ingest"})
    assert legacy.category == "full_ingest"
    assert calibration.Row.model_validate(original).category == "analysis"
    assert original["peak_gib"] == 63 and original["outcome"] == "oom"
    assert not original["telemetry_complete"]
    assert next(r for r in rows if r["attempt"] == 1)["peak_gib"] is None
    assert not calibration.history(store, "other")


def test_history_failure_is_nonfatal(tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)

    def fail(*args):
        raise OSError("unavailable")

    monkeypatch.setattr(store, "keys", fail)
    assert calibration.history(store, "project") == []
