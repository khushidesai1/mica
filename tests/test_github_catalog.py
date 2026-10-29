"""Catalog refresh uses the approved GitHub App and preserves runtime approvals."""

import json
from unittest.mock import Mock

import httpx
import pytest

from mica.agents.analysis import project_memory, reference_repos
from mica.agents.analysis.config import Settings as GitHubSettings
from mica.config import Settings
from mica.connectors.github_catalog import GitHubCatalog
from mica.models import ToolRecord, ToolStatus
from mica.storage.memory import InMemoryRepository


def repo(number=1, **extra):
    return {
        "id": number,
        "name": f"tool-{number}",
        "full_name": f"azizilab/tool-{number}",
        "default_branch": "release/v1",
        "archived": False,
        "private": True,
        "license": {"spdx_id": "MIT"},
        **extra,
    }


def tool(number=1, **extra):
    return ToolRecord(
        id=f"github-{number}",
        name=f"tool-{number}",
        source_url="https://github.com/azizilab/tool",
        version="old",
        commit_sha="b" * 40,
        install_recipe="reviewed-recipe",
        **extra,
    )


@pytest.fixture
def setup(monkeypatch):
    repository = InMemoryRepository()
    settings = GitHubSettings(
        reference_github_app_id="123",
        reference_github_installation_id="456",
        reference_github_key_secret="projects/p/secrets/app-key/versions/1",
        memory_token_secret="projects/p/secrets/memory/versions/1",
    )
    secret = Mock(return_value="private-key")
    monkeypatch.setattr(project_memory, "token", secret)
    monkeypatch.setattr(reference_repos, "app_jwt", lambda key, app: "signed-jwt")
    calls, issued = [], []
    pages = [[repo()]]
    fail = {}

    def respond(request):
        calls.append(request)
        path = request.url.path
        if path in fail:
            return httpx.Response(fail[path], json={"message": "provider-secret"})
        if path == "/app/installations/456":
            assert request.headers["Authorization"] == "Bearer signed-jwt"
            return httpx.Response(200, json={"app_id": 123, "account": {"login": "AziziLab"}})
        if path == "/app/installations/456/access_tokens":
            assert request.method == "POST"
            assert json.loads(request.content) == {"permissions": {"contents": "read"}}
            issued.append(f"fresh-{len(issued)}")
            return httpx.Response(201, json={"token": issued[-1]})
        assert request.headers["Authorization"] == "Bearer " + issued[-1]
        if path == "/installation/repositories":
            page = int(request.url.params["page"])
            return httpx.Response(200, json={"repositories": pages[page - 1]})
        assert "/repos/azizilab/" in path and b"release%2Fv1" in request.url.raw_path
        return httpx.Response(200, json={"sha": "a" * 40})

    monkeypatch.setattr(
        project_memory,
        "client_for",
        lambda token: httpx.AsyncClient(
            headers={"Authorization": "Bearer " + token}, transport=httpx.MockTransport(respond)
        ),
    )
    catalog = GitHubCatalog(
        repository,
        Settings(_env_file=None, github_app_token="expired-static-token"),
        github_settings=settings,
    )
    return catalog, repository, settings, calls, issued, pages, fail, secret


async def test_each_run_renews_read_only_installation_token_without_legacy_credentials(setup):
    catalog, repository, settings, calls, issued, _, _, secret = setup
    assert await catalog.sync() == 1
    first = await repository.get_tool("github-1")
    assert first.commit_sha == "a" * 40 and first.status == ToolStatus.CATALOGED
    assert await catalog.sync() == 1
    assert issued == ["fresh-0", "fresh-1"]
    assert (await repository.get_tool("github-1")).created_at == first.created_at
    assert all(
        c.args[0].memory_token_secret == settings.reference_github_key_secret
        for c in secret.call_args_list
    )
    assert all("expired-static-token" not in str(c.headers) for c in calls)
    assert all("/orgs/" not in c.url.path for c in calls)


async def test_pagination_and_archived_repository_filter(setup):
    catalog, repository, _, _, _, pages, _, _ = setup
    pages[:] = [[repo(i, archived=i == 5) for i in range(1, 101)], [repo(101)]]
    assert await catalog.sync() == 100
    assert await repository.get_tool("github-5") is None
    assert await repository.get_tool("github-101") is not None


@pytest.mark.parametrize(
    "status",
    [
        ToolStatus.PROPOSED,
        ToolStatus.APPROVED,
        ToolStatus.BUILDING,
        ToolStatus.AVAILABLE,
        ToolStatus.FAILED,
    ],
)
async def test_refresh_does_not_reset_reviewed_or_built_tools(setup, status):
    catalog, repository, *_ = setup
    existing = tool(status=status, image_digest="image@sha256:" + "a" * 64)
    await repository.put_tool(existing)
    assert await catalog.sync() == 0
    assert await repository.get_tool(existing.id) == existing


@pytest.mark.parametrize(
    "path",
    [
        "/app/installations/456/access_tokens",
        "/installation/repositories",
        "/repos/azizilab/tool-2/commits/release/v1",
    ],
)
async def test_auth_or_repository_failure_leaves_existing_catalog_intact(setup, path):
    catalog, repository, _, _, _, pages, fail, _ = setup
    pages[:] = [[repo(1), repo(2)]]
    existing = tool()
    await repository.put_tool(existing)
    fail[path] = 401
    with pytest.raises(httpx.HTTPStatusError):
        await catalog.sync()
    assert await repository.list_tools() == [existing]


async def test_missing_app_configuration_does_not_fall_back_to_static_token(setup):
    catalog, repository, settings, calls, *_ = setup
    settings.reference_github_app_id = ""
    with pytest.raises(ValueError, match="not configured"):
        await catalog.sync()
    assert not calls and not await repository.list_tools()


async def test_catalog_and_installation_organization_must_match(setup):
    catalog, repository, _, calls, *_ = setup
    catalog.settings.github_org = "another-org"
    with pytest.raises(ValueError, match="organization"):
        await catalog.sync()
    assert not calls and not await repository.list_tools()


async def test_unexpected_repository_owner_cannot_enter_catalog(setup):
    catalog, repository, _, _, _, pages, *_ = setup
    pages[:] = [[repo(full_name="other-org/tool")]]
    with pytest.raises(ValueError, match="outside"):
        await catalog.sync()
    assert not await repository.list_tools()


async def test_empty_reference_selection_cannot_request_all_repositories(setup):
    _, _, settings, calls, *_ = setup
    with pytest.raises(ValueError, match="selection"):
        await reference_repos.installation_token(settings, repositories=[])
    assert not calls
