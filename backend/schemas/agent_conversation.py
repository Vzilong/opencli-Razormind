from datetime import datetime
from typing import Any, Literal

from pydantic import (
    AliasPath,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    model_validator,
)

from backend.schemas.common import UTCModel

EXECUTION_CONTEXT_KEY = "_execution"


class AgentExecutionSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    runtime_id: Literal["opencli", "codex", "omp"] = "opencli"
    provider_id: str | None = Field(default=None, min_length=1, max_length=36)
    model_id: str | None = Field(default=None, min_length=1, max_length=255)
    mode: Literal["gui", "terminal"] = "gui"
    access_mode: Literal["provider", "native"] = "provider"
    binding_revision: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$", exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def require_provider_for_model(self) -> "AgentExecutionSelection":
        if self.runtime_id != "opencli":
            if self.access_mode != "native" or self.provider_id or self.model_id:
                raise ValueError("native runtime requires native access without provider overrides")
        elif self.access_mode != "provider" or self.binding_revision is not None:
            raise ValueError("OpenCLI requires provider access without a native binding")
        if self.model_id is not None and self.provider_id is None:
            raise ValueError("model_id requires provider_id")
        if self.mode == "terminal" and self.runtime_id not in {"codex", "omp"}:
            raise ValueError("terminal mode requires the Codex or OMP native runtime")
        return self


class AgentTerminalStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial_input: str = Field(min_length=1, max_length=20_000)
    cols: int = Field(default=80, ge=1, le=1000, strict=True)
    rows: int = Field(default=24, ge=1, le=1000, strict=True)


class AgentTerminalSessionRead(UTCModel):
    id: str
    conversation_id: str
    workspace_id: str
    runtime_id: Literal["codex", "omp"]
    status: Literal["starting", "active", "stopping", "exited", "failed", "lost"]
    exit_code: int | None
    cleanup_confirmed: bool
    revision: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentTerminalTicketRead(BaseModel):
    ticket: str
    expires_in: int


class AgentChatRuntimeOption(BaseModel):
    id: str
    name: str
    available: bool
    reason: str | None
    installed: bool | None
    modes: list[str]
    access_modes: list[str]


class AgentChatModelOption(BaseModel):
    id: str
    name: str


class AgentChatProviderOption(BaseModel):
    id: str
    name: str
    models: list[AgentChatModelOption]
    default_model: str | None


class AgentChatOptions(BaseModel):
    runtimes: list[AgentChatRuntimeOption]
    providers: list[AgentChatProviderOption]
    reasoning_efforts: list[str] = Field(default_factory=list)
    provider_selection_allowed: bool
    default_route_ready: bool


class AgentConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = Field(default=None, min_length=1, max_length=36)
    title: str | None = Field(default=None, max_length=255)
    context: dict[str, Any] | None = None
    execution: AgentExecutionSelection | None = None


class AgentConversationMessageCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=20_000)
    context: dict[str, Any] | None = None


class AgentConversationTurnRead(UTCModel):
    id: str
    sequence: int
    request_id: str
    user_content: str
    response: dict[str, Any] | None
    context_binding: dict[str, Any]
    tool_trace: list[dict[str, Any]]
    status: str
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentConversationRead(UTCModel):
    id: str
    workspace_id: str
    title: str | None
    status: str
    created_by_user_id: str
    context_binding: dict[str, Any]
    execution: AgentExecutionSelection = Field(
        default_factory=AgentExecutionSelection,
        validation_alias=AliasPath("context_binding", EXECUTION_CONTEXT_KEY),
    )
    revision: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    @field_serializer("context_binding")
    def public_context(self, value: dict[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if key != EXECUTION_CONTEXT_KEY}


class AgentConversationDetail(AgentConversationRead):
    turns: list[AgentConversationTurnRead] = Field(default_factory=list)


class AgentConversationMessageRead(UTCModel):
    conversation_id: str
    turn: AgentConversationTurnRead
