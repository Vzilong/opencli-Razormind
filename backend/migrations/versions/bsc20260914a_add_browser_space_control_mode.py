"""Add persisted Browser Space agent or human control mode.

Revision ID: bsc20260914a
Revises: kb20260914a
"""

import sqlalchemy as sa
from alembic import op

revision = "bsc20260914a"
down_revision = "kb20260914a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("browser_spaces") as batch:
        batch.add_column(
            sa.Column("control_mode", sa.String(length=20), nullable=False, server_default="agent")
        )
        batch.create_check_constraint(
            "ck_browser_spaces_control_mode", "control_mode IN ('agent', 'human')"
        )
    with op.batch_alter_table("browser_space_events") as batch:
        batch.drop_constraint("ck_browser_space_events_kind", type_="check")
        batch.create_check_constraint(
            "ck_browser_space_events_kind",
            "kind IN ('queued', 'started', 'completed', 'failed', 'cancel_requested', "
            "'cancelled', 'control_changed')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(
        sa.text("SELECT 1 FROM browser_spaces WHERE control_mode = 'human' LIMIT 1")
    ).scalar():
        raise RuntimeError(
            "cannot downgrade Browser Space control mode while human control state exists; "
            "migrate or resolve control ownership first"
        )
    if bind.execute(
        sa.text("SELECT 1 FROM browser_space_events WHERE kind = 'control_changed' LIMIT 1")
    ).scalar():
        raise RuntimeError(
            "cannot downgrade Browser Space control mode while control_changed audit events exist; "
            "migrate or preserve the audit history first"
        )
    with op.batch_alter_table("browser_space_events") as batch:
        batch.drop_constraint("ck_browser_space_events_kind", type_="check")
        batch.create_check_constraint(
            "ck_browser_space_events_kind",
            "kind IN ('queued', 'started', 'completed', 'failed', 'cancel_requested', 'cancelled')",
        )
    with op.batch_alter_table("browser_spaces") as batch:
        batch.drop_constraint("ck_browser_spaces_control_mode", type_="check")
        batch.drop_column("control_mode")
