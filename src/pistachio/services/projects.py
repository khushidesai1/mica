"""Project lifecycle and authorization boundaries."""

from __future__ import annotations

from pistachio.models import Project, ProjectCreate, ProjectPatch, ProjectStatus, utcnow
from pistachio.storage.base import Repository


class ProjectNotFound(LookupError):
    pass


class ProjectAccessDenied(PermissionError):
    pass


class ProjectService:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def create(self, principal: str, request: ProjectCreate) -> Project:
        owner = principal.strip().lower()
        members = {member.strip().lower() for member in request.members}
        members.add(owner)
        return await self.repository.create_project(
            Project(slug=request.slug, name=request.name.strip(), owner=owner, members=members)
        )

    async def require_access(self, project_id: str, principal: str) -> Project:
        project = await self.repository.get_project(project_id)
        if project is None:
            raise ProjectNotFound(project_id)
        if not project.can_access(principal):
            raise ProjectAccessDenied(project_id)
        return project

    async def require_owner(self, project_id: str, principal: str) -> Project:
        project = await self.require_access(project_id, principal)
        if project.owner.lower() != principal.lower():
            raise ProjectAccessDenied("only the project owner may perform this operation")
        return project

    async def patch(self, project_id: str, principal: str, request: ProjectPatch) -> Project:
        project = await self.require_owner(project_id, principal)
        if request.name is not None:
            project.name = request.name.strip()
        if request.members is not None:
            project.members = {member.strip().lower() for member in request.members}
            project.members.add(project.owner.lower())
        if request.memory_policy is not None:
            project.memory_policy = request.memory_policy
        project.updated_at = utcnow()
        return await self.repository.update_project(project)

    async def archive(self, project_id: str, principal: str) -> Project:
        project = await self.require_owner(project_id, principal)
        project.status = ProjectStatus.ARCHIVED
        project.updated_at = utcnow()
        return await self.repository.update_project(project)
