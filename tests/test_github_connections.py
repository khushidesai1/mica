"""Requester-bound consent, encrypted credentials and rotated GitHub user tokens."""

import asyncio
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from mica.agents.analysis import project_memory
from mica.agents.analysis.config import Settings
from mica.agents.analysis.connections import digest
from mica.agents.analysis.github_connections import Connections, Reconnect
from mica.api import github
from mica.config import Settings as AppSettings
from mica.storage.memory import InMemoryRepository

OWNER = "alice@example.com"
KEY = "synthetic-application-key-only-for-tests-0123456789"


@pytest.fixture
def connection():
    app = AppSettings(_env_file=None, signed_link_secret=KEY, control_plane_url="https://mica.test")
    settings = Settings(
        _env_file=None,
        pull_requests=True,
        github_pr_client_id="client123",
        github_pr_app_slug="mica-contributor",
        github_pr_client_secret_secret="projects/test/secrets/github/versions/1",
    )
    return Connections(app, settings, InMemoryRepository())


def tokens(*, expires=28800, suffix=""):
    return dict(
        access_token="access-private" + suffix,
        refresh_token="refresh-private" + suffix,
        expires_at=time.time() + expires,
        refresh_expires_at=time.time() + 100000,
    )


async def saved(connection, *, expires=28800, revision="rev1"):
    row = dict(
        owner=OWNER,
        provider=connection.provider(),
        revision=revision,
        status="active",
        github_id="123",
        login="alice",
        **connection.seal(OWNER, revision, tokens(expires=expires)),
    )
    await connection.repo.set_connector_state(connection.record_key(OWNER), row)
    return row


async def start(connection):
    link = await connection.issue(OWNER, "connection", "origin-event")
    session = await connection.exchange(link.split("#")[1])
    url, binding = await connection.start(session)
    return parse_qs(urlsplit(url).query), binding


async def test_pkce_cookie_binding_replay_and_encrypted_storage(connection, monkeypatch):
    connection.validate_runtime()
    query, binding = await start(connection)
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["offline_access"]
    state = query["state"][0]
    row = await connection.repo.get_connector_state("github-oauth:" + digest(state))
    verifier = connection.unseal(OWNER, row)["verifier"]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    ]
    assert all(
        value not in json.dumps(connection.repo.connector_state)
        for value in (state, binding, verifier)
    )
    exchange = AsyncMock(return_value=tokens())
    monkeypatch.setattr(connection, "exchange_tokens", exchange)
    monkeypatch.setattr(
        project_memory,
        "client_for",
        lambda token: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"id": 123, "login": "alice"})
            )
        ),
    )
    authorize = AsyncMock()
    with pytest.raises(ValueError):
        await connection.callback(state, "wrong-browser", "code", authorize=authorize)
    exchange.assert_not_called()
    result = await connection.callback(state, binding, "code", authorize=authorize)
    assert result["owner"] == OWNER and result["login"] == "alice"
    authorize.assert_awaited_once_with(OWNER, "origin-event")
    exchange.assert_awaited_once_with(
        dict(code="code", redirect_uri=connection.callback_url, code_verifier=verifier)
    )
    with pytest.raises(ValueError, match="already used"):
        await connection.callback(state, binding, "code", authorize=authorize)
    stored = json.dumps(connection.repo.connector_state)
    assert "access-private" not in stored and "refresh-private" not in stored
    assert await connection.credentials(OWNER, result["revision"]) == "access-private"
    with pytest.raises(Reconnect):
        await connection.current("bob@example.com", result["revision"])


@pytest.mark.parametrize("reason", ["expired", "denied", "identity", "revision"])
async def test_callback_failure_is_consumed_without_redeeming(connection, monkeypatch, reason):
    query, binding = await start(connection)
    state = query["state"][0]
    authorize = AsyncMock()
    if reason == "expired":
        key = "github-oauth:" + digest(state)
        row = await connection.repo.get_connector_state(key)
        row["expires"] = 0
        await connection.repo.set_connector_state(key, row)
    elif reason == "identity":
        authorize.side_effect = ValueError("Requester changed")
    elif reason == "revision":
        await saved(connection)
    exchange = AsyncMock()
    monkeypatch.setattr(connection, "exchange_tokens", exchange)
    with pytest.raises(ValueError):
        await connection.callback(
            state, binding, "code", "denied" if reason == "denied" else "", authorize=authorize
        )
    exchange.assert_not_called()


async def test_concurrent_refresh_rotates_once_preserves_revision(connection, monkeypatch):
    await saved(connection, expires=0)

    async def refresh(value):
        assert value == dict(grant_type="refresh_token", refresh_token="refresh-private")
        await asyncio.sleep(0.01)
        return tokens(suffix="-new")

    exchange = AsyncMock(side_effect=refresh)
    monkeypatch.setattr(connection, "exchange_tokens", exchange)
    result = await asyncio.gather(*(connection.credentials(OWNER, "rev1") for _ in range(3)))
    assert result == ["access-private-new"] * 3
    exchange.assert_awaited_once()
    row = await connection.get(OWNER)
    assert row["revision"] == "rev1"
    assert connection.unseal(OWNER, row)["refresh_token"] == "refresh-private-new"


