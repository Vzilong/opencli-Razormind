"""Business scope, immutable source, review and retrieval regression coverage."""

import io
import zipfile
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.main import app
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.record import CollectedRecord
from backend.models.source import DataSource
from backend.models.studio import StudioProject, StudioWorkflow, StudioWorkspace
from backend.models.task import CollectionTask
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import brand_knowledge_service as service


@pytest.fixture
async def knowledge(client, db_session, monkeypatch):
    from backend.security import identity

    user = User(subject="knowledge-owner")
    workspace = Workspace(name="品牌工作区", slug="brand-workspace")
    other = Workspace(name="其他工作区", slug="other-workspace")
    db_session.add_all([user, workspace, other])
    await db_session.flush()
    membership = WorkspaceMembership(
        workspace_id=workspace.id, user_id=user.id, role=WorkspaceRole.ADMIN
    )
    db_session.add(membership)
    await db_session.commit()
    actor = RequestIdentity(subject=user.subject)
    app.dependency_overrides[get_request_identity] = lambda: actor
    monkeypatch.setattr(identity, "get_request_identity", AsyncMock(return_value=actor))
    root = f"/api/v1/workspaces/{workspace.id}/brands"
    first = (await client.post(root, json={"name": "高吉星"})).json()["data"]["id"]
    second = (await client.post(root, json={"name": "其他品牌"})).json()["data"]["id"]
    path = f"{root}/{first}"
    a = (await client.post(f"{path}/products", json={"name": "产品 A"})).json()["data"]["id"]
    b = (await client.post(f"{path}/products", json={"name": "产品 B"})).json()["data"]["id"]
    await db_session.commit()
    return {
        "root": root,
        "path": path,
        "workspace": workspace.id,
        "other": other.id,
        "brand": first,
        "second": second,
        "a": a,
        "b": b,
        "membership": membership,
    }


