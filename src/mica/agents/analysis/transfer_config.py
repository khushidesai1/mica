"""Validated transfer requests and destinations frozen by application authorization."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

SAMPLE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"  # Bucket folder names stay conservative.


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
    destination: str = Field(
        default="", max_length=2048,
        description="Proposed raw-data gs:// folder inferred from project context. Prefer the "
        "parent base; a final folder ending in as_sample or sample is also accepted. "
        "The preview appends that sample folder exactly once and requires approval.",
    )
    additional_destination: str = Field(
        default="", max_length=2048,
        description="Proposed additional-files gs:// parent base or final sample folder. "
        "Infer from the same project layout; it need not be repeated in the user's message.",
    )

    @field_validator("sample")
    @classmethod
    def remote_sample(cls, value: str) -> str:
        # The source may be a hand-named AtoMx slide folder, so only path safety applies.
        if value in {".", ".."} or "/" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("Use a single AtoMx folder name without slashes")
        return value

    @field_validator("as_sample")
    @classmethod
    def safe_sample(cls, value: str) -> str:
        if value and not re.fullmatch(SAMPLE, value):
            raise ValueError("Use a sample name without slashes or spaces")
        return value

    @field_validator("export_root")
    @classmethod
    def safe_root(cls, value: str) -> str:
        # People paste export folders with or without the leading and trailing slash.
        value = value.strip()
        value = "/" + value.strip("/") if value.strip("/") else ""
        if value and (
            any(p in {"", ".", ".."} for p in value[1:].split("/"))
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Use an absolute export folder without traversal or trailing slash")
        return value

    @model_validator(mode="after")
    def bucket_name(self) -> TransferRequest:
        if not re.fullmatch(SAMPLE, self.as_sample or self.sample):
            raise ValueError(
                "Name the bucket folder with as <name> (letters, numbers, dot, dash, underscore)"
            )
        return self


def age(value: Any) -> float:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (datetime.now(UTC) - stamp).total_seconds()
    except (ValueError, TypeError):
        return float("inf")
