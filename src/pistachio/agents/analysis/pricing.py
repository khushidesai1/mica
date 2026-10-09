"""Refresh public regional SKUs in the control plane, with a bounded durable cache."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from .compute_quotes import Rates
from .config import CHECKPOINT_PREFIX
from .storage import Storage

CATALOG = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"


def from_skus(skus: list[dict[str, Any]], region: str, now: float) -> Rates:
    selected: dict[str, dict[str, Any]] = {}
    for sku in skus:
        if region not in sku.get("serviceRegions", []):
            continue
        desc = sku.get("description", "")
        key = (
            "cpu_hour"
            if desc.startswith("Spot Preemptible N2 Instance Core running in ")
            else "memory_gib_hour"
            if desc.startswith("Spot Preemptible N2 Instance Ram running in ")
            else "balanced_gb_hour"
            if desc == "Balanced PD Capacity"
            else "ssd_gb_hour"
            if desc == "SSD backed PD Capacity"
            else ""
        )
        if key:
            if key in selected:
                raise ValueError("Ambiguous regional Compute pricing.")
            usage = "Preemptible" if key in {"cpu_hour", "memory_gib_hour"} else "OnDemand"
            if sku.get("category", {}).get("usageType") != usage:
                raise ValueError("Compute SKU provisioning does not match.")
            selected[key] = sku
    if len(selected) != 4:
        raise ValueError("Regional N2 Spot and persistent-disk prices are incomplete.")
    prices: dict[str, Decimal] = {}
    for key, sku in selected.items():
        info = sorted(sku["pricingInfo"], key=lambda row: row["effectiveTime"])[-1]
        expr = info["pricingExpression"]
        expected = (
            "h" if key == "cpu_hour" else ("GiBy.h" if key == "memory_gib_hour" else "GiBy.mo")
        )
        tiers = expr["tieredRates"]
        if expr["usageUnit"] != expected or len(tiers) != 1 or tiers[0]["startUsageAmount"] != 0:
            raise ValueError("Unsupported Compute SKU units or price tiers.")
        money = tiers[0]["unitPrice"]
        if money["currencyCode"] != "USD":
            raise ValueError("Compute price must be in USD.")
        price = Decimal(money["units"]) + Decimal(money["nanos"]) / 10**9
        if expected == "GiBy.mo":
            if expr["baseUnit"] != "By.s":
                raise ValueError("Unsupported disk price base units.")
            # Use the provider's actual month conversion, never assume 730 hours.
            price *= Decimal(2**30 * 3600) / Decimal(str(expr["baseUnitConversionFactor"]))
        prices[key] = price
    return Rates(
        region=region,
        fetched_at=now,
        expires_at=now + 86400,
        sku_ids=[sku["skuId"] for sku in selected.values()],
        **prices,
    )


def refresh(region: str) -> Rates:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(  # type: ignore[no-untyped-call]
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    started = time.monotonic()
    token: str = ""
    rows: list[dict[str, Any]] = []
    with AuthorizedSession(credentials) as client:  # type: ignore[no-untyped-call]
        for _ in range(30):
            if time.monotonic() - started > 90:
                raise ValueError("Price refresh exceeded its time allowance.")
            response = client.get(
                CATALOG,
                params={"currencyCode": "USD", "pageSize": "5000", "pageToken": token},
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
            rows.extend(data.get("skus", []))
            token = str(data.get("nextPageToken", ""))
            if not token:
                return from_skus(rows, region, time.time())
    raise ValueError("Price catalog pagination exceeded its limit.")


def current(storage: Storage, region: str) -> Rates:
    key = f"{CHECKPOINT_PREFIX}/pricing/{region}.json"
    try:
        rates = Rates.model_validate_json(storage.get(key)[0])
        rates.validate_time(time.time(), region)
        return rates
    except (FileNotFoundError, ValueError):
        rates = refresh(region)
        storage.put(key, rates.model_dump_json().encode())
        return rates
