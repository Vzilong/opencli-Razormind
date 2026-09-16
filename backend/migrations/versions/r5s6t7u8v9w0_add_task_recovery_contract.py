"""add governed task recovery contract

Revision ID: r5s6t7u8v9w0
Revises: q4r5s6t7u8v9
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import context, op

revision = "r5s6t7u8v9w0"
down_revision = "q4r5s6t7u8v9"
branch_labels = None
depends_on = None


def _collection_tasks_table(*, include_recovery_columns: bool) -> sa.Table:
    metadata = sa.MetaData()
    columns = [
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("trigger_type", sa.String(length=50), nullable=False),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]
    if include_recovery_columns:
        columns.extend(
            [
                sa.Column("retry_of_task_id", sa.String(length=36), nullable=True),
                sa.Column("recovery_mode", sa.String(length=32), nullable=True),
                sa.Column("recovery_reason", sa.Text(), nullable=True),
                sa.Column("initiating_actor", sa.String(length=255), nullable=True),
                sa.Column(
                    "recovery_idempotency_key",
                    sa.String(length=255),
                    nullable=True,
                ),
            ]
        )
    constraints = [
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["agent_id"], ["ai_agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    ]
    if include_recovery_columns:
        constraints.extend(
            [
                sa.ForeignKeyConstraint(
                    ["retry_of_task_id"],
                    ["collection_tasks.id"],
                    name="fk_collection_tasks_retry_of_task_id",
                    ondelete="SET NULL",
                ),
                sa.UniqueConstraint(
                    "recovery_idempotency_key",
                    name="uq_collection_tasks_recovery_idempotency_key",
                ),
            ]
        )
    return sa.Table("collection_tasks", metadata, *columns, *constraints)


def _batch_kwargs(*, include_recovery_columns: bool) -> dict:
    if context.is_offline_mode():
        return {
            "copy_from": _collection_tasks_table(
                include_recovery_columns=include_recovery_columns
            )
        }
    return {}


def upgrade() -> None:
    if (
        not context.is_offline_mode()
        and not sa.inspect(op.get_bind()).has_table("collection_tasks")
    ):
        return
    with op.batch_alter_table(
        "collection_tasks", **_batch_kwargs(include_recovery_columns=False)
    ) as batch:
        batch.add_column(sa.Column("retry_of_task_id", sa.String(36), nullable=True))
        batch.add_column(sa.Column("recovery_mode", sa.String(32), nullable=True))
        batch.add_column(sa.Column("recovery_reason", sa.Text(), nullable=True))
        batch.add_column(sa.Column("initiating_actor", sa.String(255), nullable=True))
        batch.add_column(
            sa.Column("recovery_idempotency_key", sa.String(255), nullable=True)
        )
        batch.create_foreign_key(
            "fk_collection_tasks_retry_of_task_id",
            "collection_tasks",
            ["retry_of_task_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_unique_constraint(
            "uq_collection_tasks_recovery_idempotency_key",
            ["recovery_idempotency_key"],
        )
    op.create_index(
        "ix_collection_tasks_retry_of_task_id",
        "collection_tasks",
        ["retry_of_task_id"],
    )


def downgrade() -> None:
    if (
        not context.is_offline_mode()
        and not sa.inspect(op.get_bind()).has_table("collection_tasks")
    ):
        return
    op.drop_index("ix_collection_tasks_retry_of_task_id", table_name="collection_tasks")
    with op.batch_alter_table(
        "collection_tasks", **_batch_kwargs(include_recovery_columns=True)
    ) as batch:
        batch.drop_constraint("uq_collection_tasks_recovery_idempotency_key", type_="unique")
        batch.drop_constraint("fk_collection_tasks_retry_of_task_id", type_="foreignkey")
        batch.drop_column("recovery_idempotency_key")
        batch.drop_column("initiating_actor")
        batch.drop_column("recovery_reason")
        batch.drop_column("recovery_mode")
        batch.drop_column("retry_of_task_id")
