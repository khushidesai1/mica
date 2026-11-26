"""Personal GitHub OAuth; no installation tokens or private App key exist on this path."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from . import project_memory, reference_repos, surface
from .connections import Connections as PersonalConnections
from .connections import digest

API = "https://api.github.com"
TOKEN_URL = "https://github.com/login/oauth/access_token"


class Reconnect(ValueError):
    """Safe, public explanation without a provider response or credential."""


class Connections(PersonalConnections):
    namespace = "github"
    label = "GitHub"

    @asynccontextmanager
    async def locked(self, key: str) -> AsyncIterator[None]:
        # Wait behind an in-flight refresh so callers observe its saved token pair.
        lock_key = self.namespace + "-private-lock:" + digest(key)
        deadline = time.monotonic() + 45
        while True:
            claim = await self.repo.begin_event(lock_key, lease_seconds=60)
            if claim.status == "claimed":
                break
            if time.monotonic() >= deadline:
                raise ValueError("This connection is being updated; please retry.")
            await asyncio.sleep(0.1)
        try:
            yield
        finally:
            await self.repo.release_event(lock_key, claim.token)

    def provider(self) -> str:
        return "github.com/" + self.settings.github_pr_client_id

    def validate_runtime(self) -> None:
        self.key("credentials")
        url = urlsplit(self.app.control_plane_url)
        if (
            url.scheme != "https"
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("GitHub requires the configured HTTPS control-plane address")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.settings.github_pr_client_id):
            raise ValueError("Configure the GitHub App client ID")
        if not re.fullmatch(r"[a-z0-9-]{1,100}", self.settings.github_pr_app_slug):
            raise ValueError("Configure the GitHub App slug")
        if not re.fullmatch(
            r"projects/[^/]+/secrets/[^/]+/versions/[0-9]+",
            self.settings.github_pr_client_secret_secret,
        ):
            raise ValueError("Configure a numeric GitHub client secret version")

    def enabled(self, record: dict[str, Any]) -> None:
        if not self.settings.pull_requests:
            raise ValueError("GitHub connections and pull requests are not enabled yet.")
        event = record["payload"]["event"]
        if (
            surface.is_email(record)
            or record.get("ambient")
            or event.get("channel_type") != "im"
            or not re.fullmatch(r"D[A-Z0-9]+", event.get("channel", ""))
        ):
            raise ValueError("Connect GitHub and request pull requests in a one-to-one Slack DM.")
        pilots = self.settings.csv("pull_request_user_ids")
        if pilots and record["payload"]["team_id"] + ":" + event["user"] not in pilots:
            raise ValueError("GitHub pull requests are limited to the current pilot users.")

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.settings.github_pr_app_slug}/installations/new"

    @property
    def callback_url(self) -> str:
        return self.app.control_plane_url.rstrip("/") + "/integrations/github/callback"

    async def secret(self) -> str:
        return await asyncio.to_thread(
            project_memory.token,
            self.settings.model_copy(
                update={
                    "memory_token_secret": self.settings.github_pr_client_secret_secret,
                }
            ),
        )

    async def start(self, session_token: str) -> tuple[str, str]:
        session_key = "github-session:" + digest(session_token)
        async with self.locked(session_key):
            session = await self.session(session_token)
            if session["purpose"] != "connection":
                raise ValueError("Invalid connection session")
            state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
            identifier = digest(state)
            row = {
                "owner": session["owner"],
                "revision": identifier,
                "connection_revision": session["revision"],
                "event": session.get("event", ""),
                "session_hash": digest(session_token),
                "binding_hash": digest(binding),
                "expires": time.time() + 600,
                "used": False,
                **self.seal(session["owner"], identifier, {"verifier": verifier}),
            }
            await self.repo.set_connector_state("github-oauth:" + identifier, row)
            session["used"] = True
            await self.repo.set_connector_state(session_key, session)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            url = "https://github.com/login/oauth/authorize?" + urlencode(
                {
                    "client_id": self.settings.github_pr_client_id,
                    "redirect_uri": self.callback_url,
                    "state": state,
                    "code_challenge": challenge.rstrip(b"=").decode(),
                    "code_challenge_method": "S256",
                    "scope": "offline_access",
                    "prompt": "select_account",
                }
            )
            return url, binding

    async def exchange_tokens(self, values: dict[str, str]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": self.settings.github_pr_client_id,
                    "client_secret": await self.secret(),
                    **values,
                },
            )
        if response.is_error:
            raise Reconnect("GitHub authorization could not be completed. Reconnect your account.")
        data = response.json()
        if (
            data.get("error")
            or not isinstance(data.get("access_token"), str)
            or not isinstance(data.get("refresh_token"), str)
            or not 0 < int(data.get("expires_in", 0)) <= 28800
            or not 0 < int(data.get("refresh_token_expires_in", 0)) <= 15897600
        ):
            raise Reconnect("GitHub requires a new connection with expiring access enabled.")
        now = time.time()
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": now + int(data["expires_in"]),
            "refresh_expires_at": now + int(data["refresh_token_expires_in"]),
        }

    async def callback(
        self,
        state: str,
        binding: str,
        code: str,
        error: str = "",
        *,
        authorize: Callable[[str, str], Awaitable[None]],
    ) -> dict[str, str]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", state) or not binding:
            raise ValueError("This connection callback is unavailable. Start again from Slack.")
        state_key = "github-oauth:" + digest(state)
        async with self.locked(state_key):
            row = await self.repo.get_connector_state(state_key)
            if (
                not row
                or row["used"]
                or time.time() >= row["expires"]
                or not hmac.compare_digest(row["binding_hash"], digest(binding))
            ):
                raise ValueError("This connection callback expired or was already used.")
            # Consume before redeeming any authorization code, even on a denied consent screen.
            row["used"] = True
            await self.repo.set_connector_state(state_key, row)
            if error or not code or len(code) > 512:
                raise ValueError("GitHub authorization was not completed. Start again from Slack.")
            owner = row["owner"]
            async with self.locked(self.record_key(owner)), asyncio.timeout(50):
                old = await self.get(owner)
                if old.get("revision", "") != row["connection_revision"]:
                    raise ValueError("This connection changed. Start again from Slack.")
                await authorize(owner, row["event"])
                verifier = self.unseal(owner, row)["verifier"]
                tokens = await self.exchange_tokens(
                    {
                        "code": code,
                        "redirect_uri": self.callback_url,
                        "code_verifier": verifier,
                    }
                )
                async with project_memory.client_for(tokens["access_token"]) as client:
                    response = await client.get(API + "/user")
                    if response.is_error:
                        raise Reconnect("GitHub account verification failed. Reconnect.")
                    user = response.json()
                if not isinstance(user.get("id"), int) or not isinstance(user.get("login"), str):
                    raise ValueError("GitHub account verification failed")
                revision = secrets.token_hex(16)
                value = {
                    "owner": owner,
                    "provider": self.provider(),
                    "revision": revision,
                    "github_id": str(user["id"]),
                    "login": user["login"],
                    "status": "active",
                    "created_at": old.get("created_at", time.time()),
                    "updated_at": time.time(),
                    **self.seal(owner, revision, tokens),
                }
                await self.repo.set_connector_state(self.record_key(owner), value)
            if old.get("ciphertext"):
                with suppress(Exception):
                    await self.revoke(
                        self.unseal(owner, old)["access_token"],
                        grant=old.get("github_id") != value["github_id"],
                    )
            return {
                "owner": owner,
                "login": value["login"],
                "event": row["event"],
                "revision": revision,
            }

    async def mark_reconnect(self, owner: str, revision: str) -> None:
        async with self.locked(self.record_key(owner)):
            row = await self.get(owner)
            if row.get("revision") == revision and row.get("status") == "active":
                row.update(status="needs_reconnect", updated_at=time.time())
                await self.repo.set_connector_state(self.record_key(owner), row)

    async def current(self, owner: str, revision: str = "", github_id: str = "") -> dict[str, Any]:
        row = await self.get(owner)
        if (
            row.get("status") != "active"
            or (revision and row.get("revision") != revision)
            or (github_id and row.get("github_id") != github_id)
        ):
            raise Reconnect(
                "Connect GitHub again, then submit a new PR request for fresh approval."
            )
        try:
            self.unseal(owner, row)
        except ValueError:
            await self.mark_reconnect(owner, row["revision"])
            raise Reconnect("The connection key changed. Connect GitHub again.") from None
        return row

    async def credentials(self, owner: str, revision: str) -> str:
        async with self.locked(self.record_key(owner)), asyncio.timeout(40):
            row = await self.get(owner)
            if row.get("revision") != revision or row.get("status") != "active":
                raise Reconnect("The GitHub connection changed. Submit a new PR request.")
            try:
                tokens = self.unseal(owner, row)
                if (
                    tokens["expires_at"] - time.time()
                    <= self.settings.github_token_refresh_margin_seconds
                ):
                    if tokens["refresh_expires_at"] <= time.time():
                        raise Reconnect("GitHub authorization expired")
                    tokens = await self.exchange_tokens(
                        {
                            "grant_type": "refresh_token",
                            "refresh_token": tokens["refresh_token"],
                        }
                    )
                    row.update(self.seal(owner, revision, tokens), updated_at=time.time())
                    await self.repo.set_connector_state(self.record_key(owner), row)
                return str(tokens["access_token"])
            except Exception:
                # A timeout/lost refresh response can have consumed the old token pair too.
                row.update(status="needs_reconnect", updated_at=time.time())
                row.pop("ciphertext", None)
                await self.repo.set_connector_state(self.record_key(owner), row)
                raise Reconnect(
                    "GitHub credentials could not be renewed. Connect GitHub again."
                ) from None

    @asynccontextmanager
    async def client(self, owner: str, revision: str) -> AsyncIterator[httpx.AsyncClient]:
        credential = await self.credentials(owner, revision)
        try:
            async with project_memory.client_for(credential) as client:
                yield client
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                await self.mark_reconnect(owner, revision)
                raise Reconnect(
                    "GitHub revoked this connection. Reconnect and submit a new request."
                ) from None
            if exc.response.status_code == 403 and "X-GitHub-SSO" in exc.response.headers:
                raise ValueError(
                    "Start your organization's SAML session, then reconnect GitHub."
                ) from None
            raise ValueError(
                "GitHub refused this operation. Check repository access and retry."
            ) from None

    async def repositories(self, owner: str, revision: str, target: str = "") -> list[str]:
        found: list[str] = []
        async with self.client(owner, revision) as client, asyncio.timeout(45):
            # Refuse incomplete discovery; never infer permissions from truncation.
            installations = await reference_repos.get_json(
                client, API + "/user/installations", params={"per_page": 100}
            )
            if installations.get("total_count", 0) > 100:
                raise ValueError("Too many GitHub installations to inspect safely.")
            for installation in installations["installations"]:
                for page in range(1, 21):
                    repos = await reference_repos.get_json(
                        client,
                        API + f"/user/installations/{installation['id']}/repositories",
                        params={"per_page": 100, "page": page},
                    )
                    for repo in repos["repositories"]:
                        name = repo["full_name"].lower()
                        if target and name != target:
                            continue
                        if repo.get("permissions", {}).get("push") is True:
                            found.append(name)
                            if target:
                                return found
                        elif target:
                            raise ValueError(
                                "Your GitHub account does not have push access to this repository."
                            )
                    if page * 100 >= repos.get("total_count", 0):
                        break
                else:
                    raise ValueError("Too many GitHub repositories to inspect safely.")
        if target:
            raise ValueError(
                "Repository is not available to your GitHub connection. "
                "Install Mica on the repository and check your access: " + self.install_url
            )
        return sorted(set(found))

    async def revoke(self, credential: str, *, grant: bool) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.request(
                "DELETE",
                API
                + f"/applications/{self.settings.github_pr_client_id}/"
                + ("grant" if grant else "token"),
                auth=httpx.BasicAuth(self.settings.github_pr_client_id, await self.secret()),
                json={"access_token": credential},
            )
            if response.status_code != 404:
                response.raise_for_status()

    async def disconnect(self, owner: str) -> bool:
        revoked = True
        async with self.locked(self.record_key(owner)):
            row = await self.get(owner)
            try:
                if row.get("ciphertext"):
                    await self.revoke(self.unseal(owner, row)["access_token"], grant=True)
            except Exception:
                revoked = False
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
        return revoked

    async def status(self, owner: str) -> dict[str, Any]:
        row = await self.get(owner)
        if row.get("status") != "active":
            return {
                "status": "needs reconnect"
                if row.get("status") == "needs_reconnect"
                else "not connected",
                "install_url": self.install_url,
            }
        try:
            current = await self.current(owner)
            repos = await self.repositories(owner, current["revision"])
        except Reconnect:
            return {"status": "needs reconnect", "install_url": self.install_url}
        return {
            "status": "connected",
            "login": current["login"],
            "repositories": repos[:20],
            "repository_count": len(repos),
            "install_url": self.install_url,
        }
