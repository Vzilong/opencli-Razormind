"""ASGI integration checks for the downstream Agent data router."""

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1.agent_data import router
from backend.api.v1.workspaces import router as workspace_router
from backend.database import get_db
from backend.models.agent_run import AgentRun, AgentSession
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.studio import StudioProject, StudioWorkspace
from backend.models.workflow import Project as LegacyProject
from backend.security.identity import RequestIdentity, get_request_identity
from tests.fixtures.workflow_conformance import workflow_conformance_project


@pytest_asyncio.fixture
async def agent_data_client(db_session) -> AsyncGenerator[tuple[AsyncClient, dict]]:
    current = {"identity": RequestIdentity(subject="api-member")}
    test_app = FastAPI()
    test_app.include_router(workspace_router, prefix="/api/v1")
    test_app.include_router(router, prefix="/api/v1")

    async def override_db():
        yield db_session

    test_app.dependency_overrides[get_db] = override_db
    test_app.dependency_overrides[get_request_identity] = lambda: current["identity"]
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        yield client, current


async def _scope(db_session):
    user = User(subject="api-member")
    workspace = Workspace(name="API workspace", slug="agent-data-api")
    db_session.add_all([user, workspace])
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WorkspaceRole.VIEWER,
            ),
            StudioWorkspace(
                id=workspace.id,
                name="API Studio",
                slug="agent-data-api-studio",
            ),
        ]
    )
    await db_session.flush()
    project = StudioProject(
        workspace_id=workspace.id,
        name="API project",
        slug="agent-data-api-project",
        created_by_user_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()
    return workspace, project


@pytest.mark.asyncio
async def test_agent_data_endpoints_use_common_envelope_and_authorize(
    agent_data_client, db_session
):
    client, current = agent_data_client
    workspace, project = await _scope(db_session)
    base = f"/api/v1/workspaces/{workspace.id}/projects/{project.id}/agent-data"

    records = await client.get(f"{base}/records")
    assert records.status_code == 200
    assert records.json()["data"] == {
        "items": [],
        "total": 0,
        "truncated": False,
        "total_is_exact": True,
        "projection_truncated": False,
    }

    context = await client.get(f"{base}/context", params={"q": "unknown"})
    assert context.status_code == 200
    assert context.json()["data"]["matches"] == []
    assert context.json()["data"]["gaps"] == [
        "Project has no authorized knowledge binding.",
        "No matching project records, knowledge, or research evidence were found.",
    ]

    capabilities = await client.get(f"{base}/capabilities")
    assert capabilities.status_code == 200
    capability_data = capabilities.json()["data"]
    assert capability_data["records"] is True
    assert capability_data["context"] is True
    assert isinstance(capability_data["research"], bool)
    assert capability_data["mcp_path"] == "/mcp"

    current["identity"] = RequestIdentity(subject="api-outsider")
    denied = await client.get(f"{base}/records")
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_context_rest_returns_partial_research_with_provenance_and_gaps(
    agent_data_client, db_session
):
    client, _ = agent_data_client
    workspace, project = await _scope(db_session)
    session = AgentSession(
        workspace_id=workspace.id,
        actor_subject="api-member",
        context={"studio_workspace_id": workspace.id, "project_id": project.id},
    )
    run = AgentRun(
        session=session,
        kind="research",
        status="partial",
        goal="Collect durable evidence",
        request_payload={
            "template_id": "research-brief",
            "question": "How is evidence retained?",
            "request_id": "api-partial-research",
        },
        reply_payload={
            "result": {
                "summary": "Evidence retained.",
                "findings": [],
                "sources": [
                    {
                        "id": "api-source",
                        "url": "https://docs.example/evidence",
                        "title": "Evidence retention",
                        "fetched_at": "2026-09-14T00:00:00Z",
                        "content_hash": "api-source-hash",
                        "excerpt": "Durable captured evidence",
                        "status": "fetched",
                        "content": "Durable captured evidence is available to downstream agents.",
                    }
                ],
                "gaps": ["No configured model analyzed the evidence."],
                "changes": [],
            }
        },
    )
    db_session.add(run)
    await db_session.flush()

    base = f"/api/v1/workspaces/{workspace.id}/projects/{project.id}/agent-data"
    response = await client.get(f"{base}/context", params={"q": "durable evidence"})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    match = next(item for item in data["matches"] if item["source"]["type"] == "research")
    assert match["source"]["run_id"] == run.id
    assert match["source"]["status"] == "partial"
    assert match["source"]["evidence_kind"] == "captured-source"
    assert match["source"]["source"]["content_hash"] == "api-source-hash"
    assert "content" not in match["source"]["source"]
    assert data["gaps"][0] == (f"Research run {run.id}: No configured model analyzed the evidence.")


@pytest.mark.asyncio
async def test_context_rejects_blank_and_limits_are_bounded(agent_data_client, db_session):
    client, _ = agent_data_client
    workspace, project = await _scope(db_session)
    base = f"/api/v1/workspaces/{workspace.id}/projects/{project.id}/agent-data"

    assert (await client.get(f"{base}/context", params={"q": "   "})).status_code == 422
    assert (await client.get(f"{base}/context", params={"q": "x", "limit": 21})).status_code == 422
    assert (await client.get(f"{base}/records", params={"limit": 101})).status_code == 422


@pytest.mark.asyncio
async def test_governance_bootstrap_project_is_immediately_queryable(agent_data_client, db_session):
    client, current = agent_data_client
    user = User(subject="api-member")
    workspace = Workspace(name="Bootstrap workspace", slug="agent-data-bootstrap")
    db_session.add_all([user, workspace])
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=user.id,
                role=WorkspaceRole.ADMIN,
            ),
            StudioWorkspace(
                id=workspace.id,
                name="Bootstrap Studio",
                slug="agent-data-bootstrap-studio",
            ),
        ]
    )
    await db_session.flush()

    created = await client.post(
        f"/api/v1/governance/workspaces/{workspace.id}/projects/bootstrap",
        json={
            "project": {
                "name": "Bootstrap project",
                "slug": "agent-data-bootstrap-project",
            },
            "workflow": {
                "name": "Bootstrap workflow",
                "graph": workflow_conformance_project(),
            },
        },
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["data"]["project"]["id"]

    legacy = LegacyProject(
        workspace_id=workspace.id,
        name="Existing legacy project",
        slug="existing-legacy-project",
        created_by_user_id=user.id,
    )
    other_workspace = Workspace(name="Other workspace", slug="agent-data-other")
    db_session.add_all([legacy, other_workspace])
    await db_session.flush()
    db_session.add_all(
        [
            WorkspaceMembership(
                workspace_id=other_workspace.id,
                user_id=user.id,
                role=WorkspaceRole.VIEWER,
            ),
            StudioWorkspace(
                id=other_workspace.id,
                name="Other Studio",
                slug="agent-data-other-studio",
            ),
        ]
    )
    await db_session.flush()
    other_project = StudioProject(
        workspace_id=other_workspace.id,
        name="Other project",
        slug="agent-data-other-project",
        created_by_user_id=user.id,
    )
    db_session.add(other_project)
    await db_session.flush()

    listed = await client.get(f"/api/v1/governance/workspaces/{workspace.id}/projects")
    assert listed.status_code == 200, listed.text
    listed_projects = listed.json()["data"]
    assert {item["id"] for item in listed_projects} == {project_id, legacy.id}
    assert all(item["workspace_id"] == workspace.id for item in listed_projects)

    other_list = await client.get(f"/api/v1/governance/workspaces/{other_workspace.id}/projects")
    assert other_list.status_code == 200, other_list.text
    assert [item["id"] for item in other_list.json()["data"]] == [other_project.id]

    records = await client.get(
        f"/api/v1/workspaces/{workspace.id}/projects/{project_id}/agent-data/records"
    )
    assert records.status_code == 200, records.text
    assert records.json()["data"] == {
        "items": [],
        "total": 0,
        "truncated": False,
        "total_is_exact": True,
        "projection_truncated": False,
    }

    current["identity"] = RequestIdentity(subject="api-outsider")
    denied = await client.get(f"/api/v1/governance/workspaces/{workspace.id}/projects")
    assert denied.status_code == 403