@pytest.mark.parametrize("reason", ["provider", "save", "key"])
async def test_failed_refresh_never_exposes_unsaved_tokens(connection, monkeypatch, reason):
    await saved(connection, expires=0)
    exchange = AsyncMock(return_value=tokens(suffix="-new"))
    monkeypatch.setattr(connection, "exchange_tokens", exchange)
    if reason == "provider":
        exchange.side_effect = RuntimeError("sensitive-provider-response")
    if reason == "key":
        connection.app.signed_link_secret = KEY + "rotated"
    if reason == "save":
        original = connection.repo.set_connector_state

        async def save(key, value):
            if value.get("status") == "active":
                raise RuntimeError("storage unavailable")
            await original(key, value)

        monkeypatch.setattr(connection.repo, "set_connector_state", save)
    with pytest.raises(Reconnect) as exc:
        await connection.credentials(OWNER, "rev1")
    assert "sensitive" not in str(exc.value) and "private" not in str(exc.value)
    assert (await connection.get(OWNER))["status"] == "needs_reconnect"
    with pytest.raises(Reconnect):
        await connection.credentials(OWNER, "rev1")


async def test_disconnect_tombstones_even_if_github_unavailable(connection, monkeypatch):
    await saved(connection)
    old_link = await connection.issue(OWNER, "connection", "old")
    revoke = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    monkeypatch.setattr(connection, "revoke", revoke)
    assert not await connection.disconnect(OWNER)
    revoke.assert_awaited_once_with("access-private", grant=True)
    row = await connection.get(OWNER)
    assert row["status"] == "forgotten" and "ciphertext" not in row
    assert row["revision"] != "rev1"
    with pytest.raises(ValueError, match="changed"):
        await connection.exchange(old_link.split("#")[1])


@pytest.mark.parametrize("permission", [True, False])
async def test_user_installation_intersection_requires_push(connection, monkeypatch, permission):
    await saved(connection)
    calls = []

    def serve(request):
        calls.append(request.url.path)
        if request.url.path == "/user/installations":
            return httpx.Response(200, json={"total_count": 1, "installations": [{"id": 4}]})
        assert request.url.path == "/user/installations/4/repositories"
        return httpx.Response(
            200,
            json={
                "total_count": 1,
                "repositories": [
                    {"full_name": "Some-Org/Repo", "permissions": {"push": permission}}
                ],
            },
        )

    monkeypatch.setattr(
        project_memory,
        "client_for",
        lambda token: httpx.AsyncClient(transport=httpx.MockTransport(serve)),
    )
    if permission:
        assert await connection.repositories(OWNER, "rev1", "some-org/repo") == ["some-org/repo"]
    else:
        with pytest.raises(ValueError, match="push access"):
            await connection.repositories(OWNER, "rev1", "some-org/repo")
    assert calls == ["/user/installations", "/user/installations/4/repositories"]


async def test_401_invalidates_connection_and_status_contains_no_credentials(
    connection, monkeypatch
):
    await saved(connection)
    monkeypatch.setattr(
        project_memory,
        "client_for",
        lambda token: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(401))
        ),
    )
    result = await connection.status(OWNER)
    assert result["status"] == "needs reconnect"
    assert "private" not in json.dumps(result)
    assert (await connection.get(OWNER))["status"] == "needs_reconnect"


async def test_browser_manual_consent_csrf_secure_paths_and_lax_callback(connection, monkeypatch):
    app = FastAPI()
    app.include_router(github.router)
    monkeypatch.setattr(github, "service", lambda request: (SimpleNamespace(), connection))
    monkeypatch.setattr(github, "check_origin_event", AsyncMock(return_value={}))
    monkeypatch.setattr(github, "require", AsyncMock())
    link = await connection.issue(OWNER, "connection", "browser")
    before = json.dumps(connection.repo.connector_state, sort_keys=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://mica.test"
    ) as client:
        page = await client.get("/integrations/github")
        assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert "Continue" in page.text and "password" not in page.text
        assert json.dumps(connection.repo.connector_state, sort_keys=True) == before
        payload = {"token": link.split("#")[1]}
        assert (await client.post("/integrations/github/exchange", json=payload)).status_code == 400
        headers = {"Origin": "https://mica.test"}
        opened = await client.post("/integrations/github/exchange", json=payload, headers=headers)
        cookie = opened.headers["set-cookie"].lower()
        assert all(
            v in cookie
            for v in ("secure", "httponly", "samesite=strict", "path=/integrations/github")
        )
        session = (await client.get("/integrations/github/session")).json()
        assert session["owner"] == OWNER
        assert (
            await client.post("/integrations/github/start", json={}, headers=headers)
        ).status_code == 400
        headers["X-CSRF-Token"] = session["csrf"]
        started = await client.post("/integrations/github/start", json={}, headers=headers)
        assert started.status_code == 200
        assert "samesite=lax" in started.headers["set-cookie"].lower()
        assert "code_challenge_method=S256" in started.json()["url"]
        assert (
            await client.post("/integrations/github/start", json={}, headers=headers)
        ).status_code == 400
        refused = await client.get("/integrations/github/callback?state=wrong&code=secret-code")
        assert refused.status_code == 400 and "secret-code" not in refused.text
