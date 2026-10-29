"""Versioned JSON API for projects, tasks, approvals, memory, and uploads."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from mica.api.auth import Principal
from mica.api.dependencies import Services, get_services
from mica.api.migration import api_principal, replay_slack
from mica.connectors.github_catalog import GitHubCatalog
from mica.connectors.gmail import renew_watch
from mica.models import (
    ApprovalDecision,
    MemoryPatch,
    Project,
    ProjectCreate,
    ProjectPatch,
    TaskContextSubmission,
    TaskEnvelope,
    TaskListItem,
    TaskProgress,
    TaskRecord,
    TaskResult,
    TaskSource,
    TaskStatus,
    TaskSubmission,
    ToolStatus,
    new_id,
    utcnow,
)
from mica.services.artifacts import (
    ArtifactUnavailable,
    delivery_manifest,
    prepare_artifact_delivery,
    prepare_individual_artifact,
    should_bundle_artifacts,
)
from mica.services.tasks import TaskInteractionConflict
from mica.services.uploads import UploadRejected

router = APIRouter(prefix="/api/v1")
PrincipalDep = Annotated[Principal, Depends(api_principal)]
ServicesDep = Annotated[Services, Depends(get_services)]


@router.get("/projects", response_model=list[Project])
async def list_projects(principal: PrincipalDep, services: ServicesDep) -> list[Project]:
    return await services.repository.list_projects(principal.id)


@router.post("/projects", response_model=Project, status_code=201)
async def create_project(
    request: ProjectCreate, principal: PrincipalDep, services: ServicesDep
) -> Project:
    return await services.projects.create(principal.id, request)


@router.get("/projects/{project_id}", response_model=Project)
async def get_project(project_id: str, principal: PrincipalDep, services: ServicesDep) -> Project:
    return await services.projects.require_access(project_id, principal.id)


@router.get("/projects/{project_id}/tasks", response_model=list[TaskListItem])
async def list_project_tasks(
    project_id: str,
    principal: PrincipalDep,
    services: ServicesDep,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[TaskListItem]:
    await services.projects.require_access(project_id, principal.id)
    tasks = await services.repository.list_tasks(project_id, limit=limit)
    return [
        TaskListItem(
            id=task.id,
            public_name=task.public_name,
            project_id=task.envelope.project_id,
            prompt=task.envelope.prompt,
            source=task.envelope.source,
            status=task.status,
            current_stage=task.current_stage,
            error=task.error,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )
        for task in tasks
    ]


@router.patch("/projects/{project_id}", response_model=Project)
async def patch_project(
    project_id: str,
    request: ProjectPatch,
    principal: PrincipalDep,
    services: ServicesDep,
) -> Project:
    return await services.projects.patch(project_id, principal.id, request)


@router.post("/projects/{project_id}/archive", response_model=Project)
async def archive_project(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> Project:
    return await services.projects.archive(project_id, principal.id)


@router.get("/projects/{project_id}/memories")
async def list_memories(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[dict[str, object]]:
    await services.projects.require_access(project_id, principal.id)
    items = await services.repository.list_memories(project_id)
    payloads: list[dict[str, object]] = []
    for item in items:
        payload = item.model_dump(mode="json", exclude={"embedding"})
        payload["source_task_name"] = None
        payload["artifact_names"] = []
        if item.source_task_id:
            task = await services.repository.get_task(item.source_task_id)
            if task is not None and task.envelope.project_id == project_id:
                payload["source_task_name"] = task.public_name
                if task.result is not None:
                    payload["artifact_names"] = [
                        artifact.name for artifact in task.result.artifacts
                    ]
        payloads.append(payload)
    return payloads


@router.patch("/projects/{project_id}/memories/{memory_id}")
async def patch_memory(
    project_id: str,
    memory_id: str,
    request: MemoryPatch,
    principal: PrincipalDep,
    services: ServicesDep,
) -> dict[str, object]:
    await services.projects.require_access(project_id, principal.id)
    item = await services.repository.get_memory(memory_id)
    if item is None or item.project_id != project_id:
        raise HTTPException(status_code=404, detail="memory not found")
    if request.content is not None and request.content != item.content:
        item.content = request.content
        item.embedding = []
        item.version += 1
    if request.status is not None:
        item.status = request.status
    if request.tags is not None:
        item.tags = request.tags
    if request.entities is not None:
        item.entities = request.entities
    item.updated_at = utcnow()
    item = await services.memory.promote(item)
    return item.model_dump(mode="json", exclude={"embedding"})


@router.delete("/projects/{project_id}/memories/{memory_id}", status_code=204)
async def delete_memory(
    project_id: str, memory_id: str, principal: PrincipalDep, services: ServicesDep
) -> None:
    await services.projects.require_access(project_id, principal.id)
    item = await services.repository.get_memory(memory_id)
    if item is None or item.project_id != project_id:
        raise HTTPException(status_code=404, detail="memory not found")
    await services.repository.delete_memory(memory_id)


@router.post("/projects/{project_id}/compact")
async def compact_project(
    project_id: str, principal: PrincipalDep, services: ServicesDep
) -> dict[str, object]:
    await services.projects.require_owner(project_id, principal.id)
    summary = await services.compactor.compact(project_id, force=True)
    return {"summary": summary.model_dump(mode="json") if summary else None}


@router.post("/tasks", response_model=TaskRecord, status_code=202)
async def create_task(
    request: TaskSubmission, principal: PrincipalDep, services: ServicesDep
) -> TaskRecord:
    envelope = TaskEnvelope(
        **request.model_dump(exclude={"source", "conversation_id", "external_event_id"}),
        source=TaskSource.WEB,
        conversation_id=new_id("conv"),
        external_event_id=new_id("evt"),
        principal_id=principal.id,
    )
    task, created = await services.tasks.create(envelope)
    if created:
        await services.dispatcher.enqueue(task.id)
    return task


@router.get("/tasks/{task_id}", response_model=TaskRecord)
async def get_task(task_id: str, principal: PrincipalDep, services: ServicesDep) -> TaskRecord:
    return await services.tasks.require_access(task_id, principal.id)


@router.get("/tasks/{task_id}/plan")
async def get_task_plan(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> dict[str, object]:
    task = await services.tasks.require_access(task_id, principal.id)
    return {
        "planner_version": task.planner_version,
        "master_plan": task.master_plan.model_dump(mode="json") if task.master_plan else None,
        "decision_set": task.decision_set.model_dump(mode="json") if task.decision_set else None,
        "figure_specs": [item.model_dump(mode="json") for item in task.figure_specs],
    }


@router.get("/tasks/{task_id}/stages")
async def get_task_stages(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> dict[str, object]:
    task = await services.tasks.require_access(task_id, principal.id)
    return {
        "planner_version": task.planner_version,
        "current_stage": task.current_stage.value if task.current_stage else None,
        "stages": [item.model_dump(mode="json") for item in task.stage_summaries],
    }


@router.get("/tasks/{task_id}/artifacts")
async def list_artifacts(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[dict[str, object]]:
    task = await services.tasks.require_access(task_id, principal.id)
    return [
        artifact.model_dump(mode="json")
        for artifact in (task.result.artifacts if task.result else [])
    ]


@router.get("/tasks/{task_id}/artifact-delivery")
async def get_artifact_delivery(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> list[dict[str, object]]:
    task = await services.tasks.require_access(task_id, principal.id)
    return delivery_manifest(task)


@router.get("/tasks/{task_id}/artifacts/archive")
async def download_artifact_archive(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> FileResponse:
    task = await services.tasks.require_access(task_id, principal.id)
    artifacts = task.result.artifacts if task.result else []
    if not should_bundle_artifacts(artifacts):
        raise HTTPException(
            status_code=404, detail="this task's artifacts are available individually"
        )
    try:
        prepared = await prepare_artifact_delivery(task, services.settings)
    except ArtifactUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    file = prepared.files[0]
    return FileResponse(
        path=file.path,
        media_type=file.media_type,
        filename=file.name,
        background=BackgroundTask(prepared.cleanup),
    )


@router.get("/tasks/{task_id}/artifacts/{artifact_index}/download")
async def download_individual_artifact(
    task_id: str,
    artifact_index: int,
    principal: PrincipalDep,
    services: ServicesDep,
) -> FileResponse:
    task = await services.tasks.require_access(task_id, principal.id)
    artifacts = task.result.artifacts if task.result else []
    if should_bundle_artifacts(artifacts):
        raise HTTPException(status_code=404, detail="this task's artifacts are available as a ZIP")
    if artifact_index < 0 or artifact_index >= len(artifacts):
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        prepared = await prepare_individual_artifact(artifacts[artifact_index], services.settings)
    except ArtifactUnavailable as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    file = prepared.files[0]
    disposition = "inline" if file.media_type.startswith("image/") else "attachment"
    return FileResponse(
        path=file.path,
        media_type=file.media_type,
        filename=file.name,
        content_disposition_type=disposition,
        background=BackgroundTask(prepared.cleanup),
    )


@router.post("/tasks/{task_id}/approvals/{approval_id}", response_model=TaskRecord)
async def decide_approval(
    task_id: str,
    approval_id: str,
    request: ApprovalDecision,
    principal: PrincipalDep,
    services: ServicesDep,
) -> TaskRecord:
    try:
        task = await services.tasks.decide_approval(
            task_id,
            approval_id,
            principal.id,
            request,
            defer_replan=True,
        )
        if task.status == TaskStatus.RECEIVED:
            await services.dispatcher.enqueue(task.id)
        return task
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/cancel", response_model=TaskRecord)
async def cancel_task(task_id: str, principal: PrincipalDep, services: ServicesDep) -> TaskRecord:
    return await services.tasks.cancel(task_id, principal.id)


@router.post("/tasks/{task_id}/context", response_model=TaskRecord, status_code=202)
async def add_task_context(
    task_id: str,
    request: TaskContextSubmission,
    principal: PrincipalDep,
    services: ServicesDep,
) -> TaskRecord:
    try:
        task, added = await services.tasks.add_context(
            task_id,
            principal.id,
            request.message,
            source=TaskSource.WEB,
            external_event_id=request.external_event_id,
        )
    except TaskInteractionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if added:
        await services.dispatcher.enqueue(task.id)
    return task


@router.post("/uploads", status_code=201)
async def upload_file(
    principal: PrincipalDep,
    services: ServicesDep,
    file: Annotated[UploadFile, File()],
) -> dict[str, object]:
    try:
        attachment = await services.uploads.accept(file, principal.id)
    except UploadRejected as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return attachment.model_dump(mode="json")


@router.post("/internal/tasks/{task_id}/process", include_in_schema=False)
async def process_internal_task(
    task_id: str, principal: PrincipalDep, services: ServicesDep
) -> dict[str, str]:
    if services.settings.env in {"staging", "production"} and (
        principal.id != services.settings.cloud_tasks_service_account.lower()
    ):
        raise HTTPException(status_code=403, detail="untrusted task dispatcher")
    task = await services.tasks.process(task_id)
    if (
        task.builder_protocol_version == 2
        and task.development
        and not task.pending_tool_builds
        and task.status in {TaskStatus.PLANNING, TaskStatus.SUBMITTING}
    ):
        await services.dispatcher.enqueue(task.id)
    return {"task_id": task.id, "status": task.status.value}


@router.post("/internal/maintenance/compact-idle", include_in_schema=False)
async def compact_idle_projects(principal: PrincipalDep, services: ServicesDep) -> dict[str, int]:
    if services.settings.env in {"staging", "production"} and (
        principal.id != services.settings.cloud_tasks_service_account.lower()
    ):
        raise HTTPException(status_code=403, detail="untrusted scheduler")
    from mica.agents.analysis import observability

    failures = await observability.maintenance(services.repository, services.settings, purge=True)
    return {"compacted_projects": await services.tasks.close_idle_projects(), **failures}


@router.post("/internal/maintenance/log-failure", include_in_schema=False)
async def ingest_failure_log(
    request: Request, principal: PrincipalDep, services: ServicesDep
) -> dict[str, str]:
    _require_internal_principal(principal, services)
    from mica.services import failure_logs

    # Stream with a hard cap so a malformed authenticated envelope cannot grow memory unbounded.
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > failure_logs.MAX_ENVELOPE:
            return {"status": "ignored"}
    observation = failure_logs.parse(
        bytes(body),
        project=services.settings.gcp_project,
        service=os.environ.get("K_SERVICE", ""),
    )
    if observation is None:
        return {"status": "ignored"}
    try:
        async with asyncio.timeout(3):
            from mica.agents.analysis.observability import correlate_platform

            observation = await correlate_platform(
                services.repository, services.settings, observation
            )
            await services.repository.record_failure(observation, time.time())
    except Exception:
        # Pub/Sub must retry: unlike application best-effort reporting, this is the sole intake.
        raise HTTPException(status_code=503, detail="incident persistence unavailable") from None
    return {"status": "recorded"}


@router.post("/internal/maintenance/reconcile-batch", include_in_schema=False)
async def reconcile_batch_jobs(principal: PrincipalDep, services: ServicesDep) -> dict[str, int]:
    _require_internal_principal(principal, services)
    from mica.agents.analysis import observability

    # The shared Batch reconciler may be disabled or fail; incident delivery still runs.
    failures = await observability.maintenance(services.repository, services.settings)
    result = await services.tasks.reconcile_batch_jobs()
    return {**result, **failures}


@router.post("/internal/batch/completion", include_in_schema=False)
async def batch_completion(
    request: Request,
    principal: PrincipalDep,
    services: ServicesDep,
) -> dict[str, str]:
    if services.settings.env in {"staging", "production"} and (
        principal.id != services.settings.cloud_tasks_service_account.lower()
    ):
        raise HTTPException(status_code=403, detail="untrusted completion publisher")
    envelope = await request.json()
    message = envelope.get("message", {})
    event_id = f"batch:{message.get('messageId', '')}"
    claim = await services.repository.begin_event(event_id)
    if claim.status == "completed":
        return {"status": "duplicate"}
    if claim.status == "processing":
        # A non-2xx response tells Pub/Sub to redeliver after the active lease holder finishes.
        raise HTTPException(status_code=409, detail="batch event is already being processed")
    try:
        data = json.loads(base64.b64decode(message.get("data", "")).decode())
        task_id = str(data["task_id"])
        attempt = int(data["attempt"])
        if data.get("kind") == "progress":
            progress = TaskProgress.model_validate(data["progress"])
            task = await services.tasks.record_batch_progress(task_id, attempt, progress)
        else:
            from google.cloud.storage import Client as StorageClient

            blob = (
                StorageClient(project=services.settings.gcp_project)
                .bucket(services.settings.gcs_bucket)
                .blob(f"tasks/{task_id}/attempt-{attempt}/worker-result.json")
            )
            result = TaskResult.model_validate_json(await asyncio.to_thread(blob.download_as_text))
            if result.operation_id:
                from mica.execution.publication import verify_cloud_commit

                commit = await asyncio.to_thread(
                    verify_cloud_commit,
                    StorageClient(project=services.settings.gcp_project).bucket(
                        services.settings.gcs_bucket
                    ),
                    task_id,
                    attempt,
                    result,
                    None,
                )
                if "publication_seconds" in commit:
                    result.timings["publication_seconds"] = commit["publication_seconds"]
            task = await services.tasks.complete_batch(task_id, attempt, result)
            if (
                task.builder_protocol_version == 2
                and task.development
                and not task.pending_tool_builds
                and task.status in {TaskStatus.PLANNING, TaskStatus.SUBMITTING}
            ):
                await services.dispatcher.enqueue(task.id)
    except Exception:
        await services.repository.release_event(event_id, claim.token)
        raise
    await services.repository.complete_event(event_id, claim.token)
    return {"task_id": task.id, "status": task.status.value}


def _require_internal_principal(principal: Principal, services: Services) -> None:
    if services.settings.env in {"staging", "production"} and (
        principal.id != services.settings.cloud_tasks_service_account.lower()
    ):
        raise HTTPException(status_code=403, detail="untrusted scheduler")


@router.post("/internal/gmail/renew-watch", include_in_schema=False)
async def renew_gmail_watch(principal: PrincipalDep, services: ServicesDep) -> dict[str, object]:
    _require_internal_principal(principal, services)
    return await renew_watch(services)


@router.post("/internal/tools/catalog", include_in_schema=False)
async def catalog_github_tools(principal: PrincipalDep, services: ServicesDep) -> dict[str, int]:
    _require_internal_principal(principal, services)
    return {"cataloged": await GitHubCatalog(services.repository, services.settings).sync()}


@router.post("/internal/tools/build-completion", include_in_schema=False)
async def tool_build_completion(
    request: Request,
    principal: PrincipalDep,
    services: ServicesDep,
) -> dict[str, str]:
    _require_internal_principal(principal, services)
    envelope = await request.json()
    message = envelope.get("message", {})
    event_id = f"cloud-build:{message.get('messageId', '')}"
    if not await services.repository.mark_event(event_id):
        return {"status": "duplicate"}
    build = json.loads(base64.b64decode(message.get("data", "")).decode())
    status = str(build.get("status", ""))
    terminal = {"SUCCESS", "FAILURE", "INTERNAL_ERROR", "TIMEOUT", "CANCELLED"}
    if status not in terminal:
        return {"status": "ignored"}
    tool_id = build.get("substitutions", {}).get("_TOOL_ID")
    # The project-wide subscription also delivers ordinary deployment builds.
    if not isinstance(tool_id, str) or not tool_id.strip():
        return {"status": "ignored"}
    tool = await services.repository.get_tool(tool_id)
    if tool is None:
        raise HTTPException(status_code=404, detail="tool build record not found")
    if status == "SUCCESS":
        tool.status = ToolStatus.AVAILABLE
        images = build.get("results", {}).get("images", [])
        if images:
            tool.image_digest = str(images[0].get("digest", ""))
        tool.smoke_test = "passed"
    else:
        tool.status = ToolStatus.FAILED
        tool.smoke_test = f"build ended with {status}"
    tool.updated_at = utcnow()
    await services.repository.put_tool(tool)
    if tool.status == ToolStatus.AVAILABLE:
        for project in await services.repository.list_all_projects():
            for task in await services.repository.list_tasks(project.id, limit=100):
                if (
                    (task.status == TaskStatus.NEEDS_INPUT or task.pending_tool_builds)
                    and task.tool_plan
                    and tool.id in task.tool_plan.proposed_tool_ids
                ):
                    await services.dispatcher.enqueue(task.id)
    return {"tool_id": tool.id, "status": tool.status.value}


@router.get("/healthz", include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/internal/maintenance/replay-slack/{identifier}", include_in_schema=False)
async def replay_migration_slack(
    identifier: str, request: Request, principal: PrincipalDep, services: ServicesDep
) -> dict[str, str]:
    # This operator route never inherits the local-development internal-auth exception.
    if principal.id != services.settings.cloud_tasks_service_account.lower():
        raise HTTPException(403, "Untrusted migration dispatcher")
    return await replay_slack(request, identifier)
