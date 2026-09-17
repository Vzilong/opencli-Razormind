import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from backend.config import get_settings
from backend.main import app
from backend.models.agent_conversation import AgentConversationTurn, AgentTerminalSession
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_native_chat
from backend.api.v1 import agent_conversations as agent_conversation_api


@pytest.fixture
async def terminal_setup(db_session, monkeypatch):
    user = User(subject="terminal-admin")
    workspace = Workspace(name="Terminal", slug="terminal")
    other = Workspace(name="Other terminal", slug="other-terminal")
    node = EdgeNode(
        url="http://terminal-node:19823",
        protocol="ws",
        status="online",
        runtimes=["codex", "omp"],
        runtime_capabilities={"codex": ["operator_chat"], "omp": ["operator_chat"]},
    )
    db_session.add_all([user, workspace, other, node])
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(
            user_id=user.id,
            workspace_id=workspace.id,
            role=WorkspaceRole.ADMIN,
        )
    )
    await db_session.commit()
    mappings = [
        {
            "workspace_id": workspace.id,
            "runtime_id": runtime,
            "agent_url": node.url,
            "cwd": "/isolated/empty-work",
        }
        for runtime in ("codex", "omp")
    ]
    monkeypatch.setattr(get_settings(), "native_chat_bindings", mappings)
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "is_connected", lambda url: url == node.url)
    identity = RequestIdentity(subject=user.subject, is_platform_admin=True)
    app.dependency_overrides[get_request_identity] = lambda: identity
    yield SimpleNamespace(
        user=user,
        workspace=workspace,
        other=other,
        node=node,
        mappings=mappings,
        identity=identity,
    )
    app.dependency_overrides.pop(get_request_identity, None)


async def _create_terminal_conversation(client, setup, runtime="codex") -> str:
    response = await client.post(
        "/api/v1/chat/sessions",
        json={
            "workspace_id": setup.workspace.id,
            "execution": {
                "runtime_id": runtime,
                "access_mode": "native",
                "mode": "terminal",
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


@pytest.mark.parametrize("runtime", ["codex", "omp"])
async def test_terminal_create_persists_mapping_and_never_uses_gui_turns(
    client, db_session, terminal_setup, monkeypatch, runtime
):
    start = AsyncMock(
        return_value={"status": "active", "exit_code": None, "cleanup_complete": False}
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "start_native_terminal", start)
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    conversation_id = await _create_terminal_conversation(client, terminal_setup, runtime)

    response = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "explain this workspace", "cols": 120, "rows": 36},
    )

    assert response.status_code == 201, response.text
    terminal = response.json()["data"]
    assert terminal["runtime_id"] == runtime
    assert terminal["status"] == "active"
    assert terminal["conversation_id"] == conversation_id
    assert terminal_setup.node.url not in response.text
    assert "/isolated/empty-work" not in response.text
    start.assert_awaited_once()
    assert start.call_args.args[0] == terminal_setup.node.url
    assert start.call_args.kwargs["session_id"] == terminal["id"]
    assert start.call_args.kwargs["native_chat_authorized"] is True

    gui_message = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/messages",
        json={"request_id": "must-not-run", "content": "fake a terminal reply"},
    )
    assert gui_message.status_code == 409
    dispatch.assert_not_called()
    assert await db_session.scalar(select(func.count(AgentConversationTurn.id))) == 0

    close = await client.post(f"/api/v1/chat/sessions/{conversation_id}/close")
    assert close.status_code == 409


async def test_terminal_failed_start_leaves_no_attachable_mapping(
    client, db_session, terminal_setup, monkeypatch
):
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager,
        "start_native_terminal",
        AsyncMock(return_value={"status": "failed"}),
    )
    conversation_id = await _create_terminal_conversation(client, terminal_setup)

    response = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "start"},
    )

    assert response.status_code == 409
    assert await db_session.scalar(select(func.count(AgentTerminalSession.id))) == 0


@pytest.mark.parametrize("denial", ["nonadmin", "outsider", "changed-binding"])
async def test_terminal_create_revalidates_admin_workspace_and_binding(
    client, terminal_setup, monkeypatch, denial
):
    conversation_id = await _create_terminal_conversation(client, terminal_setup)
    start = AsyncMock()
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "start_native_terminal", start)
    if denial == "nonadmin":
        app.dependency_overrides[get_request_identity] = lambda: RequestIdentity(
            subject=terminal_setup.user.subject
        )
    elif denial == "outsider":
        app.dependency_overrides[get_request_identity] = lambda: RequestIdentity(
            subject="terminal-outsider", is_platform_admin=True
        )
    else:
        terminal_setup.mappings[0]["cwd"] = "/changed-binding"

    response = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "start"},
    )

    assert response.status_code in {403, 409}
    assert terminal_setup.node.url not in response.text
    assert "/changed-binding" not in response.text
    start.assert_not_called()


