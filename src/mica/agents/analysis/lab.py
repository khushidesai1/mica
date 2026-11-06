"""Live lab admission and project-use grants shared by the API and workers."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from .identity import recipient_email
from .storage import Storage

KEY = "projects/_access/lab.json"


class Lab(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    members: set[str] = Field(default_factory=set)
    all_projects: set[str] = Field(default_factory=set)

    @field_validator("members", "all_projects")
    @classmethod
    def emails(cls, values: set[str]) -> set[str]:
        return {recipient_email(value) for value in values}

    @field_serializer("members", "all_projects")
    def sorted_emails(self, values: set[str]) -> list[str]:
        return sorted(values)

    @model_validator(mode="after")
    def projects_require_membership(self) -> Self:
        if not self.all_projects <= self.members:
            raise ValueError("all_projects must be a subset of members")
        return self


def load(storage: Storage) -> Lab:
    """Do not cache grants: removals must affect the next authorization check."""
    try:
        return Lab.model_validate_json(storage.get(KEY)[0])
    except FileNotFoundError:
        return Lab()
