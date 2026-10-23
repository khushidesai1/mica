"""Validated transfer requests and destinations frozen by application authorization."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class TransferProject(BaseModel):
    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]+[a-z0-9]$")
    login_secret: str = ""  # Legacy configuration only; never used for personal connections.
    raw_prefix: str = "raw_outputs/raw/"
    additional_prefix: str = "raw_outputs/additional_files/"
    status_prefix: str = "transfer_status/"
    layout_source: str = "configured"
    max_concurrent: int = Field(default=1, ge=1, le=10)

    @field_validator("raw_prefix", "additional_prefix", "status_prefix")
    @classmethod
    def safe_prefix(cls, value: str) -> str:
        from .write_access import scope

        if (
            not value
            or value.startswith("/")
            or scope("gs://validation-bucket/" + value) != "gs://validation-bucket/" + value
        ):
            raise ValueError("Use a normalized nonempty destination prefix ending in slash")
        return value


class TransferRequest(BaseModel):
    sample: str = Field(min_length=1, max_length=128)
    export_root: str = Field(default="", max_length=2048)
    as_sample: str = Field(default="", max_length=128)
    destination: str = Field(default="", max_length=2048)
    additional_destination: str = Field(default="", max_length=2048)

    @field_validator("sample", "as_sample")
    @classmethod
    def safe_sample(cls, value: str) -> str:
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
            raise ValueError("Use a sample name without slashes or spaces")
        return value

    @field_validator("export_root")
    @classmethod
    def safe_root(cls, value: str) -> str:
        if value and (
            not value.startswith("/")
            or any(p in {"", ".", ".."} for p in value[1:].split("/"))
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Use an absolute export folder without traversal or trailing slash")
        return value


def age(value: Any) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (datetime.now(UTC) - stamp).total_seconds()
    except (ValueError, TypeError):
        return float("inf")
