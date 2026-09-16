"""bind scheduled automations to pinned agent versions and durable occurrences

Revision ID: bc3d4e5f6a7b
Revises: ab2c3d4e5f6a
"""

import sqlalchemy as sa
from alembic import context, op

revision = "bc3d4e5f6a7b"
down_revision = "ab2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    offline = context.is_offline_mode()
    bind = op.get_bind()
    is_sqlite = not offline and bind.dialect.name == "sqlite"
    tables = (
        {"automations", "operations_agent_runs"}
        if offline
        else set(sa.inspect(bind).get_table_names())
    )
    if not tables.intersection({"automations", "operations_agent_runs"}):
        return

    def has_column(table: str, column: str) -> bool:
        if table not in tables:
            return False
        if offline:
            return False
        return column in {
            item["name"] for item in sa.inspect(bind).get_columns(table)
        }

    def has_index(table: str, index: str) -> bool:
        if table not in tables:
            return False
        if offline:
            return False
        return index in {
            item["name"] for item in sa.inspect(bind).get_indexes(table)
        }

    if "automations" in tables and not has_column("automations", "revision"):
        op.add_column(
            "automations",
            sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        )
    if "automations" in tables and not has_column("automations", "operations_agent_id"):
        op.add_column(
            "automations",
            sa.Column(
                "operations_agent_id",
                sa.String(36),
                *(
                    ()
                    if is_sqlite
                    else (
                        sa.ForeignKey(
                            "operations_agent_identities.id",
                            ondelete="RESTRICT",
                        ),
                    )
                ),
                nullable=True,
            ),
        )
    if "automations" in tables and not has_column("automations", "operations_agent_version"):
        op.add_column(
            "automations",
            sa.Column("operations_agent_version", sa.Integer(), nullable=True),
        )
    if "automations" in tables and not has_index(
        "automations", "ix_automations_operations_agent_id"
    ):
        op.create_index(
            "ix_automations_operations_agent_id",
            "automations",
            ["operations_agent_id"],
        )
    # Previously enabled rows had no executable binding. Fail closed until an
    # operator explicitly pins a compatible published Agent version.
    if "automations" in tables:
        op.execute("UPDATE automations SET enabled = false WHERE operations_agent_id IS NULL")

    run_columns = (
        (
            "automation_id",
            sa.Column(
                "automation_id",
                sa.String(36),
                *(
                    ()
                    if is_sqlite
                    else (
                        sa.ForeignKey("automations.id", ondelete="RESTRICT"),
                    )
                ),
                nullable=True,
            ),
        ),
        ("automation_revision", sa.Column("automation_revision", sa.Integer(), nullable=True)),
        ("automation_snapshot", sa.Column("automation_snapshot", sa.JSON(), nullable=True)),
        (
            "scheduled_for",
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        ),
        (
            "schedule_timezone",
            sa.Column("schedule_timezone", sa.String(64), nullable=True),
        ),
    )
    if "operations_agent_runs" in tables:
        for name, column in run_columns:
            if not has_column("operations_agent_runs", name):
                op.add_column("operations_agent_runs", column)
    if not has_index(
        "operations_agent_runs",
        "ix_operations_agent_runs_automation_id",
    ):
        op.create_index(
            "ix_operations_agent_runs_automation_id",
            "operations_agent_runs",
            ["automation_id"],
        )
    if not has_index(
        "operations_agent_runs",
        "uq_operations_agent_runs_automation_occurrence",
    ):
        op.create_index(
            "uq_operations_agent_runs_automation_occurrence",
            "operations_agent_runs",
            ["automation_id", "scheduled_for"],
            unique=True,
        )


def downgrade() -> None:
    op.drop_index(
        "uq_operations_agent_runs_automation_occurrence",
        table_name="operations_agent_runs",
    )
    op.drop_index(
        "ix_operations_agent_runs_automation_id",
        table_name="operations_agent_runs",
    )
    op.drop_column("operations_agent_runs", "schedule_timezone")
    op.drop_column("operations_agent_runs", "scheduled_for")
    op.drop_column("operations_agent_runs", "automation_snapshot")
    op.drop_column("operations_agent_runs", "automation_revision")
    op.drop_column("operations_agent_runs", "automation_id")

    op.drop_index("ix_automations_operations_agent_id", table_name="automations")
    op.drop_column("automations", "operations_agent_version")
    op.drop_column("automations", "operations_agent_id")
    op.drop_column("automations", "revision")
