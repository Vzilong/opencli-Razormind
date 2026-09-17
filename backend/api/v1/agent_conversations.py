"""REST and browser WebSocket API for persistent Global Agent conversations."""

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend import ws_agent_manager
from backend.database import AsyncSessionLocal, get_db
from backend.schemas.agent_conversation import (
    AgentChatOptions,
    AgentConversationCreate,
    AgentConversationDetail,
    AgentConversationMessageCreate,
    AgentConversationMessageRead,
    AgentConversationRead,
    AgentConversationTurnRead,
    AgentTerminalSessionRead,
    AgentTerminalStart,
    AgentTerminalTicketRead,
)
from backend.schemas.common import ApiResponse
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_conversation_service as service
from backend.services.agent_chat_options import get_chat_options
from backend.services import agent_native_chat

router = APIRouter(prefix="/chat", tags=["agent-conversations"])
logger = logging.getLogger(__name__)


class AgentConversationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=64)


class AgentConversationCancelRead(BaseModel):
    accepted: bool


@router.post(
    "/sessions/{conversation_id}/terminal",
    response_model=ApiResponse[AgentTerminalSessionRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_terminal_session(
    conversation_id: str,
    body: AgentTerminalStart,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    terminal = await agent_native_chat.create_terminal_session(
        db, identity, conversation_id, body
    )
    return ApiResponse.ok(AgentTerminalSessionRead.model_validate(terminal))


@router.get(
    "/sessions/{conversation_id}/terminal",
    response_model=ApiResponse[AgentTerminalSessionRead],
)
async def get_terminal_session(
    conversation_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    terminal, _binding = await agent_native_chat.get_terminal_session(
        db, identity, conversation_id
    )
    return ApiResponse.ok(AgentTerminalSessionRead.model_validate(terminal))


@router.post(
    "/sessions/{conversation_id}/terminal/stop",
    response_model=ApiResponse[AgentTerminalSessionRead],
)
async def stop_terminal_session(
    conversation_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    terminal = await agent_native_chat.stop_terminal_session(
        db, identity, conversation_id
    )
    return ApiResponse.ok(AgentTerminalSessionRead.model_validate(terminal))


@router.post(
    "/sessions/{conversation_id}/terminal/ticket",
    response_model=ApiResponse[AgentTerminalTicketRead],
)
async def create_terminal_ticket(
    conversation_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    terminal, _binding = await agent_native_chat.get_terminal_session(
        db, identity, conversation_id, refresh=False
    )
    return ApiResponse.ok(
        AgentTerminalTicketRead(
            ticket=agent_native_chat.issue_terminal_ticket(identity, terminal),
            expires_in=60,
        )
    )


@router.websocket("/terminal/ws")
async def terminal_websocket(ws: WebSocket) -> None:
    ticket = ws.query_params.get("ticket", "")
    controller_id = ws.query_params.get("controller", "")
    takeover = ws.query_params.get("takeover") == "1"
    await ws.accept()
    try:
        identity, terminal_session_id, terminal_revision = (
            agent_native_chat.consume_terminal_ticket(ticket)
        )
        async with AsyncSessionLocal() as db:
            _terminal, binding = await agent_native_chat.claim_terminal_ticket(
                db, identity, terminal_session_id, terminal_revision
            )
        attachment = await ws_agent_manager.attach_native_terminal(
            binding.agent_url,
            terminal_session_id,
            controller_id,
            takeover=takeover,
            native_chat_authorized=True,
        )
    except (HTTPException, PermissionError, RuntimeError, ValueError) as exc:
        logger.warning("Native terminal attach denied: %s", type(exc).__name__)
        await ws.close(code=4403, reason="Terminal access denied")
        return

    async def send_output() -> None:
        while True:
            item = await attachment.queue.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                if item.get("type") == "exit":
                    try:
                        async with AsyncSessionLocal() as db:
                            await agent_native_chat.record_terminal_event(
                                db, identity, terminal_session_id, item
                            )
                    except (HTTPException, PermissionError, RuntimeError, ValueError):
                        logger.warning("Native terminal exit persistence failed")
                await ws.send_json(item)

    sender = asyncio.create_task(send_output())
    try:
        while True:
            packet = await ws.receive()
            if packet["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(packet.get("code", 1000))
            async with AsyncSessionLocal() as db:
                terminal, _binding = await agent_native_chat.authorize_terminal_action(
                    db, identity, terminal_session_id
                )
            if terminal.status not in {"active", "stopping", "exited"}:
                await ws.close(code=4404, reason="Terminal session is unavailable")
                return
            binary = packet.get("bytes")
            if isinstance(binary, bytes):
                if not 0 < len(binary) <= 64 * 1024 or terminal.status != "active":
                    await ws.close(code=4400, reason="Malformed terminal input")
                    return
                await ws_agent_manager.send_native_terminal_input(attachment, binary)
                continue
            text = packet.get("text")
            if not isinstance(text, str) or len(text) > 4096:
                await ws.close(code=4400, reason="Malformed terminal control")
                return
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                control = None
            if not isinstance(control, dict):
                await ws.close(code=4400, reason="Malformed terminal control")
                return
            if control.get("type") == "resize":
                cols = control.get("cols")
                rows = control.get("rows")
                if (
                    not isinstance(cols, int)
                    or isinstance(cols, bool)
                    or not isinstance(rows, int)
                    or isinstance(rows, bool)
                    or not 1 <= cols <= 1000
                    or not 1 <= rows <= 1000
                ):
                    await ws.close(code=4400, reason="Malformed terminal resize")
                    return
                await ws_agent_manager.resize_native_terminal(
                    attachment, cols=cols, rows=rows
                )
            elif control.get("type") == "takeover":
                result = await ws_agent_manager.takeover_native_terminal(
                    attachment, controller_id
                )
                await ws.send_json(
                    {"type": "takeover", "controls": result.get("controls") is True}
                )
            else:
                await ws.close(code=4400, reason="Unsupported terminal control")
                return
    except WebSocketDisconnect:
        return
    except (HTTPException, PermissionError, RuntimeError, ValueError):
        await ws.close(code=4403, reason="Terminal access denied")
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        await ws_agent_manager.detach_native_terminal(attachment)


@router.get("/options", response_model=ApiResponse[AgentChatOptions])
async def chat_options(
    workspace_id: str = Query(min_length=1, max_length=36),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    return ApiResponse.ok(await get_chat_options(db, identity, workspace_id))


def _detail(conversation: object, turns: list[object]) -> AgentConversationDetail:
    data = AgentConversationRead.model_validate(conversation).model_dump()
    data["turns"] = [AgentConversationTurnRead.model_validate(turn) for turn in turns]
    return AgentConversationDetail.model_validate(data)


@router.post(
    "/sessions",
    response_model=ApiResponse[AgentConversationRead],
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: AgentConversationCreate,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    conversation = await service.create_conversation(
        db,
        identity,
        workspace_id=body.workspace_id,
        title=body.title,
        context=body.context,
        execution=body.execution,
    )
    return ApiResponse.ok(AgentConversationRead.model_validate(conversation))


@router.get("/sessions", response_model=ApiResponse[list[AgentConversationRead]])
async def list_sessions(
    workspace_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None, min_length=1, max_length=255),
    workflow_id: str | None = Query(default=None, min_length=1, max_length=255),
    run_id: str | None = Query(default=None, min_length=1, max_length=255),
    limit: int = Query(default=20, ge=1, le=50),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    context = {
        key: value
        for key, value in {
            "project_id": project_id,
            "workflow_id": workflow_id,
            "run_id": run_id,
        }.items()
        if value is not None
    }
    rows = await service.list_conversations(
        db,
        identity,
        workspace_id=workspace_id,
        limit=limit,
        context=context,
    )
    return ApiResponse.ok([AgentConversationRead.model_validate(row) for row in rows])


async def _get_detail(
    conversation_id: str,
    after_sequence: int,
    limit: int,
    identity: RequestIdentity,
    db: AsyncSession,
    latest: bool = False,
) -> AgentConversationDetail:
    conversation, turns = await service.get_conversation(
        db,
        identity,
        conversation_id,
        after_sequence=after_sequence,
        limit=limit,
        latest=latest,
    )
    return _detail(conversation, turns)


@router.get(
    "/sessions/{conversation_id}",
    response_model=ApiResponse[AgentConversationDetail],
)
async def get_session(
    conversation_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
    latest: bool = False,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    return ApiResponse.ok(
        await _get_detail(conversation_id, after_sequence, limit, identity, db, latest=latest)
    )


@router.get(
    "/sessions/{conversation_id}/replay",
    response_model=ApiResponse[AgentConversationDetail],
)
async def replay_session(
    conversation_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=50),
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    return ApiResponse.ok(await _get_detail(conversation_id, after_sequence, limit, identity, db))


@router.post(
    "/sessions/{conversation_id}/messages",
    response_model=ApiResponse[AgentConversationMessageRead],
)
async def send_session_message(
    conversation_id: str,
    body: AgentConversationMessageCreate,
    request: Request,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    conversation, turn = await service.send_message(
        db,
        identity,
        conversation_id,
        request_id=body.request_id,
        content=body.content,
        context=body.context,
        is_disconnected=request.is_disconnected,
    )
    return ApiResponse.ok(
        AgentConversationMessageRead(
            conversation_id=conversation.id,
            turn=AgentConversationTurnRead.model_validate(turn),
        )
    )


@router.post(
    "/sessions/{conversation_id}/cancel",
    response_model=ApiResponse[AgentConversationCancelRead],
)
async def cancel_session_turn(
    conversation_id: str,
    body: AgentConversationCancelRequest,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    accepted = await service.cancel_conversation_turn(
        db, identity, conversation_id, request_id=body.request_id
    )
    return ApiResponse.ok(AgentConversationCancelRead(accepted=accepted))


@router.post(
    "/sessions/{conversation_id}/close",
    response_model=ApiResponse[AgentConversationRead],
)
async def close_session(
    conversation_id: str,
    identity: RequestIdentity = Depends(get_request_identity),
    db: AsyncSession = Depends(get_db),
) -> ApiResponse:
    conversation = await service.close_conversation(db, identity, conversation_id)
    return ApiResponse.ok(AgentConversationRead.model_validate(conversation))
