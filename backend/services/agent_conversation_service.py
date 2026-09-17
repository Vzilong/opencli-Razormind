"""Persistence and execution boundary for Global Agent conversations.

Conversation rows contain bounded, redacted continuity data only. Product state and
proposal execution remain owned by their existing services.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from backend import ws_agent_manager
from backend.api.v1 import chat
from backend.control.agent_control import agent_control_service  # noqa: F401
from backend.llm.base import LlmAdapterError
from backend.llm.resolver import ResolverError
from backend.models.agent_conversation import (
    AgentConversation,
    AgentConversationStatus,
    AgentTerminalSession,
    AgentConversationTurn,
    AgentConversationTurnStatus,
)
from backend.models.identity import Workspace as GovernedWorkspace
from backend.models.source_binding import Source, SourceBinding
from backend.models.studio import StudioProject, StudioWorkflow
from backend.models.workflow import Project as GovernedProject
from backend.models.workflow import Workflow as GovernedWorkflow
from backend.models.workflow_run import WorkflowRun
from backend.schemas.agent_conversation import EXECUTION_CONTEXT_KEY, AgentExecutionSelection
from backend.security.identity import RequestIdentity, is_platform_admin
from backend.security.workspace_rbac import (
    WorkspacePermission,
    require_permission,
)
from backend.services.agent_chat_options import stored_execution, validate_execution
from backend.services.agent_conversation_cancellation import (
    CANCEL_REQUESTED,
    CANCELLED,
    CANCELLED_MESSAGE,
    ConversationTurnCancelledError,
    await_cleanup,
    has_turn_runner,
    register_turn_runner,
    run_with_cancellation,
    unregister_turn_runner,
)
from backend.services.studio_agent_session_access import (
    AgentSessionWorkspaceScope,
    resolve_agent_session_workspace,
    resolve_stored_agent_session_workspace,
    session_matches_studio_context,
)
from backend.ws_agent_manager import AgentTaskUnresolvedError

MAX_USER_CONTENT = 20_000
MAX_HISTORY_TURNS = 20
MAX_HISTORY_CHARS = 32_000
MAX_ERROR_MESSAGE = 4_000
REMOTE_DISPATCH_CONTEXT_KEY = "_remote_dispatch"
REMOTE_UNCONFIRMED_MESSAGE = "执行节点尚未确认退出；当前会话保持运行锁，不能重复发送。"
REMOTE_PROBE_TIMEOUT_SECONDS = 10
_ALLOWED_CONTEXT_KEYS = frozenset({"project_id", "workflow_id", "run_id", "source_id", "surface"})
_STORED_CONTEXT_KEYS = _ALLOWED_CONTEXT_KEYS | {"studio_workspace_id"}
_SECRET_PATTERN = re.compile(
    r"(?ix)(?:"
    r"(?:api[_ -]?key|access[_ -]?token|authorization|password|secret|credential|"
    r"connection[_ -]?string|token)\s*(?:[:=]|is)\s*(?:bearer\s+)?[^\s,;]+"
    r"|bearer\s+[^\s,;]+"
    r")"
)
_URL_PATTERN = re.compile(r"https?://[^\s,;]+", re.IGNORECASE)


class AgentConversationError(ValueError):
    """Stable client-facing validation failure before a turn is written."""


async def _resolve_workspace_scope(
    db: AsyncSession,
    identity: RequestIdentity,
    workspace_id: str | None,
    *,
    context: dict[str, Any] | None,
) -> AgentSessionWorkspaceScope:
    """Keep the legacy ambiguous-membership response stable at this API edge."""

    try:
        return await resolve_agent_session_workspace(
            db,
            identity,
            workspace_id,
            context=context,
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_400_BAD_REQUEST:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "workspace_id is required when the actor belongs to multiple workspaces",
            ) from exc
        raise


async def validate_context_binding(
    db: AsyncSession,
    workspace_id: str,
    context: dict[str, Any] | None,
    *,
    studio_workspace_id: str | None = None,
    allow_stored_studio_workspace: bool = False,
) -> dict[str, str]:
    """Validate object ownership and return an immutable, bounded snapshot."""

    context = context or {}
    if not isinstance(context, dict):
        raise AgentConversationError("context must be an object")
    allowed_keys = _STORED_CONTEXT_KEYS if allow_stored_studio_workspace else _ALLOWED_CONTEXT_KEYS
    unknown = set(context) - allowed_keys
    if unknown:
        raise AgentConversationError("context contains unsupported fields")

    normalized: dict[str, str] = {}
    for key in _ALLOWED_CONTEXT_KEYS:
        value = context.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 255:
            raise AgentConversationError(f"context.{key} must be a bounded non-empty string")
        normalized_value = value.strip()
        _reject_unsafe_content(normalized_value)
        normalized[key] = normalized_value
    project_id = normalized.get("project_id")
    workflow_id = normalized.get("workflow_id")
    run_id = normalized.get("run_id")
    source_id = normalized.get("source_id")
    stored_studio_workspace_id = context.get("studio_workspace_id")
    if allow_stored_studio_workspace and stored_studio_workspace_id is not None:
        if not isinstance(stored_studio_workspace_id, str) or not stored_studio_workspace_id:
            raise AgentConversationError("context.studio_workspace_id must be a non-empty string")
        if studio_workspace_id is not None and stored_studio_workspace_id != studio_workspace_id:
            raise AgentConversationError("Studio Workspace context cannot change")
        studio_workspace_id = stored_studio_workspace_id
    if studio_workspace_id is not None and not (project_id or workflow_id or run_id):
        raise AgentConversationError(
            "Studio Agent sessions require project, workflow, or run context"
        )

    studio_workspace_mapped = studio_workspace_id is not None
    if project_id or workflow_id or run_id:
        studio_workspace_mapped = studio_workspace_mapped or (
            await db.scalar(
                select(GovernedWorkspace.id).where(
                    GovernedWorkspace.id == workspace_id,
                    GovernedWorkspace.active.is_(True),
                )
            )
            is not None
        )

    project: GovernedProject | StudioProject | None = None
    studio_scope_id = studio_workspace_id or workspace_id
    if project_id:
        project = await db.scalar(
            select(GovernedProject).where(
                GovernedProject.id == project_id,
                GovernedProject.workspace_id == workspace_id,
            )
        )
        if project is None and studio_workspace_mapped:
            project = await db.scalar(
                select(StudioProject).where(
                    StudioProject.id == project_id,
                    StudioProject.workspace_id == studio_scope_id,
                )
            )
        if project is None:
            raise AgentConversationError("project is not owned by the Workspace")

    workflow: GovernedWorkflow | StudioWorkflow | None = None
    if workflow_id:
        workflow = await db.scalar(
            select(GovernedWorkflow)
            .join(GovernedProject, GovernedProject.id == GovernedWorkflow.project_id)
            .where(
                GovernedWorkflow.id == workflow_id,
                GovernedProject.workspace_id == workspace_id,
            )
        )
        if workflow is None and studio_workspace_mapped:
            workflow = await db.scalar(
                select(StudioWorkflow)
                .join(StudioProject, StudioProject.id == StudioWorkflow.project_id)
                .where(
                    StudioWorkflow.id == workflow_id,
                    StudioProject.workspace_id == studio_scope_id,
                )
            )
        if workflow is None:
            raise AgentConversationError("workflow is not owned by the Workspace")
        if project_id and workflow.project_id != project_id:
            raise AgentConversationError("workflow does not belong to project")

    if run_id:
        run = await db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id))
        if run is None:
            raise AgentConversationError("run is not available in the Workspace")
        if workflow_id and run.workflow_id != workflow_id:
            raise AgentConversationError("run does not belong to workflow")
        if workflow is None:
            workflow = await db.scalar(
                select(GovernedWorkflow)
                .join(GovernedProject, GovernedProject.id == GovernedWorkflow.project_id)
                .where(
                    GovernedWorkflow.id == run.workflow_id,
                    GovernedProject.workspace_id == workspace_id,
                )
            )
            if workflow is None and studio_workspace_mapped:
                workflow = await db.scalar(
                    select(StudioWorkflow)
                    .join(StudioProject, StudioProject.id == StudioWorkflow.project_id)
                    .where(
                        StudioWorkflow.id == run.workflow_id,
                        StudioProject.workspace_id == studio_scope_id,
                    )
                )
            if workflow is None:
                raise AgentConversationError("run is not owned by the Workspace")
        if project is None:
            project = await db.get(GovernedProject, workflow.project_id)
            if project is None and studio_workspace_mapped:
                project = await db.get(StudioProject, workflow.project_id)
        if project is None or project.workspace_id != studio_scope_id:
            raise AgentConversationError("run is not owned by the Workspace")

    if source_id:
        if studio_workspace_id is not None:
            raise AgentConversationError("source is not available in a Studio Agent session")
        source = await db.scalar(
            select(Source).where(Source.id == source_id, Source.workspace_id == workspace_id)
        )
        if source is None:
            # A SourceBinding is also a valid proof of Workspace ownership when
            # callers identify the project-scoped binding rather than the source.
            source = await db.scalar(
                select(Source)
                .join(SourceBinding, SourceBinding.source_id == Source.id)
                .join(GovernedProject, GovernedProject.id == SourceBinding.project_id)
                .where(
                    Source.id == source_id,
                    GovernedProject.workspace_id == workspace_id,
                )
            )
        if source is None:
            raise AgentConversationError("source is not owned by the Workspace")

    if studio_workspace_id is not None:
        normalized["studio_workspace_id"] = studio_workspace_id
    return dict(normalized)


def _reject_unsafe_content(content: str) -> None:
    if not content.strip():
        raise AgentConversationError("content must not be empty")
    if len(content) > MAX_USER_CONTENT:
        raise AgentConversationError("content exceeds the 20000 character limit")
    if _SECRET_PATTERN.search(content):
        raise AgentConversationError("content contains a credential-like value")


def _redact_error(value: str) -> str:
    value = _SECRET_PATTERN.sub("[REDACTED]", value)
    return _URL_PATTERN.sub("[REDACTED_URL]", value)[:MAX_ERROR_MESSAGE]


_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(?:api[_ -]?key|access[_ -]?token|authorization|bearer|password|"
    r"secret|credential|connection|token|cookie|header|endpoint|profile|html|url)"
)


def _redact_json(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            name: "[REDACTED]"
            if _SENSITIVE_KEY_PATTERN.search(name)
            else _redact_json(item, key=name)
            for name, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_error(value)
    return value


def _ensure_studio_context_continuity(
    previous: dict[str, Any] | None,
    next_binding: dict[str, str],
) -> None:
    """A Studio session may refine its context, but never switch its target."""

    for key in ("project_id", "workflow_id", "run_id"):
        previous_value = (previous or {}).get(key)
        next_value = next_binding.get(key)
        if previous_value is not None and previous_value != next_value:
            raise AgentConversationError("Studio Agent session context cannot switch projects")


def _is_studio_session(conversation: AgentConversation) -> bool:
    binding = conversation.context_binding
    return isinstance(binding, dict) and bool(binding.get("studio_workspace_id"))


def _assistant_history(response: dict[str, Any] | None) -> str:
    if not response:
        return ""
    if response.get("type") == "message":
        content = response.get("content")
        return content if isinstance(content, str) else ""
    proposal = response.get("proposal")
    if isinstance(proposal, dict):
        summary = proposal.get("summary")
        return summary if isinstance(summary, str) else ""
    return ""


def bounded_history(
    turns: list[AgentConversationTurn], current_content: str
) -> list[dict[str, str]]:
    """Return complete user/assistant pairs inside the model context budget."""

    pairs: list[tuple[str, str]] = []
    for turn in turns[-MAX_HISTORY_TURNS:]:
        assistant = _assistant_history(turn.response)
        if assistant:
            pairs.append((turn.user_content, assistant))

    while (
        pairs
        and sum(len(user) + len(assistant) for user, assistant in pairs) + len(current_content)
        > MAX_HISTORY_CHARS
    ):
        pairs.pop(0)

    messages: list[dict[str, str]] = []
    for user, assistant in pairs:
        messages.extend(
            ({"role": "user", "content": user}, {"role": "assistant", "content": assistant})
        )
    messages.append({"role": "user", "content": current_content})
    return messages


def _safe_response(reply: chat.ChatReply) -> dict[str, Any]:
    """Persist only the public reply shape, never an SDK/model message."""

    response: dict[str, Any] = {"type": reply.type}
    if reply.content is not None:
        response["content"] = _redact_error(reply.content[:MAX_USER_CONTENT])
    if reply.proposal is not None:
        response["proposal"] = _redact_json(reply.proposal.model_dump(exclude_none=True))
    return response


async def create_conversation(
    db: AsyncSession,
    identity: RequestIdentity,
    *,
    workspace_id: str | None,
    title: str | None,
    context: dict[str, Any] | None,
    execution: AgentExecutionSelection | None = None,
) -> AgentConversation:
    scope = await _resolve_workspace_scope(db, identity, workspace_id, context=context)
    require_permission(scope.access, WorkspacePermission.READ)
    title_value = title.strip() if title else None
    try:
        binding: dict[str, Any] = await validate_context_binding(
            db,
            scope.workspace_id,
            context,
            studio_workspace_id=scope.studio_workspace_id,
        )
        if title_value:
            _reject_unsafe_content(title_value)
    except AgentConversationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if execution is not None:
        selection = await validate_execution(
            db, identity, scope.access, execution, scope.workspace_id
        )
        binding = {**binding, EXECUTION_CONTEXT_KEY: selection.model_dump()}
    conversation = AgentConversation(
        workspace_id=scope.workspace_id,
        title=title_value,
        created_by_user_id=scope.access.user_id,
        context_binding=binding,
        status=AgentConversationStatus.ACTIVE.value,
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return conversation


async def list_conversations(
    db: AsyncSession,
    identity: RequestIdentity,
    *,
    workspace_id: str | None,
    limit: int,
    context: dict[str, Any] | None = None,
) -> list[AgentConversation]:
    scope = await _resolve_workspace_scope(db, identity, workspace_id, context=context)
    require_permission(scope.access, WorkspacePermission.READ)
    rows = await db.scalars(
        select(AgentConversation)
        .where(AgentConversation.workspace_id == scope.workspace_id)
        .order_by(AgentConversation.updated_at.desc())
        .limit(50 if scope.is_studio_bridge else limit)
    )
    conversations = list(rows)
    if not scope.is_studio_bridge:
        # Studio sessions share governed storage for the FK/RBAC boundary, but
        # their project context must never leak into ordinary workspace lists.
        return [
            conversation for conversation in conversations if not _is_studio_session(conversation)
        ]
    assert scope.studio_workspace_id is not None
    return [
        row
        for row in conversations
        if session_matches_studio_context(
            row.context_binding,
            studio_workspace_id=scope.studio_workspace_id,
            context=context or {},
        )
    ][:limit]


def _remote_dispatch(turn: AgentConversationTurn) -> dict[str, str] | None:
    metadata = turn.context_binding.get(REMOTE_DISPATCH_CONTEXT_KEY)
    if not isinstance(metadata, dict):
        return None
    request_id = metadata.get("request_id")
    agent_key = metadata.get("agent_key")
    if (
        not isinstance(request_id, str)
        or not 1 <= len(request_id) <= 64
        or not isinstance(agent_key, str)
        or re.fullmatch(r"[a-f0-9]{64}", agent_key) is None
    ):
        return None
    return {"request_id": request_id, "agent_key": agent_key}


async def _reconcile_remote_turn(
    db: AsyncSession,
    conversation: AgentConversation,
    turn: AgentConversationTurn,
    *,
    terminal: dict[str, Any] | None = None,
) -> None:
    if turn.status != AgentConversationTurnStatus.RUNNING.value or has_turn_runner(turn.id):
        return
    metadata = _remote_dispatch(turn)
    if metadata is None:
        return
    if terminal is None:
        terminal = ws_agent_manager.confirmed_agent_terminal(**metadata)
    if not ws_agent_manager._confirmed_agent_terminal(terminal, metadata["request_id"]):
        return
    cancelled = terminal.get("type") == "error" and terminal.get("error_type") == "CancelledError"
    await _finish_turn(
        db,
        conversation,
        turn,
        turn_status=AgentConversationTurnStatus.FAILED.value,
        tool_trace=turn.tool_trace,
        error_code=CANCELLED if cancelled else "remote_execution_reconciled",
        error_message=CANCELLED_MESSAGE
        if cancelled
        else "执行节点已确认退出；中断的回复未恢复，请重新发送。",
        remote_dispatch=metadata,
    )
    if turn.status != AgentConversationTurnStatus.RUNNING.value:
        ws_agent_manager.forget_agent_terminal(**metadata)


async def recover_conversation_turn(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    request_id: str,
    probe: Callable[[str, str], Awaitable[dict[str, Any]]],
) -> AgentConversationTurn:
    """Admin-only recovery using a trusted server probe, never a client exit assertion.

    The integrating server supplies a probe of the mapped node's exact dispatch.
    Missing tasks, timeouts, offline nodes and absent cleanup proof cannot unlock it.
    """
    if not is_platform_admin(identity):
        raise HTTPException(403, "Only platform administrators can recover remote execution")
    conversation, _ = await get_conversation(db, identity, conversation_id, limit=1)
    scope = await resolve_stored_agent_session_workspace(
        db,
        identity,
        workspace_id=conversation.workspace_id,
        context_binding=conversation.context_binding,
    )
    require_permission(scope.access, WorkspacePermission.RUN_OPERATIONS_AGENTS)
    turn = await db.scalar(
        select(AgentConversationTurn).where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.workspace_id == scope.workspace_id,
            AgentConversationTurn.request_id == request_id,
        )
    )
    if turn is None:
        raise HTTPException(404, "Conversation turn not found")
    if turn.status != AgentConversationTurnStatus.RUNNING.value:
        return turn
    metadata = _remote_dispatch(turn)
    if metadata is None or has_turn_runner(turn.id):
        raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE)
    from backend.services.agent_native_chat import configured_binding

    execution = stored_execution(conversation.context_binding)
    binding = configured_binding(scope.workspace_id, execution.runtime_id)
    if (
        binding.revision != execution.binding_revision
        or ws_agent_manager.agent_task_key(binding.agent_url) != metadata["agent_key"]
    ):
        raise HTTPException(409, "Native binding changed; remote cleanup cannot be verified")
    await db.commit()
    try:
        async with asyncio.timeout(REMOTE_PROBE_TIMEOUT_SECONDS):
            terminal = await probe(binding.agent_url, metadata["request_id"])
    except Exception as exc:
        raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE) from exc
    if not ws_agent_manager._confirmed_agent_terminal(terminal, metadata["request_id"]):
        raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE)
    await await_cleanup(_reconcile_remote_turn(db, conversation, turn, terminal=terminal))
    if turn.status == AgentConversationTurnStatus.RUNNING.value:
        raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE)
    return turn


async def get_conversation(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 50,
    for_update: bool = False,
    latest: bool = False,
) -> tuple[AgentConversation, list[AgentConversationTurn]]:
    statement = select(AgentConversation).where(AgentConversation.id == conversation_id)
    if for_update:
        statement = statement.with_for_update()
    conversation = await db.scalar(statement)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent conversation not found")
    scope = await resolve_stored_agent_session_workspace(
        db,
        identity,
        workspace_id=conversation.workspace_id,
        context_binding=conversation.context_binding,
    )
    require_permission(scope.access, WorkspacePermission.READ)
    stored_execution(conversation.context_binding)
    if latest and after_sequence > 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "latest cannot be combined with after_sequence greater than zero",
        )
    try:
        await validate_context_binding(
            db,
            scope.workspace_id,
            {
                key: value
                for key, value in conversation.context_binding.items()
                if key != EXECUTION_CONTEXT_KEY
            },
            studio_workspace_id=scope.studio_workspace_id,
            allow_stored_studio_workspace=True,
        )
    except AgentConversationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    running = await db.scalar(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.workspace_id == scope.workspace_id,
            AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
        )
        .order_by(AgentConversationTurn.sequence.desc())
        .limit(1)
    )
    if running is not None:
        await await_cleanup(_reconcile_remote_turn(db, conversation, running))
    turns = await db.scalars(
        select(AgentConversationTurn)
        .where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.sequence > after_sequence,
        )
        .order_by(
            AgentConversationTurn.sequence.desc()
            if latest
            else AgentConversationTurn.sequence.asc()
        )
        .limit(limit)
    )
    ordered_turns = list(turns)
    if latest:
        ordered_turns.reverse()
    return conversation, ordered_turns


async def close_conversation(
    db: AsyncSession, identity: RequestIdentity, conversation_id: str
) -> AgentConversation:
    conversation, _ = await get_conversation(
        db, identity, conversation_id, limit=1, for_update=True
    )
    terminal = await db.scalar(
        select(AgentTerminalSession).where(
            AgentTerminalSession.conversation_id == conversation.id
        )
    )
    if terminal is not None and not (
        terminal.status == "exited" and terminal.cleanup_confirmed
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Terminal cleanup must be confirmed before closing the conversation",
        )
    if conversation.status != AgentConversationStatus.CLOSED.value:
        conversation.status = AgentConversationStatus.CLOSED.value
        conversation.revision += 1
        await db.commit()
        await db.refresh(conversation)
    return conversation


async def cancel_conversation_turn(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    request_id: str,
) -> bool:
    if not request_id.strip() or len(request_id) > 64:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "request_id must be 1..64 characters"
        )
    conversation = await db.scalar(
        select(AgentConversation).where(AgentConversation.id == conversation_id)
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent conversation not found")
    scope = await resolve_stored_agent_session_workspace(
        db,
        identity,
        workspace_id=conversation.workspace_id,
        context_binding=conversation.context_binding,
    )
    require_permission(scope.access, WorkspacePermission.RUN_OPERATIONS_AGENTS)
    execution = stored_execution(conversation.context_binding)
    pending = await db.scalar(
        select(AgentConversationTurn).where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.workspace_id == scope.workspace_id,
            AgentConversationTurn.request_id == request_id,
        )
    )
    if pending is None or pending.status != AgentConversationTurnStatus.RUNNING.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Requested conversation turn is not running")
    if pending.error_code == AgentTaskUnresolvedError.code or not has_turn_runner(pending.id):
        await await_cleanup(_reconcile_remote_turn(db, conversation, pending))
        if pending.status != AgentConversationTurnStatus.RUNNING.value:
            return True
        if execution.runtime_id == "opencli" or _remote_dispatch(pending) is None:
            raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE)
        await recover_conversation_turn(
            db,
            identity,
            conversation_id,
            request_id=request_id,
            probe=ws_agent_manager.probe_agent_task,
        )
        return True
    turn_id = await db.scalar(
        update(AgentConversationTurn)
        .where(
            AgentConversationTurn.conversation_id == conversation.id,
            AgentConversationTurn.workspace_id == scope.workspace_id,
            AgentConversationTurn.request_id == request_id,
            AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
            (AgentConversationTurn.error_code.is_(None))
            | (AgentConversationTurn.error_code != AgentTaskUnresolvedError.code),
        )
        .values(error_code=CANCEL_REQUESTED)
        .returning(AgentConversationTurn.id)
        .execution_options(synchronize_session=False)
    )
    if turn_id is None:
        unresolved = await db.scalar(
            select(AgentConversationTurn.id).where(
                AgentConversationTurn.conversation_id == conversation_id,
                AgentConversationTurn.request_id == request_id,
                AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
                AgentConversationTurn.error_code == AgentTaskUnresolvedError.code,
            )
        )
        if unresolved is not None:
            raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE)
        raise HTTPException(status.HTTP_409_CONFLICT, "Requested conversation turn is not running")
    await db.commit()
    return True


async def _finish_turn(
    db: AsyncSession,
    conversation: AgentConversation,
    turn: AgentConversationTurn,
    *,
    turn_status: str,
    tool_trace: list[dict[str, Any]],
    response: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    remote_dispatch: dict[str, str] | None = None,
) -> tuple[AgentConversation, AgentConversationTurn]:
    correlation = []
    if remote_dispatch is not None:
        correlation = [
            AgentConversationTurn.context_binding[REMOTE_DISPATCH_CONTEXT_KEY][key].as_string()
            == value
            for key, value in remote_dispatch.items()
        ]
    finished_id = await db.scalar(
        update(AgentConversationTurn)
        .where(
            AgentConversationTurn.id == turn.id,
            AgentConversationTurn.conversation_id == conversation.id,
            AgentConversationTurn.workspace_id == conversation.workspace_id,
            AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
            *correlation,
        )
        .values(
            status=turn_status,
            response=response,
            tool_trace=tool_trace,
            error_code=error_code,
            error_message=error_message,
        )
        .returning(AgentConversationTurn.id)
        .execution_options(synchronize_session=False)
    )
    if finished_id is not None:
        await db.execute(
            update(AgentConversation)
            .where(AgentConversation.id == conversation.id)
            .values(revision=AgentConversation.revision + 1)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    await db.refresh(turn)
    await db.refresh(conversation)
    return conversation, turn


async def _insert_running_turn(
    db: AsyncSession,
    conversation: AgentConversation,
    request_id: str,
    content: str,
    binding: dict[str, str],
) -> tuple[AgentConversationTurn | None, AgentConversationTurn | None]:
    """Claim the conversation revision and insert at most one running turn atomically."""
    conversation_id = conversation.id
    workspace_id = conversation.workspace_id
    running = (
        select(AgentConversationTurn.id)
        .where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
        )
        .exists()
    )
    duplicate = select(AgentConversationTurn).where(
        AgentConversationTurn.conversation_id == conversation_id,
        AgentConversationTurn.request_id == request_id,
    )
    for _attempt in range(2):
        existing = await db.scalar(duplicate)
        if existing is not None:
            return None, existing
        revision = await db.scalar(
            select(AgentConversation.revision).where(AgentConversation.id == conversation_id)
        )
        claimed = (
            await db.execute(
                update(AgentConversation)
                .where(
                    AgentConversation.id == conversation_id,
                    AgentConversation.workspace_id == workspace_id,
                    AgentConversation.status == AgentConversationStatus.ACTIVE.value,
                    AgentConversation.revision == revision,
                    ~running,
                )
                .values(revision=AgentConversation.revision + 1)
                .returning(AgentConversation.revision, AgentConversation.updated_at)
                .execution_options(synchronize_session=False)
            )
        ).one_or_none()
        if claimed is None:
            await db.rollback()
            await db.refresh(conversation)
            existing = await db.scalar(duplicate)
            if existing is not None:
                return None, existing
            if await db.scalar(select(running)):
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "conversation already has a running turn"
                )
            if conversation.status != AgentConversationStatus.ACTIVE.value:
                raise HTTPException(status.HTTP_409_CONFLICT, "Agent conversation is closed")
            continue
        sequence = (
            await db.scalar(
                select(func.max(AgentConversationTurn.sequence)).where(
                    AgentConversationTurn.conversation_id == conversation_id
                )
            )
            or 0
        ) + 1
        turn = AgentConversationTurn(
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            sequence=sequence,
            request_id=request_id,
            user_content=content,
            context_binding=binding,
            tool_trace=[],
            status=AgentConversationTurnStatus.RUNNING.value,
        )
        db.add(turn)
        try:
            await db.commit()
            set_committed_value(conversation, "revision", claimed.revision)
            set_committed_value(conversation, "updated_at", claimed.updated_at)
            return turn, None
        except IntegrityError:
            await db.rollback()
            await db.refresh(conversation)
            existing = await db.scalar(duplicate)
            if existing is not None:
                return None, existing
    raise HTTPException(status.HTTP_409_CONFLICT, "Could not allocate conversation turn")


async def _model_session(db: AsyncSession) -> AsyncSession:
    bind = db.bind
    if bind is None:
        raise RuntimeError("conversation database session has no bind")
    return async_sessionmaker(bind=bind, expire_on_commit=False)()


async def send_message(
    db: AsyncSession,
    identity: RequestIdentity,
    conversation_id: str,
    *,
    request_id: str,
    content: str,
    context: dict[str, Any] | None,
    chat_runner: Callable[..., Any] | None = None,
    is_disconnected: Callable[[], Awaitable[bool]] | None = None,
) -> tuple[AgentConversation, AgentConversationTurn]:
    if not request_id.strip() or len(request_id) > 64:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "request_id must be 1..64 characters"
        )
    try:
        _reject_unsafe_content(content)
    except AgentConversationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    conversation = await db.scalar(
        select(AgentConversation).where(AgentConversation.id == conversation_id).with_for_update()
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent conversation not found")
    scope = await resolve_stored_agent_session_workspace(
        db,
        identity,
        workspace_id=conversation.workspace_id,
        context_binding=conversation.context_binding,
    )
    require_permission(scope.access, WorkspacePermission.READ)
    existing = await db.scalar(
        select(AgentConversationTurn).where(
            AgentConversationTurn.conversation_id == conversation_id,
            AgentConversationTurn.request_id == request_id,
        )
    )
    if existing is not None:
        if existing.status == AgentConversationTurnStatus.RUNNING.value:
            raise HTTPException(status.HTTP_409_CONFLICT, "conversation turn is already running")
        if existing.status == AgentConversationTurnStatus.FAILED.value:
            raise HTTPException(status.HTTP_409_CONFLICT, "conversation turn previously failed")
        return conversation, existing
    if conversation.status != AgentConversationStatus.ACTIVE.value:
        raise HTTPException(status.HTTP_409_CONFLICT, "Agent conversation is closed")

    stored = stored_execution(conversation.context_binding)
    if stored.mode == "terminal":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Terminal conversations accept input only through the terminal channel",
        )
    execution = await validate_execution(
        db,
        identity,
        scope.access,
        stored,
        scope.workspace_id,
    )
    stored_context = {
        key: value
        for key, value in conversation.context_binding.items()
        if key != EXECUTION_CONTEXT_KEY
    }
    try:
        binding = await validate_context_binding(
            db,
            scope.workspace_id,
            context if context is not None else stored_context,
            studio_workspace_id=scope.studio_workspace_id,
            allow_stored_studio_workspace=context is None,
        )
        if scope.is_studio_bridge:
            _ensure_studio_context_continuity(conversation.context_binding, binding)
    except AgentConversationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    history_rows = list(
        await db.scalars(
            select(AgentConversationTurn)
            .where(
                AgentConversationTurn.conversation_id == conversation_id,
                AgentConversationTurn.status.in_(
                    (
                        AgentConversationTurnStatus.COMPLETED.value,
                        AgentConversationTurnStatus.PROPOSAL.value,
                    )
                ),
            )
            .order_by(AgentConversationTurn.sequence.desc())
            .limit(MAX_HISTORY_TURNS)
        )
    )
    history_rows.reverse()
    turn: AgentConversationTurn | None = None
    turn_id: str | None = None
    model_db: AsyncSession | None = None
    trace: list[dict[str, Any]] = []

    async def insert_turn():
        nonlocal turn, existing, turn_id
        turn, existing = await _insert_running_turn(db, conversation, request_id, content, binding)
        if turn is not None:
            turn_id = turn.id
            register_turn_runner(turn_id)

    async def persist_dispatch(agent_url: str, remote_request_id: str):
        assert turn_id is not None

        async def save():
            async with async_sessionmaker(bind=db.bind, expire_on_commit=False)() as dispatch_db:
                saved = await dispatch_db.scalar(
                    update(AgentConversationTurn)
                    .where(
                        AgentConversationTurn.id == turn_id,
                        AgentConversationTurn.conversation_id == conversation_id,
                        AgentConversationTurn.workspace_id == scope.workspace_id,
                        AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
                    )
                    .values(
                        context_binding={
                            **binding,
                            REMOTE_DISPATCH_CONTEXT_KEY: {
                                "agent_key": ws_agent_manager.agent_task_key(agent_url),
                                "request_id": remote_request_id,
                            },
                        }
                    )
                    .returning(AgentConversationTurn.id)
                )
                if saved is None:
                    raise RuntimeError("Remote dispatch has no running conversation turn")
                await dispatch_db.commit()

        await await_cleanup(save())

    async def invoke_runner():
        assert model_db is not None and turn_id is not None
        if execution.runtime_id != "opencli":
            from backend.services.agent_native_chat import (
                require_native_binding,
                run_native_chat_request,
            )

            native_binding = await require_native_binding(
                model_db,
                identity,
                scope.access,
                scope.workspace_id,
                execution.runtime_id,
                execution.binding_revision,
            )
            with ws_agent_manager.observe_agent_dispatch(persist_dispatch):
                return await run_native_chat_request(
                    model_db,
                    body,
                    identity,
                    binding=native_binding,
                    tool_trace=trace,
                    proposal_provenance=chat.ProposalProvenance(
                        conversation_id=conversation.id,
                        turn_id=turn_id,
                        context=binding,
                    ),
                )
        runner = chat_runner or chat.run_chat_request
        return await runner(
            model_db,
            body,
            identity,
            tool_trace=trace,
            proposal_provenance=chat.ProposalProvenance(
                conversation_id=conversation.id,
                turn_id=turn_id,
                context=binding,
            ),
        )

    async def finish_failure(error_code: str, error_message: str):
        if model_db is not None:
            await model_db.rollback()
        await db.rollback()
        if turn_id is None:
            return None
        stored_conversation = await db.get(AgentConversation, conversation_id)
        stored_turn = await db.get(AgentConversationTurn, turn_id)
        assert stored_conversation is not None and stored_turn is not None
        return await _finish_turn(
            db,
            stored_conversation,
            stored_turn,
            turn_status=AgentConversationTurnStatus.FAILED.value,
            tool_trace=trace,
            error_code=error_code,
            error_message=error_message,
        )

    try:
        await await_cleanup(insert_turn())
        if existing is not None:
            if existing.status == AgentConversationTurnStatus.RUNNING.value:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, "conversation turn is already running"
                )
            if existing.status == AgentConversationTurnStatus.FAILED.value:
                raise HTTPException(status.HTTP_409_CONFLICT, "conversation turn previously failed")
            return conversation, existing
        assert turn is not None
        await db.refresh(turn)
        body = chat.ChatRequest(
            messages=bounded_history(history_rows, content),
            workspace_id=conversation.workspace_id,
            context=binding,
            provider_id=execution.provider_id,
            model_id=execution.model_id,
        )
        model_db = await _model_session(db)
        await db.commit()
        result = await run_with_cancellation(
            invoke_runner(),
            async_sessionmaker(bind=db.bind, expire_on_commit=False),
            conversation_id=conversation.id,
            workspace_id=conversation.workspace_id,
            turn_id=turn.id,
            is_disconnected=is_disconnected,
        )
        reply = result.data if isinstance(result, chat.ApiResponse) else result
        if not isinstance(reply, chat.ChatReply):
            raise RuntimeError("chat runner returned an invalid reply")
        if reply.type == "proposal":
            proposal = reply.proposal
            if (
                proposal is None
                or proposal.workspace_id != conversation.workspace_id
                or not proposal.work_item_id
                or not proposal.proposal_version
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Agent proposal is not bound to the conversation Workspace",
                )

        async def finish_reply():
            await model_db.commit()
            return await _finish_turn(
                db,
                conversation,
                turn,
                turn_status=(
                    AgentConversationTurnStatus.PROPOSAL.value
                    if reply.type == "proposal"
                    else AgentConversationTurnStatus.COMPLETED.value
                ),
                response=_safe_response(reply),
                tool_trace=trace,
            )

        return await await_cleanup(finish_reply())
    except AgentTaskUnresolvedError as exc:

        async def mark_unresolved(error: AgentTaskUnresolvedError):
            if model_db is not None:
                await model_db.rollback()
            await db.rollback()
            if turn_id is not None:
                await db.execute(
                    update(AgentConversationTurn)
                    .where(
                        AgentConversationTurn.id == turn_id,
                        AgentConversationTurn.status == AgentConversationTurnStatus.RUNNING.value,
                    )
                    .values(
                        context_binding={
                            **binding,
                            REMOTE_DISPATCH_CONTEXT_KEY: {
                                "agent_key": ws_agent_manager.agent_task_key(error.agent_url),
                                "request_id": error.request_id,
                            },
                        },
                        error_code=error.code,
                        error_message=REMOTE_UNCONFIRMED_MESSAGE,
                    )
                )
                await db.commit()

        await await_cleanup(mark_unresolved(exc), propagate_cancellation=False)
        raise HTTPException(503, REMOTE_UNCONFIRMED_MESSAGE) from exc
    except ConversationTurnCancelledError:
        result = await await_cleanup(finish_failure(CANCELLED, CANCELLED_MESSAGE))
        assert result is not None
        return result
    except asyncio.CancelledError:
        await await_cleanup(
            finish_failure(CANCELLED, CANCELLED_MESSAGE), propagate_cancellation=False
        )
        raise
    except (LlmAdapterError, ResolverError) as exc:
        await await_cleanup(
            finish_failure(
                "model_unavailable"
                if isinstance(exc, LlmAdapterError) and exc.retryable
                else "model_error",
                _redact_error(str(exc)),
            )
        )
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "模型调用失败") from exc
    except HTTPException as exc:
        await await_cleanup(finish_failure("model_error", _redact_error(str(exc.detail))))
        raise
    except Exception as exc:
        await await_cleanup(finish_failure("model_error", _redact_error(str(exc))))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "模型调用失败") from exc
    finally:
        try:
            if model_db is not None:
                await await_cleanup(model_db.close())
        finally:
            if turn_id is not None:
                unregister_turn_runner(turn_id)
