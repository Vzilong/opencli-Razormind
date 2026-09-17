"""Public, project-scoped contracts for downstream Agent data consumption."""

from datetime import datetime
from typing import Any

from pydantic import Field

from backend.schemas.common import UTCModel


class ProjectRecordRead(UTCModel):
    """A normalized record with only safe, inspectable provenance."""

    id: str
    version: str
    status: str
    data: dict[str, Any]
    source: dict[str, Any]
    updated_at: datetime
    projection_truncated: bool = False


class ProjectRecordList(UTCModel):
    items: list[ProjectRecordRead]
    total: int = Field(
        ge=0, description="Matching count; a lower bound when total_is_exact is false."
    )
    truncated: bool = False
    total_is_exact: bool = True
    projection_truncated: bool = False


class ProjectContextMatch(UTCModel):
    id: str
    text: str
    source: dict[str, Any]


class ProjectContextResult(UTCModel):
    query: str
    matches: list[ProjectContextMatch]
    gaps: list[str]


class AgentDataCapabilities(UTCModel):
    records: bool
    context: bool
    research: bool
    mcp_path: str = "/mcp"


__all__ = [
    "AgentDataCapabilities",
    "ProjectContextMatch",
    "ProjectContextResult",
    "ProjectRecordList",
    "ProjectRecordRead",
]
