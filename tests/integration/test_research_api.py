from __future__ import annotations

import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from backend.api.v1.chat import _run_read_tool
from backend.api.v1.research import router
from backend.api.v1.workspaces import router as workspace_router
from backend.database import get_db
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.studio import StudioProject, StudioWorkspace
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import research_service
from tests.fixtures.workflow_conformance import workflow_conformance_project


async def _research_client(db_session, subject: str) -> AsyncClient:
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject=subject)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _governed_project(db_session):
    admin = User(subject="research-admin")
    viewer = User(subject="research-viewer")
    workspace = Workspace(name="Research", slug="research")
    db_session.add_all((admin, viewer, workspace))
    await db_session.flush()
    db_session.add_all(
        (
            StudioWorkspace(id=workspace.id, name=workspace.name, slug=workspace.slug),
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=admin.id, role=WorkspaceRole.ADMIN
            ),
            WorkspaceMembership(
                workspace_id=workspace.id, user_id=viewer.id, role=WorkspaceRole.VIEWER
            ),
        )
    )
    project = StudioProject(
        workspace_id=workspace.id,
        name="Research project",
        slug="research-project",
        created_by_user_id=admin.id,
    )
    other = StudioProject(
        workspace_id=workspace.id,
        name="Other project",
        slug="other-project",
        created_by_user_id=admin.id,
    )
    db_session.add_all((project, other))
    await db_session.commit()
    return workspace, project, other


def _payload(*, request_id: str = "request-1") -> dict:
    return {
        "template_id": "research-brief",
        "question": "What changed?",
        "seed_urls": ["https://docs.python.org/3/"],
        "max_sources": 1,
        "request_id": request_id,
    }


async def test_governed_project_allows_read_and_admin_run(db_session, monkeypatch):
    workspace, project, _ = await _governed_project(db_session)
    monkeypatch.setattr(research_service, "queue_run", lambda _run_id: None)
    async with await _research_client(db_session, "research-admin") as client:
        readiness = await client.get(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/readiness"
        )
        started = await client.post(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs", json=_payload()
        )
        listed = await client.get(f"/workspaces/{workspace.id}/projects/{project.id}/research/runs")

    assert readiness.status_code == 200
    assert started.status_code == 202
    assert listed.status_code == 200
    assert listed.json()["data"][0]["id"] == started.json()["data"]["id"]


async def test_governed_project_allows_research_tools_from_agent_chat(db_session):
    workspace, project, _ = await _governed_project(db_session)

    readiness = await _run_read_tool(
        db_session,
        "research_readiness",
        {"project_id": project.id},
        identity=RequestIdentity(subject="research-admin"),
        workspace_id=workspace.id,
    )

    assert readiness["fetch_ready"] is True


async def test_chat_research_tools_never_include_retained_page_bodies(db_session):
    workspace, project, _ = await _governed_project(db_session)
    run = await research_service.create_run(
        db_session,
        storage_workspace_id=workspace.id,
        studio_workspace_id=workspace.id,
        project_id=project.id,
        actor_subject="research-admin",
        payload=_payload(),
    )
    run.status = "partial"
    run.reply_payload = {
        "result": {
            "summary": "Captured only",
            "findings": [],
            "gaps": ["No model"],
            "changes": [],
            "sources": [
                {
                    "id": "source-1",
                    "url": "https://example.test/",
                    "fetched_at": "2026-09-14T00:00:00Z",
                    "status": "fetched",
                    "excerpt": "Public excerpt",
                    "content": "untrusted-retained-body" * 2000,
                }
            ],
        }
    }
    await db_session.flush()
    detail = await _run_read_tool(
        db_session,
        "get_research_run",
        {"project_id": project.id, "run_id": run.id},
        identity=RequestIdentity(subject="research-admin"),
        workspace_id=workspace.id,
    )
    assert detail["result"]["sources"][0]["excerpt"] == "Public excerpt"
    assert "content" not in detail["result"]["sources"][0]
    listed = await _run_read_tool(
        db_session,
        "list_research_runs",
        {"project_id": project.id},
        identity=RequestIdentity(subject="research-admin"),
        workspace_id=workspace.id,
    )
    assert listed[0]["view_truncated"] is True
    assert listed[0]["result"]["source_count"] == 1
    assert "sources" not in listed[0]["result"]


