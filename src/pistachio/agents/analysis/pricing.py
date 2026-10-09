"""Refresh public regional SKUs in the control plane, with a bounded durable cache."""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import requests

from .compute_quotes import Provisioning, Rates
from .config import CHECKPOINT_PREFIX
from .storage import Storage

CATALOG = "https://cloudbilling.googleapis.com/v1/services/6F81-5844-456A/skus"


def from_skus(
    skus: list[dict[str, Any]], region: str, now: float, provisioning: Provisioning = "SPOT"
) -> Rates:
    if provisioning not in {"SPOT", "STANDARD"}:
        raise ValueError("Unsupported compute provisioning mode.")
    prefix = "Spot Preemptible " if provisioning == "SPOT" else ""
    selected: dict[str, dict[str, Any]] = {}
    for sku in skus:
        if region not in sku.get("serviceRegions", []):
            continue
        desc = sku.get("description", "")
        key = (
            "cpu_hour"
            if desc.startswith(f"{prefix}N2 Instance Core running in ")
            else "memory_gib_hour"
            if desc.startswith(f"{prefix}N2 Instance Ram running in ")
            else "balanced_gb_hour"
            if desc == "Balanced PD Capacity"
            else "ssd_gb_hour"
            if desc == "SSD backed PD Capacity"
            else ""
        )
        if key:
            if key in selected:
                raise ValueError("Ambiguous regional Compute pricing.")
            usage = (
                "Preemptible"
                if provisioning == "SPOT" and key in {"cpu_hour", "memory_gib_hour"}
                else "OnDemand"
            )
            if sku.get("category", {}).get("usageType") != usage:
                raise ValueError("Compute SKU provisioning does not match.")
            selected[key] = sku
    if len(selected) != 4:
        raise ValueError(f"Regional N2 {provisioning} and persistent-disk prices are incomplete.")
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
        provisioning=provisioning,
        fetched_at=now,
        expires_at=now + 86400,
        sku_ids=[sku["skuId"] for sku in selected.values()],
        **prices,
    )


def refresh(region: str) -> dict[Provisioning, Rates]:
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(  # type: ignore[no-untyped-call]
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    started = time.monotonic()
    token: str = ""
    rows: list[dict[str, Any]] = []
    try:
        with AuthorizedSession(credentials) as client:  # type: ignore[no-untyped-call]
            for _ in range(30):
                remaining = 90 - (time.monotonic() - started)
                if remaining <= 0:
                    raise ValueError("Price refresh exceeded its time allowance.")
                response = client.get(
                    CATALOG,
                    params={"currencyCode": "USD", "pageSize": "5000", "pageToken": token},
                    timeout=min(30, remaining),
                )
                response.raise_for_status()
                data = response.json()
                rows.extend(data.get("skus", []))
                token = str(data.get("nextPageToken", ""))
                if not token:
                    now = time.time()
                    return {
                        "SPOT": from_skus(rows, region, now, "SPOT"),
                        "STANDARD": from_skus(rows, region, now, "STANDARD"),
                    }
    except requests.RequestException as exc:
        raise ValueError("Compute prices are temporarily unavailable; retry shortly.") from exc
    raise ValueError("Price catalog pagination exceeded its limit.")


def current(storage: Storage, region: str, provisioning: Provisioning = "SPOT") -> Rates:
    if provisioning not in {"SPOT", "STANDARD"}:
        raise ValueError("Unsupported compute provisioning mode.")
    suffix = "-standard" if provisioning == "STANDARD" else ""
    key = f"{CHECKPOINT_PREFIX}/pricing/{region}{suffix}.json"
    try:
        rates = Rates.model_validate_json(storage.get(key)[0])
        rates.validate_time(time.time(), region)
        if rates.provisioning != provisioning:
            raise ValueError("Cached compute prices do not match the provisioning mode.")
        return rates
    except (FileNotFoundError, ValueError):
        modes = refresh(region)
        for mode, rates in modes.items():
            suffix = "-standard" if mode == "STANDARD" else ""
            storage.put(
                f"{CHECKPOINT_PREFIX}/pricing/{region}{suffix}.json",
                rates.model_dump_json().encode(),
            )
        return modes[provisioning]
