"""Per-user Slack Home and suggestions, backed by the existing project registry."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from slack_sdk.web.async_client import AsyncWebClient

from . import approvals, identity
from .registry import Registry, RegistryDoc
from .transport import SlackTransport

if TYPE_CHECKING:
    from fastapi import BackgroundTasks

    from .batch import BatchBridge
    from .storage import Storage

log = logging.getLogger(__name__)
PREFIX = "mica_project_picker_"


def key(identifier: str) -> str:
    return "slack-home:" + hashlib.sha256(identifier.encode()).hexdigest()


def home(
    doc: RegistryDoc, email: str, active: str | None, recent: dict[str, float],
) -> dict[str, Any]:
    projects = sorted(
        doc.visible(email).items(), key=lambda item: (recent.get(item[0], 0), item[1].created_at),
        reverse=True,
    )
    selected = projects[:100]
    if active in dict(projects) and active not in dict(selected):
        selected[-1:] = [(active, doc.projects[active])]
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "plain_text", "text": (
            "Choose the project for new conversations. Existing threads keep their project."
        )}},
    ]
    labels = {
        pid: (project.name if doc.selector(pid, email) == project.name
              else f"{project.name[:32]} · {pid}")
        for pid, project in selected
    }
    options = [{"text": {"type": "plain_text", "text": labels[pid]}, "value": pid}
               for pid, _ in selected]
    if options:
        element: dict[str, Any] = {
            "type": "static_select", "action_id": PREFIX + "select", "options": options,
            "placeholder": {"type": "plain_text", "text": "Choose a project"},
        }
        if initial := next((o for o in options if o["value"] == active), None):
            element["initial_option"] = initial
        blocks.append({"type": "actions", "elements": [element]})
    else:
        blocks.append({"type": "section", "text": {
            "type": "plain_text",
            "text": "No projects available. Ask Mica to help you get started.",
        }})
    if len(projects) > 100:
        blocks.append({"type": "context", "elements": [{"type": "plain_text", "text": (
            "Showing your 100 most recently used projects, including your current selection. "
            "Ask Mica for other projects."
        )}]})
    lines = []
    for pid, project in selected:
        role = ("Owner" if project.owner == email else
                "Co-owner" if doc.owns(pid, email) else "Member")
        lines.append(f"{labels[pid]} · {role}\n{project.data_uri[:200]}")
    for start in range(0, len(lines), 8):
        blocks.append({"type": "section", "text": {
            "type": "plain_text", "text": "\n\n".join(lines[start:start + 8]),
        }})
    return {"type": "home", "blocks": blocks}


async def used(bridge: BatchBridge, team: str, user: str, project: str) -> None:
    if not project:
        return
    state_key = key(f"{team}:{user}")
    try:
        async with asyncio.timeout(1), approvals.lease(bridge, state_key):
            state = await bridge.repository.get_connector_state(state_key) or {}
            recent = state.setdefault("recent", {})
            recent[project] = time.time()
            state["recent"] = dict(sorted(recent.items(), key=lambda x: x[1], reverse=True)[:200])
            await bridge.repository.set_connector_state(state_key, state)
    except Exception:
        log.warning("Slack project recency update pending")


async def refresh(
    bridge: BatchBridge, team: str, user: str, channel: str, *, tab: str = "messages",
) -> None:
    """Bounded, best-effort presentation: never enqueue model or compute work."""
    if not (re.fullmatch(r"T[A-Z0-9]+", team) and re.fullmatch(r"[UW][A-Z0-9]+", user)
            and re.fullmatch(r"D[A-Z0-9]+", channel)):
        return
    try:
        async with asyncio.timeout(2.5):
            storage = bridge.storage
            email = await asyncio.to_thread(
                identity.resolve_email, bridge.app, bridge.settings, storage, team, user,
            )
            if not email:
                return
            state_key = key(f"{team}:{user}")
            async with approvals.lease(bridge, state_key):
                state = await bridge.repository.get_connector_state(state_key) or {}
                state["channel"] = channel
                if tab == "home":
                    state["home_opened"] = True
                doc = await asyncio.to_thread(Registry(storage).load)
                active = doc.active_for(email, team, channel, private=True)
                project = doc.projects[active].name if active else ""
                prompts = [
                    {"title": p.title.replace("{project}", project)[:75],
                     "message": p.message.replace("{project}", project)[:3000]}
                    for p in bridge.settings.suggested_prompts
                    if project or "{project}" not in p.title + p.message
                ]
                title = "Project: " + project if project else "No project selected"
                signature = hashlib.sha256(json.dumps([title, prompts]).encode()).hexdigest()
                client = AsyncWebClient(
                    token=bridge.app.slack_bot_token, timeout=1, retry_handlers=[],
                )
                # Persist each independent success, so a static-prompt configuration error
                # cannot prevent the Home tab from being published (or duplicate its refresh).
                if state.get("home_opened"):
                    view = home(doc, email, active, state.get("recent", {}))
                    view_hash = hashlib.sha256(json.dumps(view).encode()).hexdigest()
                    if state.get("view_hash") != view_hash:
                        await client.views_publish(user_id=user, view=view)
                        state["view_hash"] = view_hash
                await bridge.repository.set_connector_state(state_key, state)
                if state.get("prompts_hash") != signature:
                    await SlackTransport(client).set_prompts(
                        channel_id=channel, title=title, prompts=prompts,
                    )
                    state["prompts_hash"] = signature
                    await bridge.repository.set_connector_state(state_key, state)
    except Exception:
        log.warning("Slack Home/suggestions refresh pending", exc_info=True)


async def handle(
    bridge: BatchBridge, storage: Storage, payload: dict[str, Any], background: BackgroundTasks,
) -> dict[str, Any]:
    team, user = payload.get("team", {}).get("id", ""), payload.get("user", {}).get("id", "")
    if (payload.get("view", {}).get("type") != "home"
            or not re.fullmatch(r"T[A-Z0-9]+", team)
            or not re.fullmatch(r"[UW][A-Z0-9]+", user)):
        raise ValueError("Choose a project from your own Mica Home tab.")
    action = payload["actions"][0]
    stamp = action.get("action_ts", "")
    if action.get("action_id") != PREFIX + "select" or not re.fullmatch(r"\d+\.\d+", stamp):
        raise ValueError("Invalid project selection")
    state = await bridge.repository.get_connector_state(key(f"{team}:{user}")) or {}
    channel = state.get("channel", "")
    if not re.fullmatch(r"D[A-Z0-9]+", channel):
        raise ValueError("Reopen Mica's Home tab before choosing a project.")
    try:
        email = await asyncio.to_thread(
            identity.resolve_email, bridge.app, bridge.settings, storage, team, user,
        )
        if not email:
            raise ValueError("Your account is no longer enabled for Mica.")
        pid = str(action.get("selected_option", {}).get("value", ""))
        registry = Registry(storage)
        doc = await asyncio.to_thread(registry.load)
        if not doc.can_use(pid, email):
            raise ValueError("That project is no longer available to you.")

        def choose(current: RegistryDoc) -> str:
            current.bind(team, channel, email, pid, private=True)
            return pid

        event_id = hashlib.sha256(f"picker:{team}:{user}:{stamp}".encode()).hexdigest()
        await asyncio.to_thread(registry.update, event_id, email, "use:" + pid, choose)
        await used(bridge, team, user, pid)
    finally:
        await refresh(bridge, team, user, channel, tab="home")
    return {}


async def expire(bridge: BatchBridge, storage: Storage, identifier: str) -> None:
    return  # A Home selection is an immediate operation, not an approval or timed grant.


KIND = approvals.Kind("project_picker", (PREFIX,), key, None, handle, expire)
