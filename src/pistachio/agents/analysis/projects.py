from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .config import Settings
from .storage import Storage


class Project(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9-]{3,40}$")
    slack_channel_id: str
    data_uri: str
    created_by: str
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def data_path(self, settings: Settings) -> Path:
        uri = urlsplit(self.data_uri)
        if (
            uri.scheme != "gs"
            or uri.netloc not in settings.csv("data_buckets")
            or uri.query
            or uri.fragment
            or "%" in uri.path
            or "\\" in uri.path
            or any(part in {".", ".."} for part in uri.path.split("/"))
        ):
            raise ValueError("Use a gs:// URI inside an allowed data bucket, without traversal")
        root = settings.data_mount_root.resolve() / uri.netloc
        path = (root / uri.path.lstrip("/")).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Data path escapes the mounted bucket")
        return path


class Projects:
    def __init__(self, storage: Storage, settings: Settings) -> None:
        self.storage, self.settings = storage, settings
        self.channels: dict[str, Project] = {}

    def reload(self) -> None:
        channels = {}
        for key in self.storage.keys("projects/"):
            if re.fullmatch(r"projects/[a-z0-9-]{3,40}/project.json", key):
                project = Project.model_validate_json(self.storage.get(key)[0])
                # Validate only the selected project at use time; another channel may use
                # a bucket not mounted by this worker, or a bucket that was since deleted.
                if project.slack_channel_id:
                    if project.slack_channel_id in channels:
                        raise ValueError(
                            "Duplicate project binding; fix project.json before starting"
                        )
                    channels[project.slack_channel_id] = project
        self.channels = channels

    def bind(self, channel: str, slug: str, uri: str, user: str, force: bool = False) -> Project:
        project = Project(slug=slug, slack_channel_id=channel, data_uri=uri, created_by=user)
        project.data_path(self.settings)
        current = self.channels.get(channel)
        if current and current.slug == slug and current.data_uri == uri:
            return current  # Retrying interrupted setup does not rebind or erase the brief.
        if current and not force:
            raise ValueError("This channel is already linked. Add --force to replace its binding.")
        try:
            other = Project.model_validate_json(
                self.storage.get(f"projects/{slug}/project.json")[0]
            )
            if other.slack_channel_id and other.slack_channel_id != channel:
                raise ValueError("That project is already linked to another channel.")
        except FileNotFoundError:
            pass
        if current and current.slug != slug:
            self.save(current.model_copy(update={"slack_channel_id": ""}))
        self.save(project)
        self.reload()
        return project

    def save(self, project: Project) -> None:
        self.storage.put(
            f"projects/{project.slug}/project.json", project.model_dump_json().encode()
        )

    def brief(self, slug: str) -> tuple[str, str | None]:
        try:
            content, generation = self.storage.get(f"projects/{slug}/PROJECT.md")
            return content.decode("utf-8"), generation
        except FileNotFoundError:
            return "No brief yet. Rely on listing and inspecting the data folder.", None

    def save_brief(self, slug: str, data: bytes) -> None:
        if len(data) >= 100_000:
            raise ValueError("The brief must be under 100 KB.")
        data.decode("utf-8")
        self.storage.put(f"projects/{slug}/PROJECT.md", data)
