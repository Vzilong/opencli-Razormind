"""Service tests for project scope, safe output, and retrieval-only behavior."""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from backend.models.agent_run import AgentRun, AgentSession
from backend.models.brand_knowledge import (
    Brand,
    BrandProduct,
    BrandProjectScope,
    KnowledgeLibrary,
    KnowledgePage,
    ProjectKnowledgeBinding,
)
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.record import CollectedRecord
from backend.models.source import DataSource
from backend.models.studio import StudioProject, StudioWorkflow, StudioWorkspace
from backend.models.task import CollectionTask
from backend.security.identity import RequestIdentity
from backend.services import agent_data_service


async def _governed_project(db_session):
    user = User(subject="member", email="member@example.test")
    workspace = Workspace(name="Governed", slug="agent-data-governed")
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
                name="Studio mirror",
                slug="agent-data-studio-mirror",
            ),
        ]
    )
    await db_session.flush()
    project = StudioProject(
        workspace_id=workspace.id,
        name="Project",
        slug="agent-data-project",
        created_by_user_id=user.id,
    )
    other_project = StudioProject(
        workspace_id=workspace.id,
        name="Other project",
        slug="agent-data-other-project",
        created_by_user_id=user.id,
    )
    db_session.add_all([project, other_project])
    await db_session.flush()
    workflow = StudioWorkflow(project_id=project.id, name="Project workflow")
    other_workflow = StudioWorkflow(project_id=other_project.id, name="Other workflow")
    db_session.add_all([workflow, other_workflow])
    await db_session.flush()
    return user, workspace, project, other_project, workflow, other_workflow


async def _record(db_session, workflow_id, *, status="normalized", suffix="one"):
    source = DataSource(
        name=f"Source {suffix}",
        channel_type="rss",
        channel_config={"feed_url": f"https://{suffix}.example/feed", "api_key": "stored"},
    )
    db_session.add(source)
    await db_session.flush()
    task = CollectionTask(source_id=source.id, trigger_type="manual", parameters={})
    db_session.add(task)
    await db_session.flush()
    record = CollectedRecord(
        task_id=task.id,
        source_id=source.id,
        workflow_id=workflow_id,
        workflow_run_id=f"run-{suffix}",
        lineage={"source_url": f"https://{suffix}.example", "authorization": "hidden"},
        raw_data={"secret": "raw-hidden"},
        normalized_data={
            "title": f"Search design {suffix}",
            "content": "Use a project-scoped index with citations.",
            "url": f"https://{suffix}.example/doc",
            "api_key": "hidden",
            "nested": {
                "access-token": "hidden",
                "clientSecret": "hidden",
                "credentials": {"username": "hidden"},
                "safe": "visible",
            },
        },
        ai_enrichment={"private_key": "hidden"},
        content_hash=f"hash-{suffix}",
        status=status,
    )
    db_session.add(record)
    await db_session.flush()
    return record


async def _research_run(
    db_session,
    *,
    storage_workspace_id: str,
    studio_workspace_id: str,
    project_id: str,
    status: str,
    result: dict,
    suffix: str,
):
    session = AgentSession(
        workspace_id=storage_workspace_id,
        actor_subject="member",
        context={"studio_workspace_id": studio_workspace_id, "project_id": project_id},
    )
    run = AgentRun(
        session=session,
        kind="research",
        status=status,
        goal=f"Research {suffix}",
        request_payload={
            "template_id": "research-brief",
            "question": f"Question {suffix}",
            "request_id": f"request-{suffix}",
        },
        reply_payload={"result": result},
    )
    db_session.add(run)
    await db_session.flush()
    return run


@pytest.mark.asyncio
async def test_records_are_exact_project_normalized_and_credential_safe(db_session):
    _, workspace, project, _, workflow, other_workflow = await _governed_project(db_session)
    included = await _record(db_session, workflow.id, suffix="included")
    await _record(db_session, workflow.id, status="raw", suffix="raw")
    await _record(db_session, other_workflow.id, suffix="other")

    result = await agent_data_service.list_project_records(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
    )

    assert result.total == 1
    assert [item.id for item in result.items] == [included.id]
    item = result.items[0]
    assert item.version == "hash-included"
    assert item.data["nested"] == {"safe": "visible"}
    assert "api_key" not in item.data
    assert "authorization" not in item.source["lineage"]
    assert "raw_data" not in item.model_dump()
    assert "ai_enrichment" not in item.model_dump()


@pytest.mark.asyncio
async def test_record_search_cap_is_query_independent_and_reported(db_session, monkeypatch):
    _, workspace, project, _, workflow, _ = await _governed_project(db_session)
    for suffix in ("first", "second", "third"):
        await _record(db_session, workflow.id, suffix=suffix)
    monkeypatch.setattr(agent_data_service, "_MAX_RECORDS_SCANNED", 2)
    kwargs = dict(
        workspace_id=workspace.id, project_id=project.id, identity=RequestIdentity(subject="member")
    )
    for query, expected in (("hidden", 0), ("absent", 0), ("Search", 2)):
        result = await agent_data_service.list_project_records(db_session, query=query, **kwargs)
        assert result.total == expected
        assert result.truncated is True
        assert result.total_is_exact is False
    context = await agent_data_service.search_project_context(db_session, query="Search", **kwargs)
    assert any("2 most recently" in gap for gap in context.gaps)


def test_record_projection_bounds_deep_large_and_secret_values():
    nested = {"safe": "end"}
    for _ in range(100):
        nested = {"child": nested}
    result, truncated = agent_data_service._bounded_record_value(
        {
            "api_key": "secret-value",
            "large": "x" * 20_000,
            "deep": nested,
            "many": ["z" * 100 for _ in range(500)],
        }
    )
    assert truncated is True
    assert "api_key" not in result
    assert len(result["large"]) == 4_000
    assert len(str(result)) < 20_000


@pytest.mark.asyncio
async def test_context_uses_records_and_only_explicit_project_knowledge(db_session):
    _, workspace, project, _, workflow, _ = await _governed_project(db_session)
    record = await _record(db_session, workflow.id, suffix="context")
    brand = Brand(workspace_id=workspace.id, name="Bound brand", description="")
    other_brand = Brand(workspace_id=workspace.id, name="Unbound brand", description="")
    db_session.add_all([brand, other_brand])
    await db_session.flush()
    library = KnowledgeLibrary(
        workspace_id=workspace.id, name="Bound library", legacy_brand_id=brand.id
    )
    other_library = KnowledgeLibrary(
        workspace_id=workspace.id, name="Other library", legacy_brand_id=other_brand.id
    )
    db_session.add_all([library, other_library])
    await db_session.flush()
    db_session.add(ProjectKnowledgeBinding(project_id=project.id, library_id=library.id))
    db_session.add(BrandProjectScope(project_id=project.id, brand_id=brand.id))
    bound_page = KnowledgePage(
        library_id=library.id,
        brand_id=brand.id,
        title="Search architecture",
        kind="page",
        status="published",
        content="The search architecture uses citations and project boundaries.",
        revision=3,
        source_refs=[{"url": "https://docs.example/search", "token": "hidden"}],
        content_hash="knowledge-hash",
        created_by="member",
    )
    unbound_page = KnowledgePage(
        library_id=other_library.id,
        brand_id=other_brand.id,
        title="Search secret",
        kind="page",
        status="published",
        content="Search information from another brand must stay hidden.",
        revision=1,
        created_by="member",
    )
    db_session.add_all([bound_page, unbound_page])
    await db_session.flush()

    result = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="search",
        limit=8,
    )

    ids = {match.id for match in result.matches}
    assert f"record:{record.id}:hash-context" in ids
    assert any(match.id.startswith(f"knowledge:{bound_page.id}:r3:") for match in result.matches)
    assert all(unbound_page.id not in match.id for match in result.matches)
    knowledge = next(match for match in result.matches if match.source["type"] == "knowledge")
    assert knowledge.source["revision"] == 3
    assert knowledge.source["content_hash"] == "knowledge-hash"
    assert knowledge.source["source_refs"] == [{"url": "https://docs.example/search"}]
    assert result.gaps == []


@pytest.mark.asyncio
async def test_generic_library_query_needs_binding_and_revocation_is_immediate(db_session):
    _, workspace, project, other_project, _, _ = await _governed_project(db_session)
    library = KnowledgeLibrary(workspace_id=workspace.id, name="Engineering notes")
    db_session.add(library)
    await db_session.flush()
    page = KnowledgePage(
        library_id=library.id,
        brand_id=None,
        title="Asyncio source",
        kind="page",
        status="published",
        content="asyncio evidence without a brand",
        revision=1,
        created_by="member",
    )
    binding = ProjectKnowledgeBinding(project_id=project.id, library_id=library.id)
    db_session.add_all([page, binding])
    await db_session.flush()
    kwargs = dict(
        workspace_id=workspace.id, identity=RequestIdentity(subject="member"), query="asyncio"
    )
    result = await agent_data_service.search_project_context(
        db_session, project_id=project.id, **kwargs
    )
    assert any(match.source.get("library_id") == library.id for match in result.matches)
    denied = await agent_data_service.search_project_context(
        db_session, project_id=other_project.id, **kwargs
    )
    assert not denied.matches
    await db_session.delete(binding)
    await db_session.flush()
    revoked = await agent_data_service.search_project_context(
        db_session, project_id=project.id, **kwargs
    )
    assert not revoked.matches
    assert any("no authorized knowledge binding" in gap for gap in revoked.gaps)


