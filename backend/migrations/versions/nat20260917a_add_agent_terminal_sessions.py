"""add conversation-owned native terminal sessions

Revision ID: nat20260917a
Revises: bsc20260914a
Create Date: 2026-09-17
"""

import sqlalchemy as sa
from alembic import op

revision = "nat20260917a"
down_revision = "bsc20260914a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_terminal_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_id", sa.String(length=16), nullable=False),
        sa.Column("agent_key", sa.String(length=64), nullable=False),
        sa.Column("binding_revision", sa.String(length=64), nullable=False),
        sa.Column("started_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="starting", nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("cleanup_confirmed", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "runtime_id IN ('codex', 'omp')", name="ck_agent_terminal_sessions_runtime"
        ),
        sa.CheckConstraint(
            "status IN ('starting', 'active', 'stopping', 'exited', 'failed', 'lost')",
            name="ck_agent_terminal_sessions_status",
        ),
        sa.CheckConstraint(
            "revision >= 0", name="ck_agent_terminal_sessions_revision_nonnegative"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["agent_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["started_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "conversation_id", name="uq_agent_terminal_sessions_conversation"
        ),
    )
    op.create_index(
        "ix_agent_terminal_sessions_conversation_id",
        "agent_terminal_sessions",
        ["conversation_id"],
    )
    op.create_index(
        "ix_agent_terminal_sessions_workspace_id",
        "agent_terminal_sessions",
        ["workspace_id"],
    )
    op.create_index(
        "ix_agent_terminal_sessions_started_by_user_id",
        "agent_terminal_sessions",
        ["started_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_terminal_sessions_started_by_user_id",
        table_name="agent_terminal_sessions",
    )
    op.drop_index(
        "ix_agent_terminal_sessions_workspace_id", table_name="agent_terminal_sessions"
    )
    op.drop_index(
        "ix_agent_terminal_sessions_conversation_id", table_name="agent_terminal_sessions"
    )
    op.drop_table("agent_terminal_sessions")
