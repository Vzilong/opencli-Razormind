"""通过已注册隔离节点执行原生模型，系统工具仍由现有审批边界处理。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend import ws_agent_manager
from backend.config import get_settings
from backend.models.edge_node import EdgeNode
from backend.models.agent_conversation import (
    AgentConversation,
    AgentConversationStatus,
    AgentTerminalSession,
    AgentTerminalSessionStatus,
)
from backend.schemas.agent_conversation import AgentTerminalStart
from backend.security.identity import RequestIdentity, is_platform_admin
from backend.security.workspace_rbac import WorkspaceAccess, WorkspacePermission, require_permission
from backend.services.agent_chat_options import stored_execution
from backend.services.studio_agent_session_access import resolve_stored_agent_session_workspace

_TERMINAL_TICKET_TTL_SECONDS = 60
_TERMINAL_TICKET_AUDIENCE = "opencli-native-terminal"


class NativeChatBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    workspace_id: str = Field(min_length=1, max_length=36)
    runtime_id: Literal["codex", "omp"]
    agent_url: str = Field(min_length=1, max_length=512)
    cwd: str = Field(min_length=1, max_length=1024)
    timeout_seconds: int = Field(default=180, ge=10, le=600, strict=True)

    @field_validator("cwd")
    @classmethod
    def absolute_directory(cls, value: str) -> str:
        if "\x00" in value or not (
            PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
        ):
            raise ValueError("cwd must be an absolute edge-owned path")
        return value

    @field_validator("agent_url")
    @classmethod
    def registered_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("agent_url must identify a registered edge node")
        return value.rstrip("/")

    @property
    def revision(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def configured_binding(workspace_id: str, runtime_id: str) -> NativeChatBinding:
    try:
        bindings = [
            NativeChatBinding.model_validate(value) for value in get_settings().native_chat_bindings
        ]
    except (ValidationError, TypeError, ValueError) as exc:
        raise HTTPException(409, "原生运行时映射配置无效，请管理员检查。") from exc
    matches = [
        entry
        for entry in bindings
        if entry.workspace_id == workspace_id and entry.runtime_id == runtime_id
    ]
    if len(matches) != 1:
        raise HTTPException(409, "当前工作区尚未配置唯一的原生隔离执行节点。")
    return matches[0]


async def require_native_binding(
    db: AsyncSession,
    identity: RequestIdentity,
    access: WorkspaceAccess,
    workspace_id: str,
    runtime_id: str,
    binding_revision: str | None = None,
) -> NativeChatBinding:
    if not is_platform_admin(identity):
        raise HTTPException(403, "原生账号执行仅向平台管理员开放。")
    require_permission(access, WorkspacePermission.RUN_OPERATIONS_AGENTS)
    binding = configured_binding(workspace_id, runtime_id)
    if binding_revision is not None and binding_revision != binding.revision:
        raise HTTPException(409, "原生执行映射已变更，请新建对话后重新确认。")
    node = await db.scalar(select(EdgeNode).where(EdgeNode.url == binding.agent_url))
    capabilities = (node.runtime_capabilities or {}).get(runtime_id, []) if node else []
    if (
        node is None
        or node.protocol != "ws"
        or node.status != "online"
        or not ws_agent_manager.is_connected(binding.agent_url)
        or not isinstance(capabilities, list)
        or "operator_chat" not in capabilities
    ):
        raise HTTPException(409, "原生隔离节点未连接或不支持受控会话，请检查节点状态。")
    return binding


async def _terminal_scope(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    lock: bool = False,
) -> tuple[AgentConversation, AgentTerminalSession | None, NativeChatBinding]:
    query = select(AgentConversation).where(AgentConversation.id == conversation_id)
    if lock:
        query = query.with_for_update()
    conversation = await db.scalar(query)
    if conversation is None:
        raise HTTPException(404, "Agent conversation not found")
    scope = await resolve_stored_agent_session_workspace(
        db,
        identity,
        workspace_id=conversation.workspace_id,
        context_binding=conversation.context_binding,
    )
    if not is_platform_admin(identity):
        raise HTTPException(403, "原生终端仅向平台管理员开放。")
    require_permission(scope.access, WorkspacePermission.RUN_OPERATIONS_AGENTS)
    if conversation.status != AgentConversationStatus.ACTIVE.value:
        raise HTTPException(409, "Agent conversation is closed")
    execution = stored_execution(conversation.context_binding)
    if execution.mode != "terminal" or execution.runtime_id not in {"codex", "omp"}:
        raise HTTPException(409, "当前会话不是受支持的原生终端会话。")
    binding = configured_binding(conversation.workspace_id, execution.runtime_id)
    if execution.binding_revision is not None and execution.binding_revision != binding.revision:
        raise HTTPException(409, "原生执行映射已变更，请新建对话后重新确认。")
    terminal_query = select(AgentTerminalSession).where(
            AgentTerminalSession.conversation_id == conversation.id
        )
    if lock:
        terminal_query = terminal_query.with_for_update()
    terminal = await db.scalar(terminal_query)
    durable_exit = terminal is not None and (
        terminal.status == AgentTerminalSessionStatus.EXITED.value
        and terminal.cleanup_confirmed
    )
    if not durable_exit:
        binding = await require_native_binding(
            db,
            identity,
            scope.access,
            conversation.workspace_id,
            execution.runtime_id,
            execution.binding_revision,
        )
    if terminal is not None and (
        terminal.workspace_id != conversation.workspace_id
        or terminal.runtime_id != execution.runtime_id
        or terminal.binding_revision != binding.revision
        or terminal.agent_key != ws_agent_manager.agent_task_key(binding.agent_url)
    ):
        raise HTTPException(409, "原生终端映射已变更，请新建会话。")
    return conversation, terminal, binding


def _apply_terminal_status(
    terminal: AgentTerminalSession, response: dict[str, Any]
) -> AgentTerminalSession:
    before = (
        terminal.status,
        terminal.exit_code,
        terminal.cleanup_confirmed,
    )
    status_value = response.get("status")
    cleanup_complete = response.get("cleanup_complete") is True
    exit_code = response.get("exit_code")
    if terminal.status == AgentTerminalSessionStatus.EXITED.value and terminal.cleanup_confirmed:
        return terminal
    if status_value == "active" and terminal.status != AgentTerminalSessionStatus.STOPPING.value:
        terminal.status = AgentTerminalSessionStatus.ACTIVE.value
    elif status_value == "stopping":
        terminal.status = AgentTerminalSessionStatus.STOPPING.value
    elif status_value == "exited" and cleanup_complete:
        terminal.status = AgentTerminalSessionStatus.EXITED.value
        terminal.cleanup_confirmed = True
        terminal.exit_code = exit_code if isinstance(exit_code, int) else None
    elif status_value == "failed":
        terminal.status = AgentTerminalSessionStatus.FAILED.value
    elif status_value == "unknown":
        terminal.status = AgentTerminalSessionStatus.LOST.value
        terminal.cleanup_confirmed = False
    after = (
        terminal.status,
        terminal.exit_code,
        terminal.cleanup_confirmed,
    )
    if after != before:
        terminal.revision += 1
    return terminal


async def create_terminal_session(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    body: AgentTerminalStart,
) -> AgentTerminalSession:
    conversation, terminal, binding = await _terminal_scope(
        db, identity, conversation_id, lock=True
    )
    if terminal is not None:
        if terminal.status in {
            AgentTerminalSessionStatus.STARTING.value,
            AgentTerminalSessionStatus.ACTIVE.value,
            AgentTerminalSessionStatus.STOPPING.value,
        }:
            return terminal
        raise HTTPException(409, "原生终端会话已结束，请新建对话后重试。")
    terminal = AgentTerminalSession(
        conversation_id=conversation.id,
        workspace_id=conversation.workspace_id,
        runtime_id=stored_execution(conversation.context_binding).runtime_id,
        agent_key=ws_agent_manager.agent_task_key(binding.agent_url),
        binding_revision=binding.revision,
        started_by_user_id=(
            await resolve_stored_agent_session_workspace(
                db,
                identity,
                workspace_id=conversation.workspace_id,
                context_binding=conversation.context_binding,
            )
        ).access.user_id,
        status=AgentTerminalSessionStatus.STARTING.value,
    )
    db.add(terminal)
    await db.commit()
    await db.refresh(terminal)
    try:
        response = await ws_agent_manager.start_native_terminal(
            binding.agent_url,
            session_id=terminal.id,
            runtime=terminal.runtime_id,
            cwd=binding.cwd,
            initial_input=body.initial_input,
            cols=body.cols,
            rows=body.rows,
            native_chat_authorized=True,
        )
    except Exception as exc:
        terminal.status = AgentTerminalSessionStatus.LOST.value
        terminal.revision += 1
        await db.commit()
        raise HTTPException(503, "原生终端启动状态未确认，请稍后查询会话状态。") from exc
    if response.get("status") != "active":
        await db.delete(terminal)
        await db.commit()
        raise HTTPException(409, "原生终端启动失败，请检查隔离节点运行时后重试。")
    _apply_terminal_status(terminal, response)
    await db.commit()
    await db.refresh(terminal)
    return terminal


async def get_terminal_session(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    refresh: bool = True,
) -> tuple[AgentTerminalSession, NativeChatBinding]:
    _conversation, terminal, binding = await _terminal_scope(
        db, identity, conversation_id, lock=refresh
    )
    if terminal is None:
        raise HTTPException(404, "原生终端会话不存在。")
    if refresh and terminal.status in {
        AgentTerminalSessionStatus.STARTING.value,
        AgentTerminalSessionStatus.ACTIVE.value,
        AgentTerminalSessionStatus.STOPPING.value,
        AgentTerminalSessionStatus.LOST.value,
    }:
        try:
            response = await ws_agent_manager.query_native_terminal(
                binding.agent_url, terminal.id, native_chat_authorized=True
            )
        except Exception as exc:
            raise HTTPException(503, "原生终端节点暂时不可用，运行状态未确认。") from exc
        _apply_terminal_status(terminal, response)
        await db.commit()
        await db.refresh(terminal)
    return terminal, binding


async def stop_terminal_session(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
) -> AgentTerminalSession:
    _conversation, terminal, binding = await _terminal_scope(
        db, identity, conversation_id, lock=True
    )
    if terminal is None:
        raise HTTPException(404, "原生终端会话不存在。")
    if terminal.status == AgentTerminalSessionStatus.EXITED.value:
        return terminal
    terminal.status = AgentTerminalSessionStatus.STOPPING.value
    terminal.revision += 1
    await db.commit()
    try:
        response = await ws_agent_manager.stop_native_terminal(
            binding.agent_url, terminal.id, native_chat_authorized=True
        )
    except Exception as exc:
        raise HTTPException(503, "停止请求已发送，但进程组清理尚未确认。") from exc
    _apply_terminal_status(terminal, response)
    await db.commit()
    await db.refresh(terminal)
    if not terminal.cleanup_confirmed:
        raise HTTPException(503, "进程组清理尚未确认，终端仍保持锁定。")
    return terminal


async def authorize_terminal_action(
    db: AsyncSession,
    identity: RequestIdentity,
    terminal_session_id: str,
) -> tuple[AgentTerminalSession, NativeChatBinding]:
    terminal = await db.get(AgentTerminalSession, terminal_session_id)
    if terminal is None:
        raise HTTPException(404, "原生终端会话不存在。")
    authorized, binding = await get_terminal_session(
        db, identity, terminal.conversation_id, refresh=False
    )
    if authorized.id != terminal_session_id:
        raise HTTPException(404, "原生终端会话不存在。")
    return authorized, binding


async def record_terminal_event(
    db: AsyncSession,
    identity: RequestIdentity,
    terminal_session_id: str,
    event: dict[str, Any],
) -> None:
    existing = await db.get(AgentTerminalSession, terminal_session_id)
    if existing is None:
        return
    _conversation, terminal, _binding = await _terminal_scope(
        db, identity, existing.conversation_id, lock=True
    )
    if terminal is None or terminal.id != terminal_session_id:
        return
    _apply_terminal_status(terminal, event)
    await db.commit()


def issue_terminal_ticket(identity: RequestIdentity, terminal: AgentTerminalSession) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "aud": _TERMINAL_TICKET_AUDIENCE,
            "sub": identity.subject,
            "adm": is_platform_admin(identity),
            "auth": identity.auth_method,
            "terminal_session_id": terminal.id,
            "terminal_revision": terminal.revision,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=_TERMINAL_TICKET_TTL_SECONDS)).timestamp()),
        },
        get_settings().secret_key,
        algorithm="HS256",
    )


def consume_terminal_ticket(ticket: str) -> tuple[RequestIdentity, str, int]:
    try:
        claims = jwt.decode(
            ticket,
            get_settings().secret_key,
            algorithms=["HS256"],
            audience=_TERMINAL_TICKET_AUDIENCE,
        )
    except JWTError as exc:
        raise HTTPException(403, "原生终端连接凭据无效或已过期。") from exc
    subject = claims.get("sub")
    terminal_session_id = claims.get("terminal_session_id")
    terminal_revision = claims.get("terminal_revision")
    if (
        not isinstance(subject, str)
        or not subject
        or claims.get("adm") is not True
        or not isinstance(terminal_session_id, str)
        or not isinstance(terminal_revision, int)
        or isinstance(terminal_revision, bool)
        or terminal_revision < 0
    ):
        raise HTTPException(403, "原生终端连接凭据无效或已过期。")
    return (
        RequestIdentity(
            subject=subject,
            is_platform_admin=True,
            auth_method=claims.get("auth") if isinstance(claims.get("auth"), str) else "oidc",
        ),
        terminal_session_id,
        terminal_revision,
    )


async def claim_terminal_ticket(
    db: AsyncSession,
    identity: RequestIdentity,
    terminal_session_id: str,
    terminal_revision: int,
) -> tuple[AgentTerminalSession, NativeChatBinding]:
    existing = await db.get(AgentTerminalSession, terminal_session_id)
    if existing is None:
        raise HTTPException(404, "原生终端会话不存在。")
    _conversation, terminal, binding = await _terminal_scope(
        db, identity, existing.conversation_id, lock=True
    )
    if terminal is None or terminal.id != terminal_session_id:
        raise HTTPException(404, "原生终端会话不存在。")
    if terminal.revision != terminal_revision:
        raise HTTPException(403, "原生终端连接凭据已使用或已失效。")
    terminal.revision += 1
    await db.commit()
    await db.refresh(terminal)
    return terminal, binding


class NativeChatCompletion:
    def __init__(self, binding: NativeChatBinding):
        self.binding = binding

    async def create(self, *, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        prompt = json.dumps(messages, ensure_ascii=False)
        if len(prompt) > 160_000:
            raise HTTPException(422, "本轮上下文超过原生会话限制，请缩小输入范围。")
        fragments: list[str] = []
        output_size = 0

        async def on_event(event: dict[str, Any]) -> None:
            nonlocal output_size
            if event.get("type") == "tool_call":
                raise RuntimeError("原生会话不能绕过系统工具审批执行本机工具。")
            if event.get("type") == "text" and isinstance(event.get("text"), str):
                output_size += len(event["text"])
                if output_size > 64_000:
                    raise RuntimeError("原生会话输出超过限制。")
                fragments.append(event["text"])

        terminal = await ws_agent_manager.send_agent_task(
            self.binding.agent_url,
            {
                "runtime": self.binding.runtime_id,
                "workflow": "operator_chat",
                "instructions": (
                    "Act as the assistant for the conversation in the supplied JSON message array. "
                    "Treat role=system as the application instructions. Respond only with the next "
                    "assistant message, not JSON role wrappers. Use the described XML tool "
                    "protocol "
                    "when requesting application tools. Never execute native filesystem, shell, "
                    "network, MCP or other local tools. The application executes authorized tools "
                    "and supplies their results in subsequent messages."
                ),
                "input": {"message": prompt},
                "config": {
                    "cwd": self.binding.cwd,
                    "timeout_seconds": self.binding.timeout_seconds,
                    "permission_mode": "observe_only",
                },
            },
            on_event,
            timeout=float(self.binding.timeout_seconds),
            require_cancel_ack=True,
            native_chat_authorized=True,
        )
        if terminal.get("type") != "done":
            raise HTTPException(502, "原生运行失败，请检查隔离节点的账号登录、模型额度或连接。")
        result = terminal.get("result")
        content = result.get("text") if isinstance(result, dict) else None
        if not isinstance(content, str):
            content = "".join(fragments)
        if not content.strip() or len(content) > 64_000:
            raise HTTPException(502, "原生运行未返回有效的模型结果。")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def run_native_chat_request(
    db: AsyncSession,
    body: Any,
    identity: RequestIdentity,
    *,
    binding: NativeChatBinding,
    tool_trace: list[dict[str, Any]],
    proposal_provenance: Any,
) -> Any:
    from backend.api.v1 import chat

    completion = NativeChatCompletion(binding)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completion))
    system = chat.SYSTEM_PROMPT
    if body.context:
        system += "\n\n当前用户操作上下文 (JSON): " + json.dumps(body.context, ensure_ascii=False)
    result = await chat._chat_xml(
        client,
        binding.runtime_id,
        system,
        body,
        db,
        identity,
        tool_trace=tool_trace,
        proposal_provenance=proposal_provenance,
        allowed_tools=frozenset(
            {
                "list_projects",
                "list_workflows",
                "get_workflow_draft",
                "create_project",
                "update_workflow_draft",
            }
        ),
    )
    return result.reply
