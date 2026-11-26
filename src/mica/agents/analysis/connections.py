"""Provider-namespaced personal credentials, private links and browser sessions."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from slack_sdk.errors import SlackApiError

if TYPE_CHECKING:
    from .batch import BatchBridge

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mica.config import Settings as AppSettings
from mica.storage.base import Repository

from .config import Settings

LIFETIME = 900


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Connections:
    namespace: str
    label: str

    def __init__(self, app: AppSettings, settings: Settings, repo: Repository) -> None:
        self.app, self.settings, self.repo = app, settings, repo

    def key(self, purpose: str) -> bytes:
        secret = self.app.signed_link_secret.encode()
        if len(secret) < 32 or len(set(secret)) < 12:
            raise ValueError("Secure connection storage needs a configured application key.")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=("mica/" + self.namespace + "/" + purpose + "/v1").encode(),
        ).derive(secret)

    def key_version(self) -> str:
        return hashlib.sha256(self.key("links")).hexdigest()[:16]

    def mac(self, purpose: str, value: str) -> str:
        return hmac.new(self.key(purpose), value.encode(), hashlib.sha256).hexdigest()

    def record_key(self, owner: str) -> str:
        if not owner or owner != owner.strip().lower() or "@" not in owner:
            raise ValueError("A verified account is required.")
        return self.namespace + "-user:" + digest(owner + "\0" + self.provider())

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[None]:
        claim = await self.repo.begin_event(
            self.namespace + "-private-lock:" + digest(key), lease_seconds=60
        )
        if claim.status != "claimed":
            raise ValueError("This connection is being updated; please retry.")
        try:
            yield
        finally:
            await self.repo.release_event(
                self.namespace + "-private-lock:" + digest(key), claim.token
            )

    async def get(self, owner: str) -> dict[str, Any]:
        value = await self.repo.get_connector_state(self.record_key(owner)) or {}
        if value and (value.get("owner") != owner or value.get("provider") != self.provider()):
            raise ValueError("Connection ownership mismatch.")
        return value

    async def status(self, owner: str) -> dict[str, Any]:
        value = await self.get(owner)
        return {k: value.get(k) for k in ("revision", "status", "updated_at", "last_result")}

    def aad(self, owner: str, revision: str) -> bytes:
        return json.dumps([owner, self.provider(), revision, "v1"]).encode()

    async def issue(self, owner: str, purpose: str, event: str, pending: str = "") -> str:
        if purpose not in {"connection", "approval"}:
            raise ValueError("Invalid connection action.")
        self.key("links")
        current = await self.get(owner)
        revision = current.get("revision", "")
        identifier = self.mac("link-id", json.dumps([owner, purpose, event, pending, revision]))
        key = self.namespace + "-link:" + identifier
        async with self.locked(key):
            original = await self.repo.get_connector_state(key)
            link = original
            if original and original.get("replacement"):
                identifier = original["replacement"]
                link = await self.repo.get_connector_state(self.namespace + "-link:" + identifier)
            if link and not link.get("used") and time.time() >= link["expires"]:
                # A delayed delivery needs a fresh link, never a renewed old bearer token.
                identifier, link = secrets.token_hex(32), None
            if not link:
                token = self.mac("links", identifier)
                link = {
                    "owner": owner,
                    "event": event,
                    "purpose": purpose,
                    "pending": pending,
                    "revision": revision,
                    "expires": time.time() + LIFETIME,
                    "token_hash": digest(token),
                    "key_version": self.key_version(),
                    "used": False,
                }
                await self.repo.set_connector_state(self.namespace + "-link:" + identifier, link)
                if self.namespace + "-link:" + identifier != key:
                    await self.repo.set_connector_state(
                        key, {**(original or {}), "replacement": identifier}
                    )
        # Reconstruct an identical private delivery on retry without storing its bearer token.
        return (
            self.app.control_plane_url.rstrip("/")
            + "/integrations/"
            + self.namespace
            + "#"
            + identifier
            + "."
            + self.mac("links", identifier)
        )

    async def validate_link(self, raw: str) -> tuple[str, dict[str, Any]]:
        """Authenticate a private link without consuming it or acquiring a write lease."""
        if len(raw) != 129 or raw[64] != ".":
            raise ValueError("Open a fresh private connection link from Mica.")
        identifier, token = raw.split(".", 1)
        key = self.namespace + "-link:" + identifier
        link = await self.repo.get_connector_state(key)
        if (
            not link
            or link.get("key_version") != self.key_version()
            or link.get("used")
            or time.time() >= link["expires"]
            or not hmac.compare_digest(link["token_hash"], digest(token))
        ):
            raise ValueError(
                "This private link expired or was already opened. Ask Mica for a new link."
            )
        if (await self.get(link["owner"])).get("revision", "") != link["revision"]:
            raise ValueError("This connection changed. Ask Mica for a new link.")
        return key, link

    async def exchange(self, raw: str) -> str:
        key, _ = await self.validate_link(raw)
        async with self.locked(key):
            _, link = await self.validate_link(raw)
            session = secrets.token_urlsafe(32)
            await self.repo.set_connector_state(
                self.namespace + "-session:" + digest(session),
                {
                    **{
                        k: link[k]
                        for k in (
                            "owner",
                            "purpose",
                            "pending",
                            "revision",
                            "expires",
                            "key_version",
                        )
                    },
                    "event": link.get("event", ""),
                    "used": False,
                },
            )
            link["used"] = True
            await self.repo.set_connector_state(key, link)
            return session

    async def session(self, token: str) -> dict[str, Any]:
        if not token or len(token) > 128:
            raise ValueError("Open a fresh private link from Mica.")
        session = await self.repo.get_connector_state(self.namespace + "-session:" + digest(token))
        if (
            not session
            or session.get("key_version") != self.key_version()
            or session.get("used")
            or time.time() >= session["expires"]
        ):
            raise ValueError("This session expired; ask Mica for a new private link.")
        if (await self.get(session["owner"])).get("revision", "") != session["revision"]:
            raise ValueError("This connection changed; ask Mica for a new private link.")
        return session

    def provider(self) -> str:
        raise NotImplementedError

    def seal(self, owner: str, revision: str, value: dict[str, Any]) -> dict[str, str]:
        key = self.key("credentials")
        nonce = secrets.token_bytes(12)
        encrypted = AESGCM(key).encrypt(
            nonce, json.dumps(value).encode(), self.aad(owner, revision)
        )
        return {
            "ciphertext": base64.b64encode(nonce + encrypted).decode(),
            "key_version": hashlib.sha256(key).hexdigest()[:16],
        }

    def unseal(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        try:
            key = self.key("credentials")
            if record["key_version"] != hashlib.sha256(key).hexdigest()[:16]:
                raise ValueError("Key changed")
            raw = base64.b64decode(record["ciphertext"], validate=True)
            result = json.loads(
                AESGCM(key).decrypt(raw[:12], raw[12:], self.aad(owner, record["revision"]))
            )
            if not isinstance(result, dict):
                raise ValueError("Invalid connection data")
            return result
        except Exception:
            raise ValueError("Reconnect using a new private link.") from None


async def deliver_private(
    bridge: BatchBridge,
    owner: str,
    record: dict[str, Any],
    event_id: str,
    text: str,
    operation: str,
    *,
    namespace: str,
    label: str,
    client_factory: Any,
    replace_handler: Any = None,
    failure_cause: Any = "connection_delivery_failed",
    blocks: list[dict[str, Any]] | None = None,
    replace: bool = False,
) -> None:
    from . import access, command_intent, email_delivery, surface

    stage = namespace + "-" + hashlib.sha256(operation.encode()).hexdigest()[:24]
    receipt = f"{namespace}-delivery:{event_id}:{stage}"
    claim = await bridge.repository.begin_event(receipt, lease_seconds=60)
    if claim.status == "completed":
        return
    if claim.status != "claimed":
        raise RuntimeError("Private delivery is in progress; retry")
    try:
        grant = await asyncio.to_thread(
            access.resolve, bridge.app, bridge.settings, bridge.storage, record
        )
        if grant.email != owner:
            raise PermissionError("The connection recipient is no longer verified.")
        if surface.is_email(record):
            selected = copy.deepcopy(record)
            command_intent.private_reply(selected, grant)
            await email_delivery.deliver(bridge, event_id, selected, text, kind=stage)
        else:
            client = client_factory(token=bridge.app.slack_bot_token, timeout=10)
            event = record["payload"]["event"]
            channel = str(event.get("channel", ""))
            in_request_dm = (
                grant.private
                and event.get("channel_type") == "im"
                and re.fullmatch(r"D[A-Z0-9]+", channel)
            )
            if not in_request_dm:
                # Shared channels must never receive credentials or approval links.
                opened = await client.conversations_open(users=event["user"])
                channel = str(opened["channel"]["id"])
                if not re.fullmatch(r"D[A-Z0-9]+", channel):
                    raise PermissionError("A one-to-one private conversation is required.")
            thread = (event.get("thread_ts") or event["ts"]) if in_request_dm else None
            if replace:
                await replace_handler(
                    bridge,
                    client,
                    record,
                    event_id,
                    channel,
                    thread,
                    text,
                    blocks or [],
                    adopt=bool(in_request_dm),
                )
            else:
                await client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread,
                    text=text,
                    client_msg_id=hashlib.md5(receipt.encode()).hexdigest(),
                    unfurl_links=False,
                    unfurl_media=False,
                )
        await bridge.repository.complete_event(receipt, claim.token)
    except Exception as exc:
        if not surface.is_email(record) and not isinstance(exc, PermissionError):
            from . import observability

            await observability.report(bridge, failure_cause, event_id, record, unit=stage)
        await bridge.repository.release_event(receipt, claim.token)
        # Permanent refusals must finish the request instead of retrying for an hour.
        if isinstance(exc, PermissionError):
            raise ValueError(
                "I couldn't verify a private recipient. Please message Mica directly "
                f"to connect your {label} account."
            ) from None
        if isinstance(exc, SlackApiError) and exc.response.get("error") in {
            "missing_scope",
            "not_allowed_token_type",
            "channel_not_found",
            "not_in_channel",
            "user_not_found",
            "user_not_visible",
            "is_archived",
            "no_permission",
            "restricted_action",
            "cannot_dm_bot",
            "invalid_auth",
            "token_revoked",
            "account_inactive",
            "user_disabled",
        }:
            raise ValueError(
                f"Slack wouldn't let me deliver your private {label} link. Please message Mica "
                f"in a one-to-one DM and ask to connect {label}. If you're already in that DM, "
                "the operator needs to check Mica's Slack access."
            ) from None
        # No provider exceptions or link text in request error logs.
        raise RuntimeError(f"Private {label} delivery needs retry.") from None
