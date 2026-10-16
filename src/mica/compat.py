"""Compatibility for deployed configuration and previously generated analysis scripts."""

from __future__ import annotations

import os

from pydantic import AliasChoices


def setting_alias(name: str) -> AliasChoices:
    return AliasChoices("MICA_" + name.upper(), "PISTACHIO_" + name.upper())


def getenv(name: str, default: str = "") -> str:
    """Prefer the current name, including an explicitly empty value."""
    return os.environ.get(name, os.environ.get(name.replace("MICA_", "PISTACHIO_", 1), default))


def legacy_environment(values: dict[str, str]) -> dict[str, str]:
    """Export both names to already-saved scripts and pinned older worker images."""
    return {
        **{
            key.replace("MICA_", "PISTACHIO_", 1): value
            for key, value in values.items()
            if key.startswith("MICA_")
        },
        **values,
    }
