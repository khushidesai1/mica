"""Operator-owned background task definitions; never accepted from model output."""

from __future__ import annotations

import re
from string import Formatter
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from .tiers import GPUType, Sizing


def sample_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("Use a sample name without slashes or spaces")
    return value


class AmbientPerson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    slack_user: str = Field(pattern=r"^[UW][A-Z0-9]+$")

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        from .identity import recipient_email

        return recipient_email(value)


class AmbientWorker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Literal["standard", "large", "xlarge", "gpu"] = "standard"
    on_demand: bool = True
    max_hours: float = Field(default=1, ge=1 / 6, le=336, allow_inf_nan=False)
    gpu_type: GPUType | None = None
    gpu_sizing: Sizing | None = None

    @model_validator(mode="after")
    def gpu_requirements(self) -> Self:
        if self.tier == "gpu" and self.gpu_sizing is None:
            raise ValueError("Ambient GPU tasks need operator-supplied gpu_sizing")
        if self.tier != "gpu" and (self.gpu_sizing or self.gpu_type):
            raise ValueError("GPU sizing applies only to GPU tasks")
        return self


class AmbientTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    project: str = Field(pattern=r"^(?:p_[a-f0-9]{16}|[a-z0-9-]{3,40})$")
    enabled: bool = False
    dry_run: bool = True
    enabled_at: AwareDatetime
    authorized_by: AmbientPerson
    markers: list[str] = Field(min_length=1, max_length=8)
    skip_if_exists: str
    output: str
    recipe: str
    recipients: list[AmbientPerson] = Field(min_length=1, max_length=20)
    worker: AmbientWorker = Field(default_factory=AmbientWorker)
    max_concurrent: int = Field(default=1, ge=1, le=10)
    max_starts_per_day: int = Field(default=4, ge=1, le=100)
    task: str = Field(min_length=5, max_length=8000)
    return_scripts: bool | None = None

    @field_validator("recipe")
    @classmethod
    def recipe_path(cls, value: str) -> str:
        from .project_memory import valid_path

        valid_path(value)
        return value

    @model_validator(mode="after")
    def templates(self) -> Self:
        for value in [*self.markers, self.skip_if_exists, self.output]:
            if (
                len(value) > 800
                or value.count("{sample}") != 1
                or not re.fullmatch(r"[A-Za-z0-9_./{}-]+", value)
                or "{sample}" not in value.split("/")
                or any(p in {".", ".."} for p in value.split("/"))
                or value.startswith("/")
                or "//" in value
                or any(c in value.replace("{sample}", "") for c in "{}")
            ):
                raise ValueError("Paths require one {sample} segment and literal relative folders")
        if any(p.endswith("/") for p in self.markers):
            raise ValueError("Markers must name objects")
        if not self.output.endswith("/") or not self.skip_if_exists.endswith("/"):
            raise ValueError("Output and skip folders must end in slash")
        if len(set(self.markers)) != len(self.markers):
            raise ValueError("Duplicate marker templates")
        # The first template determines the delimiter listing; no recursive sample discovery.
        if not self.markers[0].split("{sample}")[0]:
            raise ValueError("The first marker needs a literal parent folder")
        for _, field, spec, conversion in Formatter().parse(self.task):
            if field is not None and (
                field not in {"sample", "markers", "output", "recipe"} or spec or conversion
            ):
                raise ValueError("Unknown task template field or format")
        if len({p.email for p in self.recipients}) != len(self.recipients) or len(
            {p.slack_user for p in self.recipients}
        ) != len(self.recipients):
            raise ValueError("Duplicate recipients")
        for person in self.recipients:
            if (person.email == self.authorized_by.email) != (
                person.slack_user == self.authorized_by.slack_user
            ):
                raise ValueError("Owner identity conflicts with a recipient")
        return self

    def render(self, value: str, sample: str) -> str:
        return value.format(sample=sample_name(sample))

    def output_uri(self, data_uri: str, sample: str) -> str:
        from urllib.parse import urlsplit

        return f"gs://{urlsplit(data_uri).netloc}/{self.render(self.output, sample)}"
