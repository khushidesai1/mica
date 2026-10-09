"""Offline contracts using unmodified regional SKUs captured from Google's catalog."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from pistachio.agents.analysis import pricing
from pistachio.agents.analysis.compute_quotes import Estimate, Quote, Rates, quote
from pistachio.agents.analysis.config import CHECKPOINT_PREFIX
from pistachio.agents.analysis.storage import LocalStorage

NOW = 1_800_000_000.0
REGION = "us-central1"


@pytest.fixture
def skus():
    path = Path(__file__).parent / "fixtures/compute/n2-us-central1-skus.json"
    return json.loads(path.read_text())["skus"]


def test_live_catalog_supports_both_modes_with_identical_disk_rates(skus):
    spot = pricing.from_skus(skus, REGION, NOW)
    standard = pricing.from_skus(skus, REGION, NOW, provisioning="STANDARD")
    assert spot.provisioning == "SPOT"
    assert standard.provisioning == "STANDARD"
    assert standard.cpu_hour > spot.cpu_hour > 0
    assert standard.memory_gib_hour > spot.memory_gib_hour > 0
    assert standard.balanced_gb_hour == spot.balanced_gb_hour
    assert standard.ssd_gb_hour == spot.ssd_gb_hour
    assert set(standard.sku_ids) == {
        "5B01-D157-A097",
        "BB77-5FDA-69D9",
        "6AE1-525F-8B80",
        "B188-61DD-52E4",
    }


@pytest.mark.parametrize("mode", ["SPOT", "STANDARD"])
def test_wrong_usage_type_is_rejected(skus, mode):
    rows = copy.deepcopy(skus)
    for row in rows:
        if "N2 Instance" in row["description"]:
            row["category"]["usageType"] = "Commit1Yr"
    with pytest.raises(ValueError, match="provisioning"):
        pricing.from_skus(rows, REGION, NOW, provisioning=mode)


@pytest.mark.parametrize("mode", ["SPOT", "STANDARD"])
def test_quote_identifies_provisioning_and_legacy_records_default_to_spot(skus, mode):
    rates = pricing.from_skus(skus, REGION, NOW, provisioning=mode)
    estimate = Estimate(
        tier="large",
        hours_low=1,
        hours_high=2,
        peak_gib=100,
        task_peak_gib=50,
        disk_gb=50,
        basis="Synthetic estimate for offline pricing checks.",
    )
    q = quote(estimate, rates, spent_usd=0, limit_usd=50, now=NOW)
    assert q.provisioning == mode
    assert f"({'Spot' if mode == 'SPOT' else 'on-demand'})" in q.notice()
    old_rates = rates.model_dump(exclude={"provisioning"})
    old_quote = q.model_dump(exclude={"provisioning"})
    assert Rates.model_validate(old_rates).provisioning == "SPOT"
    assert Quote.model_validate(old_quote).provisioning == "SPOT"


def test_cache_separates_modes_and_preserves_legacy_spot_key(skus, tmp_path, monkeypatch):
    store = LocalStorage(tmp_path)
    monkeypatch.setattr(pricing.time, "time", lambda: NOW)
    spot = pricing.from_skus(skus, REGION, NOW)
    standard = pricing.from_skus(skus, REGION, NOW, provisioning="STANDARD")
    refresh = Mock(
        side_effect=lambda region, provisioning: {"SPOT": spot, "STANDARD": standard}[provisioning]
    )
    monkeypatch.setattr(pricing, "refresh", refresh)
    old_key = f"{CHECKPOINT_PREFIX}/pricing/{REGION}.json"
    store.put(old_key, spot.model_dump_json().encode())
    assert pricing.current(store, REGION) == spot
    refresh.assert_not_called()
    assert pricing.current(store, REGION, "STANDARD") == standard
    assert pricing.current(store, REGION, "STANDARD") == standard
    refresh.assert_called_once_with(REGION, "STANDARD")
    assert Rates.model_validate_json(store.get(old_key)[0]) == spot
    new_key = f"{CHECKPOINT_PREFIX}/pricing/{REGION}-standard.json"
    assert Rates.model_validate_json(store.get(new_key)[0]) == standard
    # A stale/misfiled Spot record must never price an on-demand launch.
    store.put(new_key, spot.model_dump_json().encode())
    assert pricing.current(store, REGION, "STANDARD") == standard
    assert refresh.call_count == 2
