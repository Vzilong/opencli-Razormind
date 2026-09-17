"""Brand knowledge uses the existing identity, workspace RBAC and model gateway."""

import hashlib
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, UploadFile
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from starlette.concurrency import run_in_threadpool

from backend.database import get_db
from backend.models.brand_knowledge import (
    Brand,
    BrandProduct,
    BrandProjectScope,
    KnowledgePage,
    KnowledgeRevision,
)
from backend.models.studio import StudioProject
from backend.schemas.brand_knowledge import (
    KnowledgeQuestion,
    NamedItem,
    PageCreate,
    PageUpdate,
    ProjectClassification,
)
from backend.schemas.common import ApiResponse
from backend.security.identity import RequestIdentity, get_request_identity
from backend.security.workspace_rbac import (
    WorkspacePermission,
    get_workspace_access,
    require_permission,
)
from backend.services import brand_knowledge_service as service

router = APIRouter(prefix="/workspaces/{workspace_id}/brands", tags=["brand-knowledge"])


async def read_access(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
    identity: RequestIdentity = Depends(get_request_identity),
):
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    return access


async def write_access(access=Depends(read_access)):
    require_permission(access, WorkspacePermission.MANAGE_CONFIGURATION)
    return access


async def unique_flush(db):
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(409, "名称或项目分类已存在，请刷新后重试") from exc


def named_read(row):
    return {"id": row.id, "name": row.name, "description": row.description}


@router.get("")
async def list_brands(
    workspace_id: str, db: AsyncSession = Depends(get_db), access=Depends(read_access)
):
    rows = await db.scalars(
        select(Brand).where(Brand.workspace_id == workspace_id).order_by(Brand.name)
    )
    return ApiResponse.ok([named_read(row) for row in rows])