async def upload(client, path, name="资料.md", text="高吉星产品特点说明", **params):
    response = await client.post(
        f"{path}/upload", params=params, files={"file": (name, text.encode(), "text/plain")}
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def test_search_inherits_common_but_excludes_other_products_and_brands(client, knowledge):
    k = knowledge
    common = await upload(client, k["path"], text="品牌通用特点：统一售后")
    a = await upload(client, k["path"], text="产品特点：A 专属配置", product_id=k["a"])
    await upload(client, k["path"], text="产品特点：B 专属配置", product_id=k["b"])
    await upload(client, f"{k['root']}/{k['second']}", text="其他品牌特点：不能混入")
    response = await client.get(
        f"{k['path']}/search", params={"q": "产品特点是什么", "product_id": k["a"]}
    )
    assert response.status_code == 200, response.text
    assert {hit["page_id"] for hit in response.json()["data"]} == {common["id"], a["id"]}
    assert {hit["product_name"] for hit in response.json()["data"]} == {None, "产品 A"}
    assert len((await client.get(f"{k['path']}/search", params={"q": "特点"})).json()["data"]) == 3


async def test_scope_validation_blocks_cross_workspace_brand_product_and_parent(client, knowledge):
    k = knowledge
    assert (await client.get(f"/api/v1/workspaces/{k['other']}/brands")).status_code == 403
    assert (await client.get(f"{k['root']}/missing/products")).status_code == 404
    bad = await client.post(
        f"{k['root']}/{k['second']}/pages", json={"title": "wrong", "product_id": k["a"]}
    )
    assert bad.status_code == 404
    folder = (
        await client.post(
            f"{k['path']}/pages", json={"title": "A", "kind": "folder", "product_id": k["a"]}
        )
    ).json()["data"]
    bad = await client.post(
        f"{k['path']}/pages", json={"title": "B", "product_id": k["b"], "parent_id": folder["id"]}
    )
    assert bad.status_code == 422


async def test_viewer_can_search_but_cannot_upload_or_edit(client, db_session, knowledge):
    k = knowledge
    page = await upload(client, k["path"])
    k["membership"].role = WorkspaceRole.VIEWER
    await db_session.flush()
    assert (await client.get(f"{k['path']}/pages")).status_code == 200
    for method, suffix, kwargs in [
        ("POST", "/upload", {"files": {"file": ("file.md", b"text")}}),
        (
            "PATCH",
            f"/pages/{page['id']}",
            {
                "json": {
                    "title": page["title"],
                    "content": page["content"],
                    "status": "archived",
                    "revision": 1,
                }
            },
        ),
        ("POST", f"/pages/{page['id']}/summarize", {}),
    ]:
        assert (await client.request(method, k["path"] + suffix, **kwargs)).status_code == 403


async def test_upload_retains_original_deduplicates_and_rejects_rewrite(client, knowledge):
    path = knowledge["path"]
    page = await upload(client, path, name="../原文.md", text="原始内容特点")
    duplicate = await upload(client, path, name="same.md", text="原始内容特点")
    assert duplicate["id"] == page["id"]
    assert duplicate["reused"] is True
    assert page["original_name"] == "原文.md"
    download = await client.get(f"{path}/pages/{page['id']}/download")
    assert download.content.decode() == "原始内容特点"
    assert download.headers["x-content-type-options"] == "nosniff"
    bad = await client.patch(
        f"{path}/pages/{page['id']}",
        json={"title": page["title"], "content": "changed", "status": "published", "revision": 1},
    )
    assert bad.status_code == 422


async def test_duplicate_upload_does_not_silently_change_folder(client, knowledge):
    path = knowledge["path"]
    await upload(client, path)
    folder = (await client.post(f"{path}/pages", json={"title": "目录", "kind": "folder"})).json()[
        "data"
    ]
    response = await client.post(
        f"{path}/upload",
        params={"parent_id": folder["id"]},
        files={"file": ("资料.md", "高吉星产品特点说明".encode())},
    )
    assert response.status_code == 409


async def test_upload_key_is_unique_in_database(client, db_session, knowledge):
    from sqlalchemy.exc import IntegrityError

    from backend.models.brand_knowledge import KnowledgePage

    page = await upload(client, knowledge["path"])
    source = await db_session.get(KnowledgePage, page["id"])
    assert source.upload_key
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                KnowledgePage(
                    brand_id=knowledge["brand"],
                    title="concurrent",
                    kind="source",
                    status="published",
                    content="same",
                    created_by="test",
                    upload_key=source.upload_key,
                )
            )
            await db_session.flush()


