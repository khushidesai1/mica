"""Application-owned preferences, separate from model-edited memory."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictBool

from .identity import user_key
from .storage import Conflict, Storage


class Preferences(BaseModel):
    version: Literal[1] = 1
    return_scripts: StrictBool | None = None
    applied_events: list[str] = Field(default_factory=list, max_length=100)


def key(email: str) -> str:
    return f"projects/_users/{user_key(email)}/settings.json"


def load(storage: Storage, email: str) -> tuple[Preferences, str | None]:
    try:
        raw, generation = storage.get(key(email))
        return Preferences.model_validate_json(raw), generation
    except FileNotFoundError:
        return Preferences(), None


def effective(storage: Storage, email: str, default: bool = False) -> bool:
    value = load(storage, email)[0].return_scripts
    return default if value is None else value


def set_scripts(storage: Storage, email: str, enabled: bool, event_id: str) -> None:
    for _ in range(10):
        prefs, generation = load(storage, email)
        if event_id in prefs.applied_events:
            return
        prefs.return_scripts = enabled
        prefs.applied_events = [*prefs.applied_events[-99:], event_id]
        try:
            storage.put_if(
                key(email), prefs.model_dump_json(exclude_none=True).encode(), generation
            )
            return
        except Conflict:
            continue
    raise Conflict("Your settings changed at the same time. Please retry.")


def command(
    storage: Storage, email: str, args: tuple[str, ...], default: bool, event_id: str
) -> str:
    if args:
        enabled = args[0] == "on"
        set_scripts(storage, email, enabled, event_id)
        return (
            "I'll attach the scripts I run from now on. Say `scripts off` to stop."
            if enabled
            else "I'll stop attaching scripts."
        )
    prefs, _ = load(storage, email)
    value = default if prefs.return_scripts is None else prefs.return_scripts
    source = "deployment default" if prefs.return_scripts is None else "your setting"
    return (
        f"Script attachments are {'on' if value else 'off'} ({source}). "
        "Use `scripts on` or `scripts off`."
    )