async def test_viewer_cannot_start_and_runs_are_project_scoped(db_session, monkeypatch):
    workspace, project, other = await _governed_project(db_session)
    monkeypatch.setattr(research_service, "queue_run", lambda _run_id: None)
    async with await _research_client(db_session, "research-viewer") as viewer_client:
        denied = await viewer_client.post(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs", json=_payload()
        )
    async with await _research_client(db_session, "research-admin") as admin_client:
        started = await admin_client.post(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs", json=_payload()
        )
        hidden = await admin_client.get(
            f"/workspaces/{workspace.id}/projects/{other.id}/research/runs/"
            f"{started.json()['data']['id']}"
        )

    assert denied.status_code == 403
    assert started.status_code == 202
    assert hidden.status_code == 404


async def test_request_id_conflict_is_reported_at_api_boundary(db_session, monkeypatch):
    workspace, project, _ = await _governed_project(db_session)
    monkeypatch.setattr(research_service, "queue_run", lambda _run_id: None)
    async with await _research_client(db_session, "research-admin") as client:
        first = await client.post(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs", json=_payload()
        )
        conflict_body = _payload()
        conflict_body["question"] = "A changed request"
        conflict = await client.post(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs",
            json=conflict_body,
        )

    assert first.status_code == 202
    assert conflict.status_code == 409


async def test_failed_research_api_does_not_reflect_secret_or_hostile_exception(
    db_session, monkeypatch
):
    workspace, project, _ = await _governed_project(db_session)
    secret_url = "https://alice:seed-password@public.example/path?api_key=seed-api-key&topic=ai"
    hostile = (
        f"worker exploded: {secret_url} Authorization: Bearer bearer-secret "
        "password=body-password response body=hostile instructions"
    )

    async def broken_fetch(_url):
        raise RuntimeError(hostile)

    monkeypatch.setattr(research_service, "_fetch", broken_fetch)
    payload = _payload(request_id="api-failure")
    payload["seed_urls"] = [secret_url]
    run = await research_service.create_run(
        db_session,
        storage_workspace_id=workspace.id,
        studio_workspace_id=workspace.id,
        project_id=project.id,
        actor_subject="research-admin",
        payload=payload,
    )
    await db_session.commit()
    await research_service.execute_run(run.id, db_session)

    async with await _research_client(db_session, "research-admin") as client:
        response = await client.get(
            f"/workspaces/{workspace.id}/projects/{project.id}/research/runs/{run.id}"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "failed"
    assert body["data"]["error"] == research_service._safe_failure("execution")
    response_text = json.dumps(body)
    for secret in (secret_url, "seed-password", "seed-api-key", "bearer-secret", "body-password"):
        assert secret not in response_text
    assert "hostile instructions" not in response_text


async def test_governance_bootstrap_project_is_a_valid_research_scope(db_session, monkeypatch):
    workspace, _, _ = await _governed_project(db_session)
    monkeypatch.setattr(research_service, "queue_run", lambda _run_id: None)
    app = FastAPI()
    app.include_router(workspace_router)
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject="research-admin")

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    bootstrap_body = {
        "project": {"name": "Bootstrapped", "slug": "bootstrapped", "app_type": "workflow"},
        "workflow": {"name": "Primary", "graph": workflow_conformance_project()},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post(
            f"/governance/workspaces/{workspace.id}/projects/bootstrap", json=bootstrap_body
        )
        project_id = created.json()["data"]["project"]["id"]
        readiness = await client.get(
            f"/workspaces/{workspace.id}/projects/{project_id}/research/readiness"
        )

    assert created.status_code == 201
    assert readiness.status_code == 200
