"""AtoMx provider on the shared personal connection mechanism."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .connections import Connections as PersonalConnections

LIFETIME = 900


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Connections(PersonalConnections):
    namespace = "atomx"
    label = "AtoMx"

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

    def provider(self) -> str:
        return f"{self.settings.transfer_host}:{self.settings.transfer_port}"

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
