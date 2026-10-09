"""Immutable tool registry and conservative bootstrap catalog."""

from __future__ import annotations

import asyncio
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from pistachio.config import Settings
from pistachio.models import ToolRecord, ToolStatus
from pistachio.storage.base import Repository
from pistachio.tool_catalog import (
    ATOMX_TRANSFER_COMMIT,
    ATOMX_TRANSFER_ID,
    ATOMX_TRANSFER_SOURCE,
    ATOMX_TRANSFER_UNAVAILABLE,
    SPATIAL_ANALYSIS_COMMIT,
    SPATIAL_ANALYSIS_ID,
    SPATIAL_ANALYSIS_NOTE,
    SPATIAL_ANALYSIS_SOURCE,
)

BOOTSTRAP_TOOLS = (
    ToolRecord(
        id="tool-runtime-python",
        name="Python",
        source_url="https://github.com/python/cpython",
        version="3.12.12",
        license="PSF-2.0",
        install_recipe="python:3.12.12-slim base image",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned immutable Batch worker runtime",
    ),
    ToolRecord(
        id="tool-runtime-numpy",
        name="NumPy",
        source_url="https://github.com/numpy/numpy",
        version="2.2.6",
        license="BSD-3-Clause",
        install_recipe="pip install numpy==2.2.6",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-pandas",
        name="pandas",
        source_url="https://github.com/pandas-dev/pandas",
        version="2.2.3",
        license="BSD-3-Clause",
        install_recipe="pip install pandas==2.2.3",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-scipy",
        name="SciPy",
        source_url="https://github.com/scipy/scipy",
        version="1.15.3",
        license="BSD-3-Clause",
        install_recipe="pip install scipy==1.15.3",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-scikit-learn",
        name="scikit-learn",
        source_url="https://github.com/scikit-learn/scikit-learn",
        version="1.5.2",
        license="BSD-3-Clause",
        install_recipe="pip install scikit-learn==1.5.2",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-matplotlib",
        name="Matplotlib",
        source_url="https://github.com/matplotlib/matplotlib",
        version="3.10.3",
        license="PSF-2.0",
        install_recipe="pip install matplotlib==3.10.3",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-seaborn",
        name="Seaborn",
        source_url="https://github.com/mwaskom/seaborn",
        version="0.13.2",
        license="BSD-3-Clause",
        install_recipe="pip install seaborn==0.13.2",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-anndata",
        name="AnnData",
        source_url="https://github.com/scverse/anndata",
        version="0.11.4",
        license="BSD-3-Clause",
        install_recipe="pip install anndata==0.11.4",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-scanpy",
        name="Scanpy",
        source_url="https://github.com/scverse/scanpy",
        version="1.11.1",
        license="BSD-3-Clause",
        install_recipe="pip install scanpy==1.11.1",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-scvi-tools",
        name="scvi-tools",
        source_url="https://github.com/scverse/scvi-tools",
        version="1.3.3",
        license="BSD-3-Clause",
        install_recipe="pip install scvi-tools==1.3.3",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-runtime-squidpy",
        name="Squidpy",
        source_url="https://github.com/scverse/squidpy",
        version="1.6.5",
        license="BSD-3-Clause",
        install_recipe="pip install squidpy==1.6.5",
        status=ToolStatus.AVAILABLE,
        provenance="Pinned in docker/constraints-science.txt",
    ),
    ToolRecord(
        id="tool-azizilab-starfysh",
        name="Starfysh",
        source_url="https://github.com/azizilab/starfysh",
        version="1.2.0",
        commit_sha="56fb01ef734401d067eb3078280dd805b97621d1",
        license="BSD-3-Clause",
        install_recipe=(
            "pip install git+https://github.com/azizilab/starfysh.git@"
            "56fb01ef734401d067eb3078280dd805b97621d1"
        ),
        status=ToolStatus.CATALOGED,
        provenance="Azizilab bootstrap catalog",
    ),
    ToolRecord(
        id="tool-azizilab-decipher",
        name="Decipher",
        source_url="https://github.com/azizilab/decipher",
        version="0.1.3",
        commit_sha="9610b317844c8515e077a81abafea07c7494a54c",
        license="MIT",
        install_recipe="pip install scdecipher==0.1.3",
        status=ToolStatus.CATALOGED,
        provenance="Azizilab bootstrap catalog",
    ),
    ToolRecord(
        id="tool-azizilab-echidna",
        name="Echidna",
        source_url="https://github.com/azizilab/echidna",
        version="1.0.3",
        commit_sha="adf9d71f366bb3680ad21631b2d407baf8ba10a9",
        license="MIT",
        install_recipe="pip install sc-echidna==1.0.3",
        status=ToolStatus.CATALOGED,
        provenance="Azizilab bootstrap catalog",
    ),
    ToolRecord(
        id="tool-azizilab-amici",
        name="Amici",
        source_url="https://github.com/azizilab/amici",
        version="0.1.0",
        commit_sha="2de0481c3a9907448b8e9e8d258e15df69ecbca4",
        license="CC-BY-NC-ND-4.0",
        install_recipe="pip install amici-st==0.1.0",
        status=ToolStatus.CATALOGED,
        provenance="Azizilab bootstrap catalog",
    ),
    ToolRecord(
        id=ATOMX_TRANSFER_ID,
        name="atomx-transfer",
        source_url=ATOMX_TRANSFER_SOURCE,
        version=ATOMX_TRANSFER_COMMIT[:12],
        commit_sha=ATOMX_TRANSFER_COMMIT,
        license="unknown",
        install_recipe="cloudbuild/build-transfer.yaml; pinned upstream plus reviewed CLI overlay",
        execution_backend="transfer_job",
        availability_note=ATOMX_TRANSFER_UNAVAILABLE,
        status=ToolStatus.CATALOGED,
        provenance="Public source reviewed 2026-10-06; README and docs/PLAN.md (M0 only)",
    ),
    ToolRecord(
        id=SPATIAL_ANALYSIS_ID,
        name="spatial_analysis_pipeline",
        source_url=SPATIAL_ANALYSIS_SOURCE,
        version="0.1.0",
        commit_sha=SPATIAL_ANALYSIS_COMMIT,
        license="unknown",
        install_recipe="cloudbuild/build-spatial-analysis.yaml; isolated /opt/sap/bin/python",
        build_profile="spatial_analysis",
        availability_note=SPATIAL_ANALYSIS_NOTE,
        status=ToolStatus.CATALOGED,
        provenance=(
            "Public source reviewed 2026-10-06: pyproject.toml, src/sap, and envs; "
            "no license file or declared runtime dependencies at this pin"
        ),
    ),
)