@pytest.mark.asyncio
async def test_legacy_product_binding_never_exposes_sibling_or_regrants_after_removal(db_session):
    _, workspace, project, _, _, _ = await _governed_project(db_session)
    brand = Brand(workspace_id=workspace.id, name="Legacy")
    db_session.add(brand)
    await db_session.flush()
    first = BrandProduct(brand_id=brand.id, name="Allowed")
    second = BrandProduct(brand_id=brand.id, name="Sibling")
    library = KnowledgeLibrary(workspace_id=workspace.id, name="Legacy", legacy_brand_id=brand.id)
    db_session.add_all([first, second, library])
    await db_session.flush()
    binding = ProjectKnowledgeBinding(
        project_id=project.id, library_id=library.id, product_id=first.id
    )
    db_session.add_all(
        [binding, BrandProjectScope(project_id=project.id, brand_id=brand.id, product_id=first.id)]
    )
    pages = [
        KnowledgePage(
            library_id=library.id,
            brand_id=brand.id,
            product_id=product_id,
            title=label,
            kind="page",
            status="published",
            content="scope evidence",
            revision=1,
            created_by="member",
        )
        for product_id, label in [(None, "Common"), (first.id, "Allowed"), (second.id, "Sibling")]
    ]
    db_session.add_all(pages)
    await db_session.flush()
    kwargs = dict(
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="scope",
    )
    result = await agent_data_service.search_project_context(db_session, **kwargs)
    assert {match.source["page_id"] for match in result.matches} == {pages[0].id, pages[1].id}
    await db_session.delete(binding)
    await db_session.flush()
    assert not (await agent_data_service.search_project_context(db_session, **kwargs)).matches


@pytest.mark.asyncio
async def test_context_flattens_unicode_nested_record_fields_after_redaction(db_session):
    _, workspace, project, _, workflow, _ = await _governed_project(db_session)
    record = await _record(db_session, workflow.id, suffix="nested")
    record.normalized_data = {
        **record.normalized_data,
        "profile": {
            "display_name": "认证设计负责人",
            "details": {"note": "项目范围内的认证设计"},
            "access_token": "credential-only-needle",
        },
    }
    await db_session.flush()

    result = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="认证设计",
    )

    match = next(item for item in result.matches if item.source["type"] == "record")
    assert match.id == f"record:{record.id}:hash-nested"
    assert "profile.display_name: 认证设计负责人" in match.text
    assert "profile.details.note: 项目范围内的认证设计" in match.text
    assert "credential-only-needle" not in match.text

    sensitive_only = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="credential-only-needle",
    )
    assert all(item.source["type"] != "record" for item in sensitive_only.matches)
    hidden_records = await agent_data_service.list_project_records(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="credential-only-needle",
    )
    assert hidden_records.items == []
    assert hidden_records.total == 0


def test_research_projection_hashes_retained_content_once_per_run(monkeypatch):
    run = AgentRun(
        id="projection-run",
        kind="research",
        status="partial",
        request_payload={"template_id": "research-brief"},
        reply_payload={
            "result": {
                "sources": [
                    {
                        "id": str(index),
                        "status": "fetched",
                        "content": "matching needle",
                        "excerpt": "matching needle",
                        "url": f"https://example.test/{index}",
                    }
                    for index in range(3)
                ],
                "findings": [],
                "gaps": [],
                "changes": [],
            }
        },
    )
    calls = []
    original = agent_data_service._research_version

    def counted(current):
        calls.append(current.id)
        return original(current)

    monkeypatch.setattr(agent_data_service, "_research_version", counted)
    matches, _ = agent_data_service._research_run_matches(run, query="needle")
    assert len(matches) == 3
    assert calls == [run.id]


