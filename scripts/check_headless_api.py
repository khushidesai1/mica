"""Smoke-test the installed API image without cloud credentials or external calls."""

from pathlib import Path

from fastapi.testclient import TestClient

import mica
from mica.api.app import create_app
from mica.config import Settings
from mica.storage.memory import InMemoryRepository


def check() -> None:
    root = Path(mica.__file__).parent
    for relative in ("api/web.py", "api/templates/index.html", "api/static/pista_logo.svg"):
        assert not (root / relative).exists(), relative
    settings = Settings(_env_file=None, env="test", auth_mode="dev")
    with TestClient(create_app(settings=settings, repository=InMemoryRepository())) as client:
        for path in (
            "/",
            "/auth/login",
            "/auth/callback",
            "/assets/pista_logo.svg",
            "/docs",
            "/redoc",
        ):
            assert client.get(path, follow_redirects=False).status_code == 404, path
        assert client.post("/auth/logout").status_code == 404
        assert client.get("/api/v1/healthz").json() == {"status": "ok"}
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/api/v1/tasks",
            "/api/v1/projects",
            "/integrations/slack/events",
            "/integrations/slack/actions",
            "/integrations/gmail/push",
        ):
            assert path in paths, path
        assert (
            client.post(f"/api/v1/internal/slack-analysis/process/{'a' * 32}/0").status_code == 503
        )
        assert (
            client.get(f"/api/v1/internal/slack-analysis/credentials/{'a' * 32}").status_code == 503
        )
    print("HEADLESS_API_VERIFIED")


if __name__ == "__main__":
    check()
