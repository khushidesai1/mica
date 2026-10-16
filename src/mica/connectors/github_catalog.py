"""Read-only GitHub App catalog for public and selected private repositories."""

from __future__ import annotations

import httpx

from mica.config import Settings
from mica.models import ToolRecord, ToolStatus
from mica.storage.base import Repository


class GitHubCatalog:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def sync(self) -> int:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.settings.github_app_token:
            headers["Authorization"] = f"Bearer {self.settings.github_app_token}"
        count = 0
        async with httpx.AsyncClient(
            base_url="https://api.github.com", headers=headers, timeout=30
        ) as client:
            page = 1
            while True:
                response = await client.get(
                    f"/orgs/{self.settings.github_org}/repos",
                    params={"type": "all", "per_page": 100, "page": page},
                )
                response.raise_for_status()
                repos = response.json()
                if not repos:
                    break
                for repo in repos:
                    if repo.get("archived"):
                        continue
                    full_name = str(repo["full_name"])
                    branch = str(repo.get("default_branch", "main"))
                    commit_response = await client.get(f"/repos/{full_name}/commits/{branch}")
                    commit_response.raise_for_status()
                    commit = str(commit_response.json()["sha"])
                    license_name = str((repo.get("license") or {}).get("spdx_id", "unknown"))
                    tool = ToolRecord(
                        id=f"github-{int(repo['id'])}",
                        name=str(repo["name"]),
                        source_url=str(repo["html_url"]),
                        version=commit[:12],
                        commit_sha=commit,
                        license=license_name,
                        install_recipe=(
                            f"git clone --filter=blob:none {repo['clone_url']} && "
                            f"git checkout --detach {commit}"
                        ),
                        status=ToolStatus.CATALOGED,
                        provenance="GitHub App metadata/contents-read catalog",
                    )
                    await self.repository.put_tool(tool)
                    count += 1
                if len(repos) < 100:
                    break
                page += 1
        return count
