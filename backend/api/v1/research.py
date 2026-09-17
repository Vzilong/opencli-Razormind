"""Project-scoped research routes.  Router registration is coordinator-owned."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database import get_db
from backend.llm.resolver import resolver
from backend.models.agent_run import AgentRun, AgentSession
from backend.models.studio import StudioProject
from backend.schemas.common import ApiResponse
from backend.schemas.research import ResearchRunCreate, ResearchRunRead
from backend.security.identity import RequestIdentity, get_request_identity
from backend.security.workspace_rbac import WorkspacePermission, require_permission
from backend.services import research_service
from backend.services.studio_agent_session_access import resolve_agent_session_workspace

router = APIRouter(
    prefix="/workspaces/{workspace_id}/projects/{project_id}/research", tags=["research"]
)


async def _scope(db: AsyncSession, identity: RequestIdentity, workspace_id: str, project_id: str):
    scope = await resolve_agent_session_workspace(
        db, identity, workspace_id, context={"project_id": project_id}
    )
    require_permission(scope.access, WorkspacePermission.READ)
    if scope.studio_workspace_id == workspace_id:
        project = await db.scalar(
            select(StudioProject).where(
                StudioProject.id == project_id,
                StudioProject.workspace_id == workspace_id,
                StudioProject.archived.is_(False),
            )
        )
    elif scope.studio_workspace_id is None:
        # Governed bootstrap intentionally creates StudioProject rows using the
        # same workspace ID; `projects` is a separate legacy workflow table.
        project = await db.scalar(
            select(StudioProject).where(
                StudioProject.id == project_id,
                StudioProject.workspace_id == workspace_id,
                StudioProject.archived.is_(False),
            )
        )
    else:
        raise HTTPException(status_code=403, detail="Project scope does not match workspace")
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found in workspace")
    return scope


async def _run(db: AsyncSession, scope, project_id: str, run_id: str) -> AgentRun:
    run = await db.scalar(
        select(AgentRun)
        .options(selectinload(AgentRun.session))
        .join(AgentSession)
        .where(
            AgentRun.id == run_id,
            AgentRun.kind == "research",
            AgentSession.workspace_id == scope.workspace_id,
            AgentSession.context["project_id"].as_string() == project_id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Research run not found")
    return run


@router.get("/readiness", response_model=ApiResponse[dict])
async def get_readiness(
    workspace_id: str,
    project_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    await _scope(db, identity, workspace_id, project_id)
    return ApiResponse.ok(
        research_service.readiness(analysis_ready=await resolver.has_candidates(db, "chat"))
    )


@router.post("/runs", response_model=ApiResponse[ResearchRunRead], status_code=202)
async def start_run(
    workspace_id: str,
    project_id: str,
    body: ResearchRunCreate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    scope = await _scope(db, identity, workspace_id, project_id)
    require_permission(scope.access, WorkspacePermission.RUN_OPERATIONS_AGENTS)
    run = await research_service.create_run(
        db,
        storage_workspace_id=scope.workspace_id,
        studio_workspace_id=workspace_id,
        project_id=project_id,
        actor_subject=identity.subject,
        payload=body.model_dump(mode="json"),
    )
    if run.status == "queued":
        # The worker uses its own session. Commit before scheduling so it can
        # observe the durable queue record instead of racing this request.
        await db.commit()
        await db.refresh(run)
        research_service.queue_run(run.id)
    return ApiResponse.ok(
        ResearchRunRead.model_validate(
            research_service.run_view(run, project_id=project_id, workspace_id=scope.workspace_id)
        )
    )


@router.get("/runs", response_model=ApiResponse[list[ResearchRunRead]])
async def list_runs(
    workspace_id: str,
    project_id: str,
    limit: int = 20,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    scope = await _scope(db, identity, workspace_id, project_id)
    limit = min(max(limit, 1), 100)
    rows = (
        (
            await db.execute(
                select(AgentRun)
                .options(selectinload(AgentRun.session))
                .join(AgentSession)
                .where(
                    AgentRun.kind == "research",
                    AgentSession.workspace_id == scope.workspace_id,
                    AgentSession.context["project_id"].as_string() == project_id,
                )
                .order_by(AgentRun.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return ApiResponse.ok(
        [
            ResearchRunRead.model_validate(research_service.run_view(row, project_id=project_id))
            for row in rows
        ]
    )


@router.get("/runs/{run_id}", response_model=ApiResponse[ResearchRunRead])
async def get_run(
    workspace_id: str,
    project_id: str,
    run_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    scope = await _scope(db, identity, workspace_id, project_id)
    run = await _run(db, scope, project_id, run_id)
    return ApiResponse.ok(
        ResearchRunRead.model_validate(research_service.run_view(run, project_id=project_id))
    )
