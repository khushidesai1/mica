"""Private per-principal AtoMx connections and short-lived browser capabilities."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from mica.config import Settings as AppSettings
from mica.storage.base import Repository

from .config import Settings

LIFETIME = 900


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Connections:
    def __init__(self, app: AppSettings, settings: Settings, repo: Repository) -> None:
        self.app, self.settings, self.repo = app, settings, repo

    def key(self, purpose: str) -> bytes:
        secret = self.app.signed_link_secret.encode()
        if len(secret) < 32 or len(set(secret)) < 12:
            raise ValueError("AtoMx secure connection storage needs a configured application key.")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=("mica/atomx/" + purpose + "/v1").encode(),
        ).derive(secret)

    def key_version(self) -> str:
        return hashlib.sha256(self.key("links")).hexdigest()[:16]

    def validate_runtime(self) -> None:
        self.key("credentials")
        url = urlsplit(self.app.control_plane_url)
        if url.scheme != "https" or not url.netloc or url.username or url.query or url.fragment:
            raise ValueError("AtoMx requires the configured HTTPS control-plane address.")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]+", self.settings.transfer_host):
            raise ValueError("Configure the trusted AtoMx provider hostname.")
        try:
            serialization.load_ssh_public_key(self.settings.transfer_host_key.encode())
        except (ValueError, TypeError):
            raise ValueError("Configure a trusted AtoMx public host key.") from None

    def mac(self, purpose: str, value: str) -> str:
        return hmac.new(self.key(purpose), value.encode(), hashlib.sha256).hexdigest()

    def provider(self) -> str:
        return f"{self.settings.transfer_host}:{self.settings.transfer_port}"

    def record_key(self, owner: str) -> str:
        if not owner or owner != owner.strip().lower() or "@" not in owner:
            raise ValueError("A verified account is required.")
        return "atomx-user:" + digest(owner + "\0" + self.provider())

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[None]:
        claim = await self.repo.begin_event("atomx-private-lock:" + digest(key), lease_seconds=60)
        if claim.status != "claimed":
            raise ValueError("This connection is being updated; please retry.")
        try:
            yield
        finally:
            await self.repo.release_event("atomx-private-lock:" + digest(key), claim.token)

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

    async def save(self, owner: str, revision: str, username: str, password: str) -> dict[str, Any]:
        if not username.strip() or not password or len(username) > 320 or len(password) > 4096:
            raise ValueError("Enter a username and password in the secure form.")
        if any(c in username + password for c in ("\x00", "\r", "\n")):
            raise ValueError("Login fields cannot contain line breaks.")
        async with self.locked(self.record_key(owner)):
            old = await self.get(owner)
            if old.get("revision", "") != revision:
                raise ValueError("This connection changed; open a fresh connection link.")
            version, nonce = secrets.token_hex(16), secrets.token_bytes(12)
            key = self.key("credentials")
            encrypted = AESGCM(key).encrypt(
                nonce,
                json.dumps({"username": username.strip(), "password": password}).encode(),
                self.aad(owner, version),
            )
            value = {
                "owner": owner,
                "provider": self.provider(),
                "revision": version,
                "key_version": hashlib.sha256(key).hexdigest()[:16],
                "ciphertext": base64.b64encode(nonce + encrypted).decode(),
                "status": "saved",
                "updated_at": time.time(),
                "created_at": old.get("created_at", time.time()),
            }
            await self.repo.set_connector_state(self.record_key(owner), value)
            return value

    async def forget(self, owner: str, revision: str) -> None:
        async with self.locked(self.record_key(owner)):
            old = await self.get(owner)
            if old.get("revision", "") != revision:
                raise ValueError("This connection changed; open a fresh connection link.")
            await self.repo.set_connector_state(
                self.record_key(owner),
                {
                    "owner": owner,
                    "provider": self.provider(),
                    "revision": secrets.token_hex(16),
                    "status": "forgotten",
                    "updated_at": time.time(),
                },
            )

    async def decrypt(self, owner: str, revision: str) -> dict[str, str]:
        value = await self.get(owner)
        if value.get("revision") != revision or value.get("status") not in {"saved", "valid"}:
            raise ValueError("This personal connection is no longer available.")
        try:
            raw = base64.b64decode(value["ciphertext"], validate=True)
            key = self.key("credentials")
            if value["key_version"] != hashlib.sha256(key).hexdigest()[:16]:
                raise ValueError("Key changed")
            login = json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], self.aad(owner, revision)))
            return {k: str(login[k]) for k in ("username", "password")}
        except Exception:
            raise ValueError("Reconnect your AtoMx account using a new private link.") from None

    async def result(self, owner: str, revision: str, status: str) -> None:
        async with self.locked(self.record_key(owner)):
            value = await self.get(owner)
            if value.get("revision") == revision:
                value["last_result"] = status
                if status == "auth_failed":
                    value["status"] = "invalid"
                elif status == "done":
                    value["status"] = "valid"
                await self.repo.set_connector_state(self.record_key(owner), value)

    async def issue(self, owner: str, purpose: str, event: str, pending: str = "") -> str:
        if purpose not in {"connection", "approval"}:
            raise ValueError("Invalid connection action.")
        self.key("links")
        current = await self.get(owner)
        revision = current.get("revision", "")
        identifier = self.mac("link-id", json.dumps([owner, purpose, event, pending, revision]))
        key = "atomx-link:" + identifier
        async with self.locked(key):
            original = await self.repo.get_connector_state(key)
            link = original
            if original and original.get("replacement"):
                identifier = original["replacement"]
                link = await self.repo.get_connector_state("atomx-link:" + identifier)
            if link and not link.get("used") and time.time() >= link["expires"]:
                # A delayed delivery needs a fresh link, never a renewed old bearer token.
                identifier, link = secrets.token_hex(32), None
            if not link:
                token = self.mac("links", identifier)
                link = {
                    "owner": owner,
                    "purpose": purpose,
                    "pending": pending,
                    "revision": revision,
                    "expires": time.time() + LIFETIME,
                    "token_hash": digest(token),
                    "key_version": self.key_version(),
                    "used": False,
                }
                await self.repo.set_connector_state("atomx-link:" + identifier, link)
                if "atomx-link:" + identifier != key:
                    await self.repo.set_connector_state(
                        key, {**(original or {}), "replacement": identifier}
                    )
        # Reconstruct an identical private delivery on retry without storing its bearer token.
        return (
            self.app.control_plane_url.rstrip("/")
            + "/integrations/atomx#"
            + identifier
            + "."
            + self.mac("links", identifier)
        )

    async def exchange(self, raw: str) -> str:
        if len(raw) != 129 or raw[64] != ".":
            raise ValueError("Open a fresh private AtoMx link from Mica.")
        identifier, token = raw.split(".", 1)
        key = "atomx-link:" + identifier
        async with self.locked(key):
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
            session = secrets.token_urlsafe(32)
            await self.repo.set_connector_state(
                "atomx-session:" + digest(session),
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
                    "used": False,
                },
            )
            link["used"] = True
            await self.repo.set_connector_state(key, link)
            return session

    async def session(self, token: str) -> dict[str, Any]:
        if not token or len(token) > 128:
            raise ValueError("Open a fresh private link from Mica.")
        session = await self.repo.get_connector_state("atomx-session:" + digest(token))
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
