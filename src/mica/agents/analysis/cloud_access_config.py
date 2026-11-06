"""Operator-configured access bundles; models never choose projects or roles."""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .identity import recipient_email


def grant_role(value: str) -> str:
    if (
        not re.fullmatch(r"roles/[A-Za-z][A-Za-z0-9_.]{0,100}", value)
        or value == "roles/owner"
        or value.startswith(("roles/iam.", "roles/resourcemanager."))
    ):
        raise ValueError("Use a predefined role without owner or IAM administration rights.")
    return value


class CloudAccessBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=60)
    gcp_project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    roles: tuple[str, ...] = Field(min_length=1, max_length=10)
    channel: str = Field(pattern=r"^T[A-Z0-9]+:[CG][A-Z0-9]+$")
    approver: str = Field(default="", pattern=r"^(?:T[A-Z0-9]+:[UW][A-Z0-9]+)?$")
    approver_emails: tuple[str, ...] = Field(min_length=1, max_length=5)
    allow_lab_members: bool = False
    guide_url: str = Field(max_length=2048)
    welcome: str = Field(min_length=1, max_length=12000)

    @field_validator("roles")
    @classmethod
    def valid_roles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({grant_role(value) for value in values}))

    @field_validator("approver_emails")
    @classmethod
    def emails(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({recipient_email(value) for value in values}))

    @field_validator("guide_url")
    @classmethod
    def guide(cls, value: str) -> str:
        uri = urlsplit(value)
        if uri.scheme != "https" or not uri.hostname or uri.username or uri.password:
            raise ValueError("Use an HTTPS setup guide URL.")
        return value


class CloudAccessProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    email: str = Field(max_length=254)
    note: Annotated[str, Field(min_length=1, max_length=500)]

    @field_validator("email")
    @classmethod
    def target(cls, value: str) -> str:
        return recipient_email(value)

    @field_validator("note")
    @classmethod
    def required_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Add a short note about the requested access.")
        return value.strip()
