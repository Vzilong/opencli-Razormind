"""Business classification and cited knowledge, owned by governed workspaces."""

from sqlalchemy import JSON, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from backend.models.base import TimestampMixin


class Brand(TimestampMixin):
    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")


class BrandProduct(TimestampMixin):
    __tablename__ = "brand_products"
    __table_args__ = (UniqueConstraint("brand_id", "name"),)

    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")


class BrandProjectScope(TimestampMixin):
    __tablename__ = "brand_project_scopes"

    project_id: Mapped[str] = mapped_column(
        ForeignKey("studio_projects.id", ondelete="CASCADE"), unique=True
    )
    brand_id: Mapped[str] = mapped_column(ForeignKey("brands.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("brand_products.id"), index=True)


class KnowledgeLibrary(TimestampMixin):
    """A workspace-owned knowledge container.

    ``legacy_brand_id`` is populated only for the compatibility library created
    for an existing Brand.  New libraries deliberately have no Brand row.
    """

    __tablename__ = "knowledge_libraries"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    legacy_brand_id: Mapped[str | None] = mapped_column(
        ForeignKey("brands.id"), unique=True, index=True
    )


class ProjectKnowledgeBinding(TimestampMixin):
    """An explicit project grant for a knowledge library.

    This is intentionally separate from ``BrandProjectScope``: callers of the
    new model must never infer a grant from the legacy table after a binding was
    removed.
    """

    __tablename__ = "project_knowledge_bindings"
    __table_args__ = (UniqueConstraint("project_id", "library_id"),)

    project_id: Mapped[str] = mapped_column(
        ForeignKey("studio_projects.id", ondelete="CASCADE"), index=True
    )
    library_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_libraries.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[str | None] = mapped_column(ForeignKey("brand_products.id"), index=True)


class KnowledgePage(TimestampMixin):
    __tablename__ = "knowledge_pages"

    library_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_libraries.id"), index=True
    )
    # Brand remains for legacy URLs; generic pages intentionally have no Brand.
    brand_id: Mapped[str | None] = mapped_column(ForeignKey("brands.id"), index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("brand_products.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_pages.id"), index=True)
    title: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16))  # folder | source | page
    status: Mapped[str] = mapped_column(String(16), default="draft")
    content: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    original_name: Mapped[str | None] = mapped_column(String(255))
    original_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    upload_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_by: Mapped[str] = mapped_column(String(100))


class KnowledgeRevision(TimestampMixin):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (UniqueConstraint("page_id", "revision"),)

    page_id: Mapped[str] = mapped_column(ForeignKey("knowledge_pages.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(100))
