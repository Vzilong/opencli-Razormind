"""Project-scoped read API consumed by downstream Agents and MCP."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.schemas.agent_data import (
    AgentDataCapabilities,
    ProjectContextResult,
    ProjectRecordList,
)
from backend.schemas.common import ApiResponse
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_data_service

router = APIRouter(
    prefix="/workspaces/{workspace_id}/projects/{project_id}/agent-data",
    tags=["agent-data"],
)


@router.get("/records", response_model=ApiResponse[ProjectRecordList])
async def list_records(
    workspace_id: str,
    project_id: str,
    q: str | None = Query(default=None, max_length=500),
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    identity: RequestIdentity = Depends(get_request_identity),
) -> ApiResponse[ProjectRecordList]:
    result = await agent_data_service.list_project_records(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
        query=q,
        limit=limit,
    )
    return ApiResponse.ok(result)


@router.get("/context", response_model=ApiResponse[ProjectContextResult])
async def search_context(
    workspace_id: str,
    project_id: str,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(default=8, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    identity: RequestIdentity = Depends(get_request_identity),
) -> ApiResponse[ProjectContextResult]:
    result = await agent_data_service.search_project_context(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
        query=q,
        limit=limit,
    )
    return ApiResponse.ok(result)


@router.get("/capabilities", response_model=ApiResponse[AgentDataCapabilities])
async def capabilities(
    workspace_id: str,
    project_id: str,
    db: AsyncSession = Depends(get_db),
    identity: RequestIdentity = Depends(get_request_identity),
) -> ApiResponse[AgentDataCapabilities]:
    await agent_data_service.authorize_project(
        db,
        workspace_id=workspace_id,
        project_id=project_id,
        identity=identity,
    )
    return ApiResponse.ok(agent_data_service.get_capabilities())


__all__ = ["router"]
