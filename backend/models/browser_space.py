from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database import Base
from backend.models.base import TimestampMixin


class BrowserSpaceOwnerType(StrEnum):
    OPERATOR = "operator"
    RUNTIME_AGENT = "runtime_agent"


class BrowserSpaceStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    CLOSED = "closed"
    ERROR = "error"


class BrowserSpaceControlMode(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class BrowserSpaceTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BrowserSpaceEventKind(StrEnum):
    QUEUED = "queued"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    CONTROL_CHANGED = "control_changed"


_ACTIVE_SPACE_STATUSES = ("idle", "running", "error")
_ACTIVE_TASK_STATUSES = ("queued", "running")


class BrowserSpace(TimestampMixin):
    """Workspace-scoped reservation for one existing BrowserInstance."""

    __tablename__ = "browser_spaces"
    __table_args__ = (
        CheckConstraint(
            "owner_type IN ('operator', 'runtime_agent')",
            name="ck_browser_spaces_owner_type",
        ),
        CheckConstraint(
            "status IN ('idle', 'running', 'closed', 'error')",
            name="ck_browser_spaces_status",
        ),
        CheckConstraint(
            "control_mode IN ('agent', 'human')",
            name="ck_browser_spaces_control_mode",
        ),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    browser_instance_id: Mapped[str] = mapped_column(
        ForeignKey("browser_instances.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    binding_id: Mapped[str | None] = mapped_column(
        ForeignKey("browser_bindings.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_type: Mapped[str] = mapped_column(String(20), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserSpaceStatus.IDLE.value
    )
    control_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserSpaceControlMode.AGENT.value
    )
    granted_capabilities: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)


Index(
    "uq_browser_spaces_active_instance",
    BrowserSpace.browser_instance_id,
    unique=True,
    sqlite_where=BrowserSpace.status.in_(_ACTIVE_SPACE_STATUSES),
    postgresql_where=BrowserSpace.status.in_(_ACTIVE_SPACE_STATUSES),
)


class BrowserSpaceTask(TimestampMixin):
    """One structured capability invocation leased by a BrowserSpace."""

    __tablename__ = "browser_space_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_browser_space_tasks_status",
        ),
    )

    space_id: Mapped[str] = mapped_column(
        ForeignKey("browser_spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    capability: Mapped[str] = mapped_column(String(255), nullable=False)
    # Only the redacted argument-key projection is persisted; raw args stay in the
    # executor call stack and are never returned or logged.
    args: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BrowserSpaceTaskStatus.QUEUED.value
    )
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


Index(
    "uq_browser_space_tasks_request",
    BrowserSpaceTask.space_id,
    BrowserSpaceTask.request_id,
    unique=True,
)
Index(
    "uq_browser_space_tasks_active_space",
    BrowserSpaceTask.space_id,
    unique=True,
    sqlite_where=BrowserSpaceTask.status.in_(_ACTIVE_TASK_STATUSES),
    postgresql_where=BrowserSpaceTask.status.in_(_ACTIVE_TASK_STATUSES),
)

class BrowserSpaceEventCounter(Base):
    """Database-owned event sequence allocator for cross-process writers."""

    __tablename__ = "browser_space_event_counters"

    space_id: Mapped[str] = mapped_column(
        ForeignKey("browser_spaces.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BrowserSpaceEvent(TimestampMixin):
    """Append-only bounded event stream for a BrowserSpace."""

    __tablename__ = "browser_space_events"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('queued', 'started', 'completed', 'failed', 'cancel_requested', "
            "'cancelled', 'control_changed')",
            name="ck_browser_space_events_kind",
        ),
    )

    space_id: Mapped[str] = mapped_column(
        ForeignKey("browser_spaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable only for a close event, which has no task to reference.
    task_id: Mapped[str | None] = mapped_column(
        ForeignKey("browser_space_tasks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


Index(
    "uq_browser_space_events_sequence",
    BrowserSpaceEvent.space_id,
    BrowserSpaceEvent.sequence,
    unique=True,
)
