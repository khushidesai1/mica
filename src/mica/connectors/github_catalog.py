"""Read-only metadata catalog of the GitHub App's approved repository selection."""

from __future__ import annotations

import re
from urllib.parse import quote

from mica.agents.analysis import project_memory, reference_repos
from mica.agents.analysis.config import Settings as GitHubSettings
from mica.config import Settings
from mica.models import ToolRecord, ToolStatus
from mica.storage.base import Repository


class GitHubCatalog:
    def __init__(
        self,
        repository: Repository,
        settings: Settings,
        *,
        github_settings: GitHubSettings | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.github_settings = github_settings if github_settings is not None else GitHubSettings()

    async def sync(self) -> int:
        if self.settings.github_org.lower() != self.github_settings.reference_github_org.lower():
            raise ValueError("Catalog organization does not match the GitHub App installation")
        # Mint on every invocation; never reuse the legacy static GitHub token or memory token.
        token = await reference_repos.installation_token(self.github_settings, repositories=None)
        tools: list[ToolRecord] = []
        async with project_memory.client_for(token) as client:
            page = 1
            while True:
                response = await client.get(
                    "https://api.github.com/installation/repositories",
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
                repos = response.json()["repositories"]
                if not repos:
                    break
                for repo in repos:
                    if repo.get("archived"):
                        continue
                    full_name = str(repo["full_name"])
                    if (
                        not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]{1,100}", full_name)
                        or full_name.split("/")[0].lower() != self.settings.github_org.lower()
                    ):
                        raise ValueError(
                            "Catalog repository is outside the configured organization"
                        )
                    branch = str(repo.get("default_branch", "main"))
                    commit_response = await client.get(
                        f"https://api.github.com/repos/{full_name}/commits/{quote(branch, safe='')}"
                    )
                    commit_response.raise_for_status()
                    commit = str(commit_response.json()["sha"])
                    if not re.fullmatch(r"[a-f0-9]{40}", commit):
                        raise ValueError("Invalid catalog commit identity")
                    license_name = str((repo.get("license") or {}).get("spdx_id", "unknown"))
                    tool = ToolRecord(
                        id=f"github-{int(repo['id'])}",
                        name=str(repo["name"]),
                        source_url=f"https://github.com/{full_name}",
                        version=commit[:12],
                        commit_sha=commit,
                        license=license_name,
                        install_recipe=(
                            f"git clone --filter=blob:none https://github.com/{full_name}.git && "
                            f"git checkout --detach {commit}"
                        ),
                        status=ToolStatus.CATALOGED,
                        provenance="GitHub App metadata/contents-read catalog",
                    )
                    tools.append(tool)
                if len(repos) < 100:
                    break
                page += 1
        # Finish GitHub reads before publishing, so an auth/read failure leaves the catalog intact.
        count = 0
        for tool in tools:
            existing = await self.repository.get_tool(tool.id)
            # A metadata refresh must never reset an approval, build, or available runtime.
            if existing and existing.status != ToolStatus.CATALOGED:
                continue
            if existing:
                tool.created_at = existing.created_at
            await self.repository.put_tool(tool)
            count += 1
        return count