async def test_review_revision_conflict_and_archive_exclusion(client, knowledge):
    path = knowledge["path"]
    page = (
        await client.post(f"{path}/pages", json={"title": "知识", "content": "特点：需要审核"})
    ).json()["data"]
    assert not (await client.get(f"{path}/search", params={"q": "特点"})).json()["data"]
    body = {"title": "知识", "content": "特点：已经核实", "status": "published", "revision": 1}
    response = await client.patch(f"{path}/pages/{page['id']}", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["revision"] == 2
    assert (await client.patch(f"{path}/pages/{page['id']}", json=body)).status_code == 409
    assert len((await client.get(f"{path}/search", params={"q": "特点"})).json()["data"]) == 1
    await client.patch(
        f"{path}/pages/{page['id']}", json={**body, "revision": 2, "status": "archived"}
    )
    assert not (await client.get(f"{path}/search", params={"q": "特点"})).json()["data"]
    versions = (await client.get(f"{path}/pages/{page['id']}/revisions")).json()["data"]
    assert [item["revision"] for item in versions] == [3, 2, 1]
    assert versions[-1]["content"] == "特点：需要审核"


async def test_answer_and_summary_use_scoped_citations_and_review(client, knowledge, monkeypatch):
    path = knowledge["path"]
    source = await upload(client, path, text="产品特点：轻便")
    mock = AsyncMock(return_value="产品轻便。[1]")
    monkeypatch.setattr(service.resolver, "resolve_with_fallback", mock)
    response = await client.post(f"{path}/ask", json={"question": "产品特点"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["citations"][0]["page_id"] == source["id"]
    summary = await client.post(f"{path}/pages/{source['id']}/summarize")
    assert summary.status_code == 201, summary.text
    assert summary.json()["data"]["status"] == "draft"
    assert summary.json()["data"]["source_refs"][0]["revision"] == 1
    mock.return_value = "胡乱引用。[999]"
    response = await client.post(f"{path}/ask", json={"question": "产品特点"})
    assert "胡乱引用" not in response.json()["data"]["answer"]
    assert (await client.post(f"{path}/pages/{source['id']}/summarize")).status_code == 502


async def test_empty_search_does_not_call_model_and_no_model_is_explicit(
    client, knowledge, monkeypatch
):
    mock = AsyncMock(side_effect=service.ResolverError("unconfigured"))
    monkeypatch.setattr(service.resolver, "resolve_with_fallback", mock)
    path = knowledge["path"]
    assert (await client.post(f"{path}/ask", json={"question": "未知"})).status_code == 200
    mock.assert_not_called()
    await upload(client, path)
    assert (await client.post(f"{path}/ask", json={"question": "产品特点"})).status_code == 503
    assert (await client.get(f"{path}/search", params={"q": "特点"})).status_code == 200


@pytest.mark.parametrize(
    "name,data,status",
    [
        ("a.pdf", b"pdf", 415),
        ("a.md", b"\xff", 422),
        ("a.md", b"", 413),
        ("a.docx", b"broken", 422),
        ("a.txt", b"a" * (service.MAX_UPLOAD + 1), 413),
    ],
    ids=["unsupported", "encoding", "empty", "broken-docx", "too-large"],
)
def test_upload_validation(name, data, status):
    with pytest.raises(HTTPException) as error:
        service.extract_upload(name, data)
    assert error.value.status_code == status


def test_docx_extraction_and_entity_rejection():
    def docx(xml):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return buffer.getvalue()

    data = docx(
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>产品特点</w:t></w:r></w:p></w:body></w:document>'
    )
    assert service.extract_upload("a.docx", data)[1] == "产品特点"
    with pytest.raises(HTTPException):
        service.extract_upload("a.docx", docx('<!DOCTYPE foo [<!ENTITY x "bad">]><a>&x;</a>'))


async def test_product_classification_filters_records_and_can_be_reversed(
    client, db_session, knowledge
):
    k = knowledge
    db_session.add(StudioWorkspace(id=k["workspace"], name="Studio", slug="studio"))
    source = DataSource(name="Source", channel_type="rss", channel_config={})
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id)
    db_session.add(task)
    await db_session.flush()
    projects = []
    for i in range(3):
        project = StudioProject(
            workspace_id=k["workspace"],
            name=f"Project {i}",
            slug=f"p{i}",
            created_by_user_id="test",
        )
        db_session.add(project)
        await db_session.flush()
        workflow = StudioWorkflow(project_id=project.id, name="workflow")
        db_session.add(workflow)
        await db_session.flush()
        db_session.add(
            CollectedRecord(
                task_id=task.id,
                source_id=source.id,
                workflow_id=workflow.id,
                content_hash=str(i),
                raw_data={"title": "特点"},
            )
        )
        projects.append(project)
    await db_session.flush()
    for project, product_id in zip(projects, (k["a"], k["b"]), strict=False):
        response = await client.put(
            f"{k['path']}/projects/{project.id}", json={"product_id": product_id}
        )
        assert response.status_code == 200, response.text
    params = {"workspace_id": k["workspace"], "brand_id": k["brand"], "product_id": k["a"]}
    result = await client.get("/api/v1/records", params=params)
    assert result.status_code == 200, result.text
    assert result.json()["meta"]["total"] == 1
    assert result.json()["data"][0]["content_hash"] == "0"
    result = await client.get(
        "/api/v1/records", params={"workspace_id": k["workspace"], "unclassified": True}
    )
    assert result.json()["meta"]["total"] == 1
    assert (await client.delete(f"{k['path']}/projects/{projects[0].id}")).status_code == 200
    assert (await client.get("/api/v1/records", params=params)).json()["meta"]["total"] == 0
    assert (await client.get("/api/v1/records", params={"brand_id": k["brand"]})).status_code == 422
    assert (
        await client.get("/api/v1/records", params={"workspace_id": k["other"]})
    ).status_code == 403