async def test_terminal_ticket_is_short_lived_bound_and_single_use(
    client, db_session, terminal_setup, monkeypatch
):
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager,
        "start_native_terminal",
        AsyncMock(return_value={"status": "active", "cleanup_complete": False}),
    )
    conversation_id = await _create_terminal_conversation(client, terminal_setup)
    created = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "start"},
    )
    terminal_id = created.json()["data"]["id"]

    response = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal/ticket"
    )

    assert response.status_code == 200
    assert response.json()["data"]["expires_in"] == 60
    identity, decoded_terminal_id, decoded_revision = agent_native_chat.consume_terminal_ticket(
        response.json()["data"]["ticket"]
    )
    assert decoded_terminal_id == terminal_id
    assert identity.subject == terminal_setup.identity.subject
    assert identity.is_platform_admin is True
    assert decoded_revision == created.json()["data"]["revision"]
    await agent_native_chat.claim_terminal_ticket(
        db_session,
        identity,
        decoded_terminal_id,
        decoded_revision,
    )
    with pytest.raises(HTTPException) as replay:
        await agent_native_chat.claim_terminal_ticket(
            db_session,
            identity,
            decoded_terminal_id,
            decoded_revision,
        )
    assert replay.value.status_code == 403


async def test_terminal_stop_stays_unconfirmed_until_node_cleanup_proof(
    client, terminal_setup, monkeypatch
):
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager,
        "start_native_terminal",
        AsyncMock(return_value={"status": "active", "cleanup_complete": False}),
    )
    stop = AsyncMock(side_effect=TimeoutError("node still cleaning"))
    query = AsyncMock(
        return_value={"status": "exited", "exit_code": 130, "cleanup_complete": True}
    )
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "stop_native_terminal", stop)
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "query_native_terminal", query)
    conversation_id = await _create_terminal_conversation(client, terminal_setup)
    await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "start"},
    )

    stopping = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal/stop"
    )
    assert stopping.status_code == 503
    assert "清理尚未确认" in stopping.text

    recovered = await client.get(
        f"/api/v1/chat/sessions/{conversation_id}/terminal"
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["data"]["status"] == "exited"
    assert recovered.json()["data"]["cleanup_confirmed"] is True
    assert recovered.json()["data"]["exit_code"] == 130
    query.assert_awaited_once()


async def test_terminal_exit_event_is_durable_when_node_goes_offline(
    client, db_session, terminal_setup, monkeypatch
):
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager,
        "start_native_terminal",
        AsyncMock(return_value={"status": "active", "cleanup_complete": False}),
    )
    conversation_id = await _create_terminal_conversation(client, terminal_setup)
    created = await client.post(
        f"/api/v1/chat/sessions/{conversation_id}/terminal",
        json={"initial_input": "start"},
    )
    terminal_id = created.json()["data"]["id"]

    await agent_native_chat.record_terminal_event(
        db_session,
        terminal_setup.identity,
        terminal_id,
        {
            "type": "exit",
            "status": "exited",
            "exit_code": 0,
            "cleanup_complete": True,
        },
    )
    terminal_setup.node.status = "offline"
    await db_session.commit()
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "is_connected", lambda url: False)

    response = await client.get(f"/api/v1/chat/sessions/{conversation_id}/terminal")

    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "exited"
    assert response.json()["data"]["cleanup_confirmed"] is True
    assert response.json()["data"]["exit_code"] == 0


async def test_terminal_websocket_accepts_before_attach_and_detaches(monkeypatch):
    terminal_id = "11111111-1111-4111-8111-111111111111"
    events = []
    attachment = SimpleNamespace(queue=asyncio.Queue())
    identity = RequestIdentity(subject="terminal-admin", is_platform_admin=True)

    class Socket:
        query_params = {
            "ticket": "signed-ticket",
            "controller": "22222222-2222-4222-8222-222222222222",
        }

        async def accept(self):
            events.append("accept")

        async def close(self, **kwargs):
            events.append(("close", kwargs))

        async def receive(self):
            return {"type": "websocket.disconnect", "code": 1000}

        async def send_bytes(self, payload):
            events.append(("bytes", payload))

        async def send_json(self, payload):
            events.append(("json", payload))

    async def attach(*args, **kwargs):
        del args, kwargs
        events.append("attach")
        return attachment

    monkeypatch.setattr(
        agent_native_chat,
        "consume_terminal_ticket",
        lambda ticket: (identity, terminal_id, 1),
    )
    monkeypatch.setattr(
        agent_native_chat,
        "claim_terminal_ticket",
        AsyncMock(
            return_value=(
                SimpleNamespace(status="active"),
                SimpleNamespace(agent_url="http://terminal-node:19823"),
            )
        ),
    )
    monkeypatch.setattr(agent_conversation_api.ws_agent_manager, "attach_native_terminal", attach)
    detach = AsyncMock()
    monkeypatch.setattr(agent_conversation_api.ws_agent_manager, "detach_native_terminal", detach)

    await agent_conversation_api.terminal_websocket(Socket())

    assert events[:2] == ["accept", "attach"]
    detach.assert_awaited_once_with(attachment)
