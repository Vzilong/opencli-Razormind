"""Add brand-optional workspace knowledge libraries and explicit project bindings.

Revision ID: kb20260914a
Revises: kb20260906a
"""

import uuid

import sqlalchemy as sa
from alembic import op

revision = "kb20260914a"
down_revision = "kb20260906a"
branch_labels = None
depends_on = None


def _base():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "knowledge_libraries",
        *_base(),
        sa.Column("workspace_id", sa.String(36), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("legacy_brand_id", sa.String(36), sa.ForeignKey("brands.id"), unique=True),
    )
    op.create_index("ix_knowledge_libraries_workspace_id", "knowledge_libraries", ["workspace_id"])
    op.create_index(
        "ix_knowledge_libraries_legacy_brand_id", "knowledge_libraries", ["legacy_brand_id"]
    )

    op.create_table(
        "project_knowledge_bindings",
        *_base(),
        sa.Column(
            "project_id",
            sa.String(36),
            sa.ForeignKey("studio_projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "library_id",
            sa.String(36),
            sa.ForeignKey("knowledge_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("product_id", sa.String(36), sa.ForeignKey("brand_products.id")),
        sa.UniqueConstraint("project_id", "library_id"),
    )
    for column in ("project_id", "library_id", "product_id"):
        op.create_index(
            f"ix_project_knowledge_bindings_{column}", "project_knowledge_bindings", [column]
        )

    with op.batch_alter_table("knowledge_pages") as batch:
        batch.add_column(sa.Column("library_id", sa.String(36), nullable=True))
        batch.create_foreign_key(
            "fk_knowledge_pages_library_id", "knowledge_libraries", ["library_id"], ["id"]
        )
        batch.alter_column("brand_id", existing_type=sa.String(36), nullable=True)
    op.create_index("ix_knowledge_pages_library_id", "knowledge_pages", ["library_id"])

    bind = op.get_bind()
    brands = list(
        bind.execute(sa.text("SELECT id, workspace_id, name, description FROM brands")).mappings()
    )
    libraries = [
        {
            "id": str(uuid.uuid4()),
            "workspace_id": brand["workspace_id"],
            "name": brand["name"],
            "description": brand["description"],
            "legacy_brand_id": brand["id"],
        }
        for brand in brands
    ]
    if libraries:
        bind.execute(
            sa.text(
                "INSERT INTO knowledge_libraries "
                "(id, created_at, updated_at, workspace_id, name, description, legacy_brand_id) "
                "VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :workspace_id, :name, "
                ":description, :legacy_brand_id)"
            ),
            libraries,
        )
        bind.execute(
            sa.text(
                "UPDATE knowledge_pages SET library_id = ("
                "SELECT id FROM knowledge_libraries "
                "WHERE legacy_brand_id = knowledge_pages.brand_id)"
            )
        )
        library_by_brand = {library["legacy_brand_id"]: library["id"] for library in libraries}
        scopes = list(
            bind.execute(
                sa.text("SELECT project_id, brand_id, product_id FROM brand_project_scopes")
            ).mappings()
        )
        bindings = [
            {
                "id": str(uuid.uuid4()),
                "project_id": scope["project_id"],
                "library_id": library_by_brand[scope["brand_id"]],
                "product_id": scope["product_id"],
            }
            for scope in scopes
            if scope["brand_id"] in library_by_brand
        ]
        if bindings:
            bind.execute(
                sa.text(
                    "INSERT INTO project_knowledge_bindings "
                    "(id, created_at, updated_at, project_id, library_id, product_id) "
                    "VALUES (:id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, :project_id, "
                    ":library_id, :product_id)"
                ),
                bindings,
            )


def downgrade() -> None:
    bind = op.get_bind()
    generic_library = bind.execute(
        sa.text("SELECT 1 FROM knowledge_libraries WHERE legacy_brand_id IS NULL LIMIT 1")
    ).first()
    generic_page = bind.execute(
        sa.text("SELECT 1 FROM knowledge_pages WHERE brand_id IS NULL LIMIT 1")
    ).first()
    binding_mismatch = bind.execute(
        sa.text(
            "SELECT 1 FROM project_knowledge_bindings AS binding "
            "JOIN knowledge_libraries AS library ON library.id = binding.library_id "
            "LEFT JOIN brand_project_scopes AS scope "
            "ON scope.project_id = binding.project_id "
            "AND scope.brand_id = library.legacy_brand_id "
            "AND (scope.product_id = binding.product_id "
            "OR (scope.product_id IS NULL AND binding.product_id IS NULL)) "
            "WHERE scope.id IS NULL LIMIT 1"
        )
    ).first()
    scope_mismatch = bind.execute(
        sa.text(
            "SELECT 1 FROM brand_project_scopes AS scope "
            "LEFT JOIN knowledge_libraries AS library ON library.legacy_brand_id = scope.brand_id "
            "LEFT JOIN project_knowledge_bindings AS binding "
            "ON binding.project_id = scope.project_id AND binding.library_id = library.id "
            "AND (binding.product_id = scope.product_id "
            "OR (binding.product_id IS NULL AND scope.product_id IS NULL)) "
            "WHERE binding.id IS NULL LIMIT 1"
        )
    ).first()
    if generic_library or generic_page or binding_mismatch or scope_mismatch:
        raise RuntimeError(
            "Cannot downgrade general knowledge libraries while it would discard generic data "
            "or restore a removed project grant. Export or reconcile the data first."
        )

    op.drop_index("ix_knowledge_pages_library_id", table_name="knowledge_pages")
    with op.batch_alter_table("knowledge_pages") as batch:
        batch.drop_constraint("fk_knowledge_pages_library_id", type_="foreignkey")
        batch.drop_column("library_id")
        batch.alter_column("brand_id", existing_type=sa.String(36), nullable=False)
    op.drop_table("project_knowledge_bindings")
    op.drop_table("knowledge_libraries")