@pytest.mark.asyncio
async def test_context_retrieves_scoped_research_findings_and_captured_partial_evidence(db_session):
    _, workspace, project, _, _, _ = await _governed_project(db_session)
    content = "OAuth requests require a caller bearer and an independent fleet token."
    completed = await _research_run(
        db_session,
        storage_workspace_id=workspace.id,
        studio_workspace_id=workspace.id,
        project_id=project.id,
        status="completed",
        suffix="completed",
        result={
            "summary": "A model summary is not independently returned as evidence.",
            "findings": [
                {
                    "text": "OAuth uses separate caller and fleet credentials.",
                    "source_ids": ["source-auth"],
                    "evidence": [
                        {
                            "source_id": "source-auth",
                            "quote": "OAuth requests require a caller bearer",
                        }
                    ],
                },
                {
                    "text": "Unsupported assertion must stay hidden.",
                    "source_ids": ["source-auth"],
                    "evidence": [
                        {"source_id": "source-auth", "quote": "not present in retained content"}
                    ],
                },
            ],
            "sources": [
                {
                    "id": "source-auth",
                    "url": "https://docs.example/auth",
                    "title": "Authentication",
                    "fetched_at": "2026-09-14T00:00:00Z",
                    "content_hash": "auth-hash",
                    "excerpt": content,
                    "status": "fetched",
                    "content": content,
                }
            ],
            "gaps": [],
            "changes": [],
        },
    )
    partial = await _research_run(
        db_session,
        storage_workspace_id=workspace.id,
        studio_workspace_id=workspace.id,
        project_id=project.id,
        status="partial",
        suffix="partial",
        result={
            "summary": "Evidence retained.",
            "findings": [],
            "sources": [
                {
                    "id": "source-crawler",
                    "url": "https://docs.example/crawler",
                    "title": "Crawler",
                    "fetched_at": "2026-09-14T00:01:00Z",
                    "content_hash": "crawler-hash",
                    "excerpt": "Bounded crawler evidence",
                    "status": "fetched",
                    "content": "Bounded crawler evidence was captured without model analysis.",
                }
            ],
            "gaps": ["No configured model analyzed the evidence."],
            "changes": [],
        },
    )
    await _research_run(
        db_session,
        storage_workspace_id="other-governed-workspace",
        studio_workspace_id=workspace.id,
        project_id=project.id,
        status="completed",
        suffix="cross-scope",
        result={
            "summary": "Crossscope secret summary.",
            "findings": [],
            "sources": [
                {
                    "id": "source-cross",
                    "content": "crossscope secret evidence",
                    "status": "fetched",
                }
            ],
            "gaps": [],
        },
    )

    analyzed = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="OAuth",
    )
    finding = next(
        item
        for item in analyzed.matches
        if item.source.get("evidence_kind") == "model-analyzed-finding"
    )
    assert finding.source["run_id"] == completed.id
    assert finding.source["status"] == "completed"
    assert len(finding.source["version"]) == 64
    assert finding.source["source_ids"] == ["source-auth"]
    assert finding.source["evidence"] == [
        {"source_id": "source-auth", "quote": "OAuth requests require a caller bearer"}
    ]
    assert finding.source["sources"][0]["content_hash"] == "auth-hash"
    assert "content" not in finding.source["sources"][0]

    captured = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="crawler evidence",
    )
    evidence = next(
        item for item in captured.matches if item.source.get("evidence_kind") == "captured-source"
    )
    assert evidence.source["run_id"] == partial.id
    assert evidence.source["status"] == "partial"
    assert evidence.text.startswith("Captured evidence (not a model-analyzed finding):")
    assert evidence.source["gaps"] == ["No configured model analyzed the evidence."]
    assert captured.gaps[0] == (
        f"Research run {partial.id}: No configured model analyzed the evidence."
    )

    unsupported = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="unsupported assertion",
    )
    assert all(
        item.source.get("evidence_kind") != "model-analyzed-finding" for item in unsupported.matches
    )

    cross_scope = await agent_data_service.search_project_context(
        db_session,
        workspace_id=workspace.id,
        project_id=project.id,
        identity=RequestIdentity(subject="member"),
        query="crossscope secret",
    )
    assert all(item.source.get("type") != "research" for item in cross_scope.matches)


