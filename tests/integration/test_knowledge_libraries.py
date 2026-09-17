"""Generic-library and explicit-binding regression coverage."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.main import app
from backend.models.brand_knowledge import (
    BrandProjectScope,
    KnowledgeLibrary,
    KnowledgePage,
    ProjectKnowledgeBinding,
)
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.studio import StudioProject, StudioWorkspace
from backend.security.identity import RequestIdentity, get_request_identity


@pytest.fixture
async def libraries(client, db_session, monkeypatch):
    from backend.security import identity

    user = User(subject="knowledge-library-owner")
    workspace = Workspace(name="Knowledge Workspace", slug="knowledge-library-workspace")
    other = Workspace(name="Other Workspace", slug="other-knowledge-library-workspace")
    db_session.add_all((user, workspace, other))
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.ADMIN)
    )
    db_session.add(StudioWorkspace(id=workspace.id, name="Studio", slug="knowledge-library-studio"))
    project = StudioProject(
        workspace_id=workspace.id,
        name="Knowledge Project",
        slug="knowledge-project",
        created_by_user_id=user.id,
    )
    db_session.add(project)
    await db_session.commit()
    actor = RequestIdentity(subject=user.subject)
    app.dependency_overrides[get_request_identity] = lambda: actor
    monkeypatch.setattr(identity, "get_request_identity", AsyncMock(return_value=actor))
    root = f"/api/v1/workspaces/{workspace.id}/knowledge-libraries"
    return {"root": root, "workspace": workspace.id, "other": other.id, "project": project.id}


async def test_generic_library_does_not_create_brand_and_rejects_product_scope(
    client, db_session, libraries
):
    scope = libraries
    response = await client.post(
        scope["root"], json={"name": "Research Notes", "description": "No brand required"}
    )
    assert response.status_code == 201, response.text
    library = response.json()["data"]
    assert library["legacy_brand_id"] is None
    stored = await db_session.get(KnowledgeLibrary, library["id"])
    assert stored and stored.legacy_brand_id is None

    page = await client.post(
        f"{scope['root']}/{library['id']}/pages",
        json={"title": "General", "content": "Verified general knowledge"},
    )
    assert page.status_code == 201, page.text
    page_data = page.json()["data"]
    assert page_data["library_id"] == library["id"]
    assert page_data["brand_id"] is None
    assert (
        await client.post(
            f"{scope['root']}/{library['id']}/pages",
            json={"title": "Invalid product", "product_id": "not-a-product"},
        )
    ).status_code == 422

    published = await client.patch(
        f"{scope['root']}/{library['id']}/pages/{page_data['id']}",
        json={
            "title": page_data["title"],
            "content": page_data["content"],
            "status": "published",
            "revision": page_data["revision"],
        },
    )
    assert published.status_code == 200, published.text
    hits = await client.get(f"{scope['root']}/{library['id']}/search", params={"q": "general"})
    assert hits.status_code == 200
    assert [hit["page_id"] for hit in hits.json()["data"]] == [page_data["id"]]
    cross_workspace = await client.get(
        f"/api/v1/workspaces/{scope['other']}/knowledge-libraries"
    )
    assert cross_workspace.status_code == 403


async def test_explicit_project_binding_adds_and_removes_without_legacy_fallback(
    client, db_session, libraries
):
    scope = libraries
    library = (
        await client.post(scope["root"], json={"name": "Project Knowledge"})
    ).json()["data"]
    bindings = (
        f"/api/v1/workspaces/{scope['workspace']}/projects/{scope['project']}/knowledge-libraries"
    )
    created = await client.put(f"{bindings}/{library['id']}", json={})
    assert created.status_code == 200, created.text
    assert (await client.get(bindings)).json()["data"] == [{**library, "product_id": None}]
    assert await db_session.scalar(
        select(ProjectKnowledgeBinding).where(
            ProjectKnowledgeBinding.project_id == scope["project"],
            ProjectKnowledgeBinding.library_id == library["id"],
        )
    )
    removed = await client.delete(f"{bindings}/{library['id']}")
    assert removed.status_code == 200, removed.text
    assert (await client.get(bindings)).json()["data"] == []
    assert not await db_session.scalar(
        select(ProjectKnowledgeBinding).where(
            ProjectKnowledgeBinding.project_id == scope["project"],
            ProjectKnowledgeBinding.library_id == library["id"],
        )
    )
    assert not await db_session.scalar(
        select(BrandProjectScope).where(BrandProjectScope.project_id == scope["project"])
    )


async def test_legacy_brand_pages_and_project_scope_keep_compatibility_mapping(
    client, db_session, libraries
):
    scope = libraries
    brands = f"/api/v1/workspaces/{scope['workspace']}/brands"
    brand = (await client.post(brands, json={"name": "Legacy Brand"})).json()["data"]
    library = await db_session.scalar(
        select(KnowledgeLibrary).where(KnowledgeLibrary.legacy_brand_id == brand["id"])
    )
    assert library and library.workspace_id == scope["workspace"]
    page = await client.post(
        f"{brands}/{brand['id']}/pages", json={"title": "Legacy page", "content": "preserved"}
    )
    assert page.status_code == 201, page.text
    stored_page = await db_session.get(KnowledgePage, page.json()["data"]["id"])
    assert stored_page.library_id == library.id
    project_url = f"{brands}/{brand['id']}/projects/{scope['project']}"
    assert (await client.put(project_url, json={})).status_code == 200
    binding = await db_session.scalar(
        select(ProjectKnowledgeBinding).where(
            ProjectKnowledgeBinding.project_id == scope["project"],
            ProjectKnowledgeBinding.library_id == library.id,
        )
    )
    assert binding and binding.product_id is None
    assert (await client.delete(project_url)).status_code == 200
    assert not await db_session.scalar(
        select(ProjectKnowledgeBinding).where(
            ProjectKnowledgeBinding.project_id == scope["project"],
            ProjectKnowledgeBinding.library_id == library.id,
        )
    )