@router.post("", status_code=201)
async def create_brand(
    workspace_id: str,
    body: NamedItem,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    row = Brand(workspace_id=workspace_id, **body.model_dump())
    db.add(row)
    await unique_flush(db)
    await service.legacy_library(db, row)
    return ApiResponse.ok(named_read(row))


@router.get("/{brand_id}/products")
async def list_products(
    workspace_id: str,
    brand_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    rows = await db.scalars(
        select(BrandProduct).where(BrandProduct.brand_id == brand_id).order_by(BrandProduct.name)
    )
    return ApiResponse.ok([named_read(row) for row in rows])


@router.post("/{brand_id}/products", status_code=201)
async def create_product(
    workspace_id: str,
    brand_id: str,
    body: NamedItem,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    row = BrandProduct(brand_id=brand_id, **body.model_dump())
    db.add(row)
    await unique_flush(db)
    return ApiResponse.ok(named_read(row))


@router.get("/{brand_id}/projects")
async def project_scopes(
    workspace_id: str,
    brand_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    rows = await db.execute(
        select(StudioProject, BrandProjectScope)
        .outerjoin(BrandProjectScope, BrandProjectScope.project_id == StudioProject.id)
        .where(StudioProject.workspace_id == workspace_id, StudioProject.archived.is_(False))
    )
    return ApiResponse.ok(
        [
            {
                "id": project.id,
                "name": project.name,
                "brand_id": scope.brand_id if scope else None,
                "product_id": scope.product_id if scope else None,
            }
            for project, scope in rows
        ]
    )


@router.put("/{brand_id}/projects/{project_id}")
async def classify_project(
    workspace_id: str,
    brand_id: str,
    project_id: str,
    body: ProjectClassification,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id, body.product_id)
    project = await db.get(StudioProject, project_id)
    if project is None or project.workspace_id != workspace_id or project.archived:
        raise HTTPException(404, "项目不存在")
    brand = await service.brand_scope(db, workspace_id, brand_id)
    library = await service.legacy_library(db, brand)
    await service.bind_legacy_library(db, project_id, library, body.product_id)
    await unique_flush(db)
    return ApiResponse.ok(
        {"project_id": project_id, "brand_id": brand_id, "product_id": body.product_id}
    )


@router.delete("/{brand_id}/projects/{project_id}")
async def unclassify_project(
    workspace_id: str,
    brand_id: str,
    project_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    scope = await db.scalar(
        select(BrandProjectScope)
        .join(StudioProject, StudioProject.id == BrandProjectScope.project_id)
        .where(
            BrandProjectScope.project_id == project_id,
            BrandProjectScope.brand_id == brand_id,
            StudioProject.workspace_id == workspace_id,
        )
    )
    if scope is None:
        raise HTTPException(404, "项目分类不存在")
    brand = await service.brand_scope(db, workspace_id, brand_id)
    library = await service.legacy_library(db, brand)
    await service.unbind_legacy_library(db, project_id, library)
    return ApiResponse.ok(None)


@router.get("/{brand_id}/pages")
async def list_pages(
    workspace_id: str,
    brand_id: str,
    product_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id, product_id)
    stmt = (
        select(KnowledgePage)
        .options(defer(KnowledgePage.original_bytes), defer(KnowledgePage.content))
        .where(KnowledgePage.brand_id == brand_id)
    )
    if product_id:
        stmt = stmt.where(service.product_scope(product_id))
    rows = await db.scalars(stmt.order_by(KnowledgePage.created_at))
    return ApiResponse.ok([service.page_read(row) for row in rows])


@router.post("/{brand_id}/pages", status_code=201)
async def create_page(
    workspace_id: str,
    brand_id: str,
    body: PageCreate,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id, body.product_id)
    await service.validate_parent(db, brand_id, body.product_id, body.parent_id)
    brand = await service.brand_scope(db, workspace_id, brand_id)
    library = await service.legacy_library(db, brand)
    page = KnowledgePage(
        brand_id=brand_id,
        library_id=library.id,
        **body.model_dump(),
        created_by=access.user_id,
        status="published" if body.kind == "folder" else "draft",
    )
    db.add(page)
    await service.save_revision(db, page, access.user_id)
    return ApiResponse.ok(service.page_read(page, full=True))


@router.post("/{brand_id}/upload", status_code=201)
async def upload(
    workspace_id: str,
    brand_id: str,
    file: UploadFile,
    product_id: str | None = None,
    parent_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id, product_id)
    await service.validate_parent(db, brand_id, product_id, parent_id)
    brand = await service.brand_scope(db, workspace_id, brand_id)
    library = await service.legacy_library(db, brand)
    try:
        data = await file.read(service.MAX_UPLOAD + 1)
    finally:
        await file.close()
    name, content, digest = await run_in_threadpool(
        service.extract_upload, file.filename or "", data
    )
    upload_key = hashlib.sha256(f"{library.id}:{product_id or ''}:{digest}".encode()).hexdigest()
    existing = await db.scalar(
        select(KnowledgePage)
        .where(
            KnowledgePage.brand_id == brand_id,
            KnowledgePage.product_id == product_id,
            KnowledgePage.content_hash == digest,
        )
        .options(defer(KnowledgePage.original_bytes))
    )
    if existing:
        return reused_upload(existing, parent_id)
    page = KnowledgePage(
        brand_id=brand_id,
        library_id=library.id,
        product_id=product_id,
        parent_id=parent_id,
        title=name,
        kind="source",
        status="published",
        content=content,
        original_name=name,
        original_bytes=data,
        content_hash=digest,
        upload_key=upload_key,
        created_by=access.user_id,
    )
    try:
        async with db.begin_nested():
            db.add(page)
            await service.save_revision(db, page, access.user_id)
    except IntegrityError:
        existing = await db.scalar(
            select(KnowledgePage).where(KnowledgePage.upload_key == upload_key)
        )
        if existing is None:
            raise
        return reused_upload(existing, parent_id)
    return ApiResponse.ok(service.page_read(page, full=True))


def reused_upload(page, parent_id):
    if page.status == "archived":
        raise HTTPException(409, "相同资料已归档，请开启“显示已归档”并恢复原资料")
    if page.parent_id != parent_id:
        raise HTTPException(409, f"相同资料已存在于其他目录：{page.title}，请打开已有页面")
    return ApiResponse.ok({**service.page_read(page, full=True), "reused": True})


@router.get("/{brand_id}/search")
async def search(
    workspace_id: str,
    brand_id: str,
    q: str = Query(min_length=1, max_length=1000),
    product_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id, product_id)
    return ApiResponse.ok(await service.search_knowledge(db, brand_id, product_id, q))


@router.post("/{brand_id}/ask")
async def ask(
    workspace_id: str,
    brand_id: str,
    body: KnowledgeQuestion,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id, body.product_id)
    hits = await service.search_knowledge(db, brand_id, body.product_id, body.question)
    return ApiResponse.ok(await service.grounded_answer(db, body.question, hits))


@router.get("/{brand_id}/pages/{page_id}")
async def read_page(
    workspace_id: str,
    brand_id: str,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    return ApiResponse.ok(
        service.page_read(await service.get_page(db, brand_id, page_id), full=True)
    )


@router.patch("/{brand_id}/pages/{page_id}")
async def edit_page(
    workspace_id: str,
    brand_id: str,
    page_id: str,
    body: PageUpdate,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    page = await service.get_page(db, brand_id, page_id)
    if page.kind == "source" and (body.content != page.content or body.title != page.title):
        raise HTTPException(422, "原始资料不可改写，请新建整理页面或上传新文件")
    if page.kind == "folder" and body.status == "archived":
        child = await db.scalar(
            select(KnowledgePage.id)
            .where(KnowledgePage.parent_id == page.id, KnowledgePage.status != "archived")
            .limit(1)
        )
        if child:
            raise HTTPException(409, "请先归档目录中的页面")
    if page.kind == "page" and body.status == "published" and not body.content.strip():
        raise HTTPException(422, "空页面不能发布")
    result = await db.execute(
        update(KnowledgePage)
        .where(KnowledgePage.id == page_id, KnowledgePage.revision == body.revision)
        .values(
            title=body.title, content=body.content, status=body.status, revision=body.revision + 1
        )
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "页面已被更新，请重新打开后再编辑")
    await db.refresh(page, attribute_names=["title", "content", "status", "revision", "updated_at"])
    await service.save_revision(db, page, access.user_id)
    return ApiResponse.ok(service.page_read(page, full=True))


@router.get("/{brand_id}/pages/{page_id}/revisions")
async def revisions(
    workspace_id: str,
    brand_id: str,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    await service.get_page(db, brand_id, page_id)
    rows = await db.scalars(
        select(KnowledgeRevision)
        .where(KnowledgeRevision.page_id == page_id)
        .order_by(KnowledgeRevision.revision.desc())
        .limit(50)
    )
    return ApiResponse.ok(
        [
            {
                "revision": row.revision,
                "title": row.title,
                "content": row.content,
                "status": row.status,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    )


@router.post("/{brand_id}/pages/{page_id}/summarize", status_code=201)
async def summarize(
    workspace_id: str,
    brand_id: str,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(write_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    source = await service.get_page(db, brand_id, page_id)
    if source.kind == "folder" or source.status != "published":
        raise HTTPException(422, "只能整理已发布资料")
    if len(source.content) > 16_000:
        raise HTTPException(422, "单次 AI 整理支持 1.6 万字，请先分拆资料，避免遗漏内容")
    product = await db.get(BrandProduct, source.product_id) if source.product_id else None
    result = await service.grounded_answer(
        db,
        "整理这份资料：提炼关键事实、适用产品、注意事项和待核实问题。每个事实保留来源编号。",
        [
            {
                "page_id": source.id,
                "title": source.title,
                "revision": source.revision,
                "product_id": source.product_id,
                "product_name": product.name if product else None,
                "kind": source.kind,
                "excerpt": source.content,
            }
        ],
    )
    if result["answer"].startswith("模型未返回有效出处"):
        raise HTTPException(502, "模型未提供有效出处，未生成整理草稿")
    brand = await service.brand_scope(db, workspace_id, brand_id)
    library = await service.legacy_library(db, brand)
    page = KnowledgePage(
        brand_id=brand_id,
        library_id=library.id,
        product_id=source.product_id,
        parent_id=source.parent_id,
        title=f"整理：{source.title}"[:255],
        kind="page",
        status="draft",
        content=result["answer"],
        source_refs=result["citations"],
        created_by=access.user_id,
    )
    db.add(page)
    await service.save_revision(db, page, access.user_id)
    return ApiResponse.ok(service.page_read(page, full=True))


@router.get("/{brand_id}/pages/{page_id}/download")
async def download(
    workspace_id: str,
    brand_id: str,
    page_id: str,
    db: AsyncSession = Depends(get_db),
    access=Depends(read_access),
):
    await service.brand_scope(db, workspace_id, brand_id)
    page = await service.get_page(db, brand_id, page_id)
    if not page.original_name:
        raise HTTPException(404, "该页面没有原始附件")
    data = await db.scalar(select(KnowledgePage.original_bytes).where(KnowledgePage.id == page_id))
    return Response(
        data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": (
                f"attachment; filename*=UTF-8''{quote(page.original_name, safe='')}"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )
