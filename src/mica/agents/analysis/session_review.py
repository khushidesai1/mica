"""Bounded import review contract shared by workers and the controller."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from . import project_memory
from .script_capture import read_regular

REVIEW_BYTES = 32000
ANCHOR = r"t[0-9]{9}_[0-9]{6}"
PathText = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: PathText
    summary: str = Field(min_length=1, max_length=300)

    @field_validator("path")
    @classmethod
    def valid_path(cls, value: str) -> str:
        project_memory.valid_path(value)
        if value.startswith(("threads/", "archive/", "_unmerged/")):
            raise ValueError("Review items must name active project entries")
        return value


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    next_anchor: str | None = Field(default=None, pattern="^" + ANCHOR + "$")
    saved: list[Item] = Field(default_factory=list, max_length=10)
    highlights: dict[str, list[PathText] | str] = Field(default_factory=dict, max_length=10)

    @model_validator(mode="after")
    def bounded(self) -> Review:
        if len(self.model_dump_json().encode()) > REVIEW_BYTES:
            raise ValueError("Import review is too large")
        return self

    @field_validator("highlights")
    @classmethod
    def valid_highlights(cls, value: dict[str, list[str] | str]) -> dict[str, list[str] | str]:
        for key, paths in value.items():
            if not re.fullmatch(r"h(?:[1-9]|10)", key):
                raise ValueError("Invalid highlight identifier")
            if isinstance(paths, str):
                if paths != "not_found":
                    raise ValueError("Use paths or not_found")
            else:
                if not 1 <= len(paths) <= 10:
                    raise ValueError("Provide one to ten paths")
                for path in paths:
                    Item.valid_path(path)
        return value


def read(work: Path, highlights: list[str] | None = None) -> Review:
    raw = read_regular(work, Path("notes/import-review.json"))
    if len(raw) > REVIEW_BYTES:
        raise ValueError("Import review is too large")
    review = Review.model_validate_json(raw)
    if highlights is not None and set(review.highlights) != set(highlights):
        raise ValueError("Account for every supplied highlight ID exactly once")
    return review


def origin(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("ambient") or {}
    return value if value.get("kind") == "session_import" else {}