@pytest.mark.asyncio
async def test_context_rejects_blank_query_without_running_any_work(db_session):
    with pytest.raises(HTTPException) as exc:
        await agent_data_service.search_project_context(
            db_session,
            workspace_id="workspace",
            project_id="project",
            identity=RequestIdentity(subject="member"),
            query="   ",
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_outsider_and_cross_project_access_fail_closed(db_session):
    _, workspace, project, other_project, _, _ = await _governed_project(db_session)

    with pytest.raises(HTTPException) as outsider:
        await agent_data_service.list_project_records(
            db_session,
            workspace_id=workspace.id,
            project_id=project.id,
            identity=RequestIdentity(subject="outsider"),
        )
    assert outsider.value.status_code == 403

    second_workspace = Workspace(name="Second", slug="agent-data-second")
    db_session.add(second_workspace)
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(
            workspace_id=second_workspace.id,
            user_id=(await db_session.scalar(select(User).where(User.subject == "member"))).id,
            role=WorkspaceRole.VIEWER,
        )
    )
    await db_session.flush()
    with pytest.raises(HTTPException) as cross_project:
        await agent_data_service.list_project_records(
            db_session,
            workspace_id=second_workspace.id,
            project_id=other_project.id,
            identity=RequestIdentity(subject="member"),
        )
    assert cross_project.value.status_code == 404


@pytest.mark.asyncio
async def test_legacy_studio_bridge_is_local_admin_only(db_session):
    user = User(subject="local-admin")
    governed = Workspace(name="Local governed", slug="agent-data-local-governed")
    studio = StudioWorkspace(name="Legacy Studio", slug="agent-data-legacy-studio")
    db_session.add_all([user, governed, studio])
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(
            workspace_id=governed.id,
            user_id=user.id,
            role=WorkspaceRole.ADMIN,
        )
    )
    project = StudioProject(
        workspace_id=studio.id,
        name="Legacy project",
        slug="agent-data-legacy-project",
        created_by_user_id=user.id,
    )
    db_session.add(project)
    await db_session.flush()

    scope = await agent_data_service.authorize_project(
        db_session,
        workspace_id=studio.id,
        project_id=project.id,
        identity=RequestIdentity(
            subject="local-admin",
            is_platform_admin=True,
            auth_method="local",
        ),
    )
    assert scope.governed_workspace_id == governed.id

    brand = Brand(workspace_id=governed.id, name="Legacy bound brand", description="")
    db_session.add(brand)
    await db_session.flush()
    library = KnowledgeLibrary(
        workspace_id=governed.id, name="Legacy library", legacy_brand_id=brand.id
    )
    db_session.add(library)
    await db_session.flush()
    db_session.add(ProjectKnowledgeBinding(project_id=project.id, library_id=library.id))
    page = KnowledgePage(
        library_id=library.id,
        brand_id=brand.id,
        title="Legacy research",
        kind="page",
        status="published",
        content="The legacy project keeps research in its governed workspace.",
        revision=1,
        content_hash="legacy-knowledge",
        created_by=user.subject,
    )
    db_session.add_all([BrandProjectScope(project_id=project.id, brand_id=brand.id), page])
    await db_session.flush()

    context = await agent_data_service.search_project_context(
        db_session,
        workspace_id=studio.id,
        project_id=project.id,
        identity=RequestIdentity(
            subject="local-admin",
            is_platform_admin=True,
            auth_method="local",
        ),
        query="research",
    )
    assert any(match.source.get("page_id") == page.id for match in context.matches)

    research_run = await _research_run(
        db_session,
        storage_workspace_id=governed.id,
        studio_workspace_id=studio.id,
        project_id=project.id,
        status="partial",
        suffix="legacy-bridge",
        result={
            "summary": "Evidence retained.",
            "findings": [],
            "sources": [
                {
                    "id": "legacy-source",
                    "url": "https://docs.example/legacy",
                    "status": "fetched",
                    "content": "Captured legacy evidence belongs to the authorized bridge.",
                }
            ],
            "gaps": ["No configured model analyzed the evidence."],
        },
    )
    bridged_research = await agent_data_service.search_project_context(
        db_session,
        workspace_id=studio.id,
        project_id=project.id,
        identity=RequestIdentity(
            subject="local-admin",
            is_platform_admin=True,
            auth_method="local",
        ),
        query="legacy evidence",
    )
    assert any(match.source.get("run_id") == research_run.id for match in bridged_research.matches)

    with pytest.raises(HTTPException) as oidc_admin:
        await agent_data_service.authorize_project(
            db_session,
            workspace_id=studio.id,
            project_id=project.id,
            identity=RequestIdentity(
                subject="local-admin",
                is_platform_admin=True,
                auth_method="oidc",
            ),
        )
    assert oidc_admin.value.status_code == 403
