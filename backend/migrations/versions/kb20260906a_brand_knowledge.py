"""Add brands, product classification and versioned knowledge pages."""

import sqlalchemy as sa
from alembic import op

revision = "kb20260906a"
down_revision = "acc20260905c"
branch_labels = None
depends_on = None


def _base():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table(
        "brands",
        *_base(),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.UniqueConstraint("workspace_id", "name"),
    )
    op.create_table(
        "brand_products",
        *_base(),
        sa.Column("brand_id", sa.String(36), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.UniqueConstraint("brand_id", "name"),
    )
    op.create_table(
        "brand_project_scopes",
        *_base(),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("studio_projects.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("brand_id", sa.String(36), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("brand_products.id")),
    )
    op.create_table(
        "knowledge_pages",
        *_base(),
        sa.Column("brand_id", sa.String(36), sa.ForeignKey("brands.id"), nullable=False),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("brand_products.id")),
        sa.Column("parent_id", sa.String(36), sa.ForeignKey("knowledge_pages.id")),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("original_name", sa.String(255)),
        sa.Column("original_bytes", sa.LargeBinary()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("upload_key", sa.String(64), unique=True),
        sa.Column("created_by", sa.String(100), nullable=False),
    )
    op.create_table(
        "knowledge_revisions",
        *_base(),
        sa.Column("page_id", sa.String(36), sa.ForeignKey("knowledge_pages.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.UniqueConstraint("page_id", "revision"),
    )
    for table, columns in {
        "brands": ["workspace_id"],
        "brand_products": ["brand_id"],
        "brand_project_scopes": ["brand_id", "product_id"],
        "knowledge_pages": ["brand_id", "product_id", "parent_id", "content_hash"],
        "knowledge_revisions": ["page_id"],
    }.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in (
        "knowledge_revisions",
        "knowledge_pages",
        "brand_project_scopes",
        "brand_products",
        "brands",
    ):
        op.drop_table(table)
