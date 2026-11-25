import json
from types import SimpleNamespace

import pytest

from mica.agents.analysis.storage import LocalStorage
from mica.agents.analysis.transfers import Transfers, marker_state, markers, stamp


def test_backfilled_marker_is_not_displayed_as_verified():
    assert marker_state({"done": {"provenance": "backfill"}}) == (
        "done (backfilled; listing-only, not verified)"
    )
    assert marker_state({"done": {}}) == "done (marker; manifest not rechecked here)"


def test_active_transfer_still_takes_precedence():
    assert (
        marker_state(
            {
                "done": {"provenance": "backfill"},
                "in_progress": {"heartbeat": stamp()},
            }
        )
        == "in_progress"
    )


@pytest.mark.parametrize("scope", ["raw", "extra", None])
async def test_destination_text_controls_availability(tmp_path, monkeypatch, scope):
    store = LocalStorage(tmp_path)
    record = {"sample": "TMA7", "run_id": "october"}
    store.put("history/TMA7.done.json", json.dumps(record).encode())
    # Renamed defective folders must not reserve the original sample name.
    store.put("raw/TMA7_defective/done.txt", b"old marker")
    store.put("extra/TMA7_defective/done.txt", b"old marker")
    if scope:
        store.put(f"{scope}/TMA7/done.txt", b"done")
    monkeypatch.setattr("mica.agents.analysis.transfers.GCSStorage", lambda _: store)
    service = Transfers.__new__(Transfers)
    pending = {
        "config": {"bucket": "data", "raw_prefix": "raw/", "additional_prefix": "extra/",
                   "status_prefix": "history/"},
        "request": {"sample": "remote-slide", "as_sample": "TMA7"},
    }
    if scope:
        with pytest.raises(ValueError, match="done.txt"):
            await service.available(pending)
    else:
        result = await service.available(pending)
        assert result == {"completion_record": record}
        assert "historical" in marker_state(result)


async def test_text_without_service_record_blocks_and_live_lease_survives(tmp_path):
    store = LocalStorage(tmp_path)
    store.put("raw_outputs/raw/TMA7/done.txt", b"manual marker")
    result = markers(store, "TMA7")
    assert result["done"]["scopes"] == ["raw"]
    assert "not verified" in marker_state(result)
    store.path("raw_outputs/raw/TMA7/done.txt").unlink()
    store.put("transfer_status/TMA7.done.json", b'{"run_id":"old"}')
    store.put("transfer_status/TMA7.in_progress.json", json.dumps({"heartbeat": stamp()}).encode())
    service = Transfers.__new__(Transfers)
    service.destination_markers = lambda *_: markers(store, "TMA7")
    with pytest.raises(ValueError, match="live transfer"):
        await service.available({"request": {"sample": "TMA7", "as_sample": ""}})


async def test_marker_storage_error_is_not_treated_as_missing():
    store = SimpleNamespace(get=lambda _: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(PermissionError):
        markers(store, "TMA7")