async def ensure_bootstrap_tools(repository: Repository) -> None:
    existing = {tool.id for tool in await repository.list_tools()}
    for tool in BOOTSTRAP_TOOLS:
        if tool.id not in existing:
            await repository.put_tool(tool.model_copy(deep=True))


class ToolBuildService:
    """Start the preconfigured immutable Cloud Build pipeline after exact approval."""

    def __init__(self, repository: Repository, settings: Settings) -> None:
        self.repository = repository
        self.settings = settings

    async def start(self, tool: ToolRecord) -> bool:
        if tool.execution_backend != "analysis":
            raise ValueError(
                f"{tool.name} requires a dedicated transfer job, not the Python package builder. "
                + tool.availability_note
            )
        if tool.build_profile == "spatial_analysis":
            raise ValueError(
                f"{tool.name} requires cloudbuild/build-spatial-analysis.yaml, "
                "not the Python package builder. " + tool.availability_note
            )
        if not self.settings.tool_build_trigger or not self.settings.tool_image_repository:
            return False
        image_uri = (
            f"{self.settings.tool_image_repository.rstrip('/')}/{tool.id}:{tool.commit_sha[:12]}"
        )
        endpoint = (
            "https://cloudbuild.googleapis.com/v1/projects/"
            f"{self.settings.gcp_project}/locations/{self.settings.gcp_region}/triggers/"
            f"{self.settings.tool_build_trigger}:run"
        )
        body: dict[str, Any] = {
            "projectId": self.settings.gcp_project,
            "branchName": "main",
            "substitutions": {
                "_SOURCE_URL": tool.source_url,
                "_COMMIT_SHA": tool.commit_sha,
                "_TOOL_ID": tool.id,
                "_IMAGE_URI": image_uri,
                "_IMPORT_NAME": tool.name.lower().replace("-", "_").replace(" ", "_"),
            },
        }

        def submit() -> None:
            credentials, _ = default(  # type: ignore[no-untyped-call]
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            session = AuthorizedSession(credentials)  # type: ignore[no-untyped-call]
            response = session.post(endpoint, json=body, timeout=30)
            response.raise_for_status()

        await asyncio.to_thread(submit)
        tool.status = ToolStatus.BUILDING
        await self.repository.put_tool(tool)
        return True
