"""Offline contracts using unmodified regional SKUs captured from Google's catalog."""

import copy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

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
    refresh = Mock(return_value={"SPOT": spot, "STANDARD": standard})
    monkeypatch.setattr(pricing, "refresh", refresh)
    old_key = f"{CHECKPOINT_PREFIX}/pricing/{REGION}.json"
    store.put(old_key, spot.model_dump_json().encode())
    assert pricing.current(store, REGION) == spot
    refresh.assert_not_called()
    assert pricing.current(store, REGION, "STANDARD") == standard
    assert pricing.current(store, REGION, "STANDARD") == standard
    refresh.assert_called_once_with(REGION)
    assert Rates.model_validate_json(store.get(old_key)[0]) == spot
    new_key = f"{CHECKPOINT_PREFIX}/pricing/{REGION}-standard.json"
    assert Rates.model_validate_json(store.get(new_key)[0]) == standard
    # A stale/misfiled Spot record must never price an on-demand launch.
    store.put(new_key, spot.model_dump_json().encode())
    assert pricing.current(store, REGION, "STANDARD") == standard
    assert refresh.call_count == 2


@pytest.fixture
def catalog(monkeypatch, skus):
    import google.auth
    from google.auth.transport import requests as transport

    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value.json.return_value = {"skus": skus}
    monkeypatch.setattr(google.auth, "default", Mock(return_value=(Mock(), "test")))
    monkeypatch.setattr(transport, "AuthorizedSession", Mock(return_value=client))
    return client


def test_one_catalog_download_fills_both_caches(catalog, tmp_path):
    store = LocalStorage(tmp_path)
    spot = pricing.current(store, REGION)
    standard = pricing.current(store, REGION, "STANDARD")
    assert spot.provisioning == "SPOT" and standard.provisioning == "STANDARD"
    catalog.get.assert_called_once()
    assert catalog.get.call_args.kwargs["timeout"] == 30
    assert spot.fetched_at == standard.fetched_at


def test_read_timeout_is_a_retryable_value_error(catalog):
    catalog.get.side_effect = requests.ReadTimeout("private details")
    with pytest.raises(
        ValueError, match="Compute prices are temporarily unavailable; retry shortly"
    ):
        pricing.refresh(REGION)


async def test_price_timeout_returns_422(catalog, monkeypatch, tmp_path, repository):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException, Response
    from starlette.requests import Request

    from pistachio.agents.analysis.config import Settings
    from pistachio.api import slack_analysis

    record = {
        "compute_version": 1,
        "attempt": 0,
        "project_id": "project",
        "principal": "user",
        "compute_limit": 50,
    }
    bridge = SimpleNamespace(
        repository=repository,
        settings=Settings(_env_file=None),
        app=SimpleNamespace(gcp_region=REGION),
    )
    monkeypatch.setattr(
        slack_analysis,
        "worker_context",
        AsyncMock(return_value=(bridge, LocalStorage(tmp_path), record)),
    )
    catalog.get.side_effect = requests.ReadTimeout("private details")

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(
                {
                    "attempt": 0,
                    "action": "estimate",
                    "estimate": {
                        "tier": "standard",
                        "hours_low": 0.1,
                        "hours_high": 0.2,
                        "peak_gib": 1,
                        "task_peak_gib": 1,
                        "disk_gb": 1,
                        "basis": "Measured fixture for timeout regression.",
                    },
                }
            ).encode(),
        }

    with pytest.raises(HTTPException) as exc:
        await slack_analysis.compute_operation(
            "a" * 32, Request({"type": "http", "headers": []}, receive=receive), Response()
        )
    assert exc.value.status_code == 422
    assert exc.value.detail == "Compute prices are temporarily unavailable; retry shortly."
