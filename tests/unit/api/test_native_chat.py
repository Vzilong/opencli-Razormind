import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend import ws_agent_manager
from backend.config import get_settings
from backend.main import app
from backend.models.agent_conversation import AgentConversationTurn
from backend.models.edge_node import EdgeNode
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.studio import StudioProject, StudioWorkspace
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_conversation_service as conversation_service
from backend.services import agent_native_chat


@pytest.fixture
async def native_setup(db_session, monkeypatch):
    user = User(subject="native-admin")
    workspace = Workspace(name="Native", slug="native")
    other = Workspace(name="Other", slug="native-other")
    node = EdgeNode(
        url="http://isolated:19823",
        protocol="ws",
        status="online",
        runtimes=["codex", "omp"],
        runtime_capabilities={"codex": ["operator_chat"], "omp": ["operator_chat"]},
    )
    db_session.add_all([user, workspace, other, node])
    await db_session.flush()
    for target in (workspace, other):
        db_session.add(
            WorkspaceMembership(user_id=user.id, workspace_id=target.id, role=WorkspaceRole.ADMIN)
        )
    await db_session.commit()
    mappings = [
        {
            "workspace_id": workspace.id,
            "runtime_id": runtime_id,
            "agent_url": node.url,
            "cwd": "/isolated/work",
        }
        for runtime_id in ("codex", "omp")
    ]
    monkeypatch.setattr(get_settings(), "native_chat_bindings", mappings)
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager, "is_connected", lambda url: url == node.url
    )
    identity = RequestIdentity(subject=user.subject, is_platform_admin=True)
    app.dependency_overrides[get_request_identity] = lambda: identity
    yield SimpleNamespace(
        workspace=workspace, other=other, node=node, mappings=mappings, identity=identity
    )
    app.dependency_overrides.pop(get_request_identity, None)


async def create_native(client, setup, runtime_id="codex", workspace_id=None):
    return await client.post(
        "/api/v1/chat/sessions",
        json={
            "workspace_id": workspace_id or setup.workspace.id,
            "execution": {"runtime_id": runtime_id, "access_mode": "native"},
        },
    )


@pytest.mark.parametrize("runtime_id", ["codex", "omp"])
async def test_native_chat_persists_selection_and_history_without_provider(
    client, native_setup, monkeypatch, runtime_id
):
    setup = native_setup
    dispatch = AsyncMock(return_value={"type": "done", "result": {"text": "真实执行通道回复"}})
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    options = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    runtime = next(item for item in options.json()["data"]["runtimes"] if item["id"] == runtime_id)
    assert runtime["available"] is True
    assert runtime["access_modes"] == ["native"]
    created = await create_native(client, setup, runtime_id)
    assert created.status_code == 201, created.text
    session = created.json()["data"]
    assert len(session["execution"]["binding_revision"]) == 64
    assert "agent_url" not in created.text and "/isolated/work" not in created.text
    for sequence in (1, 2):
        sent = await client.post(
            f"/api/v1/chat/sessions/{session['id']}/messages",
            json={
                "request_id": f"turn-{sequence}",
                "content": f"记住第 {sequence} 条消息",
            },
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["data"]["turn"]["status"] == "completed"
        assert sent.json()["data"]["turn"]["response"]["content"] == "真实执行通道回复"
    arguments = dispatch.call_args
    assert arguments.args[0] == setup.node.url
    assert arguments.args[1]["runtime"] == runtime_id
    assert arguments.args[1]["workflow"] == "operator_chat"
    assert arguments.kwargs["require_cancel_ack"] is True
    history = json.loads(arguments.args[1]["input"]["message"])
    assert any("第 1 条消息" in message["content"] for message in history)
    assert any("真实执行通道回复" in message["content"] for message in history)
    restored = await client.get(f"/api/v1/chat/sessions/{session['id']}", params={"latest": True})
    assert restored.json()["data"]["execution"] == session["execution"]
    assert len(restored.json()["data"]["turns"]) == 2


async def test_native_chat_denies_other_workspace_and_nonadmin(client, native_setup):
    assert (
        await create_native(client, native_setup, workspace_id=native_setup.other.id)
    ).status_code == 409
    app.dependency_overrides[get_request_identity] = lambda: RequestIdentity(subject="native-admin")
    assert (await create_native(client, native_setup)).status_code == 403


async def test_native_binding_change_blocks_existing_session_before_execution(
    client, db_session, native_setup, monkeypatch
):
    created = await create_native(client, native_setup)
    session_id = created.json()["data"]["id"]
    native_setup.mappings[0]["cwd"] = "/different/root"
    dispatch = AsyncMock()
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "changed", "content": "hello"},
    )
    assert sent.status_code == 409
    assert await db_session.scalar(select(func.count(AgentConversationTurn.id))) == 0
    dispatch.assert_not_called()


@pytest.mark.parametrize(
    "failure", ["offline", "disconnected", "capability", "wrong-node", "duplicate"]
)
async def test_native_chat_requires_exact_ready_binding(
    client, db_session, native_setup, monkeypatch, failure
):
    if failure == "offline":
        native_setup.node.status = "offline"
    elif failure == "disconnected":
        monkeypatch.setattr(agent_native_chat.ws_agent_manager, "is_connected", lambda url: False)
    elif failure == "capability":
        native_setup.node.runtime_capabilities = {"codex": ["streaming"]}
    elif failure == "wrong-node":
        native_setup.mappings[0]["agent_url"] = "http://different:19823"
    else:
        native_setup.mappings.append(dict(native_setup.mappings[0]))
    await db_session.commit()
    assert (await create_native(client, native_setup)).status_code == 409


@pytest.mark.parametrize(
    "text",
    [
        "",
        '<tool_use name="list_providers">{}</tool_use>',
        '<tool_use name="toggle_source">{"source_id":"other","enabled":true}</tool_use>',
    ],
)
async def test_native_invalid_or_global_tool_output_never_completes(
    client, native_setup, monkeypatch, text
):
    monkeypatch.setattr(
        agent_native_chat.ws_agent_manager,
        "send_agent_task",
        AsyncMock(return_value={"type": "done", "result": {"text": text}}),
    )
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "invalid", "content": "hello"},
    )
    assert response.status_code == 502
    restored = await client.get(f"/api/v1/chat/sessions/{session_id}")
    assert restored.json()["data"]["turns"][0]["status"] == "failed"


async def test_native_tool_event_is_rejected(native_setup, monkeypatch):
    async def dispatch(url, task, on_event, **kwargs):
        await on_event({"type": "tool_call", "name": "shell"})
        pytest.fail("native tool event must fail closed")

    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    binding = agent_native_chat.configured_binding(native_setup.workspace.id, "codex")
    with pytest.raises(RuntimeError, match="审批"):
        await agent_native_chat.NativeChatCompletion(binding).create(messages=[])


async def test_native_read_tool_keeps_workspace_scope(
    client, db_session, native_setup, monkeypatch
):
    for workspace in (native_setup.workspace, native_setup.other):
        db_session.add(StudioWorkspace(id=workspace.id, name=workspace.name, slug=workspace.slug))
    await db_session.flush()
    for workspace, name in (
        (native_setup.workspace, "visible-project"),
        (native_setup.other, "private-other"),
    ):
        db_session.add(
            StudioProject(
                workspace_id=workspace.id, name=name, slug=name, created_by_user_id="native-admin"
            )
        )
    await db_session.commit()
    dispatch = AsyncMock(
        side_effect=[
            {"type": "done", "result": {"text": '<tool_use name="list_projects">{}</tool_use>'}},
            {"type": "done", "result": {"text": "Scoped result read."}},
        ]
    )
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "scoped-read", "content": "List projects"},
    )
    assert response.status_code == 200, response.text
    model_context = dispatch.call_args.args[1]["input"]["message"]
    assert "visible-project" in model_context and "private-other" not in model_context
    assert any(
        entry["name"] == "list_projects" for entry in response.json()["data"]["turn"]["tool_trace"]
    )


async def test_native_write_stays_a_confirmation_proposal(
    client, db_session, native_setup, monkeypatch
):
    arguments = {
        "project": {"name": "Native proposed", "slug": "native-proposed"},
        "workflow": {
            "name": "Draft",
            "graph": {
                "id": "native-draft",
                "name": "Draft",
                "profile": "agent-debug",
                "nodes": [{"id": "summary", "kind": "agent", "capability": "summarize"}],
            },
        },
    }
    dispatch = AsyncMock(
        return_value={
            "type": "done",
            "result": {
                "text": '<tool_use name="create_project">' + json.dumps(arguments) + "</tool_use>"
            },
        }
    )
    monkeypatch.setattr(agent_native_chat.ws_agent_manager, "send_agent_task", dispatch)
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "write-proposal", "content": "Prepare a project"},
    )
    assert response.status_code == 200, response.text
    turn = response.json()["data"]["turn"]
    assert turn["status"] == "proposal"
    assert turn["response"]["proposal"]["work_item_id"]
    assert await db_session.scalar(select(func.count(StudioProject.id))) == 0


@pytest.fixture
def recovery_transport(native_setup, monkeypatch):
    monkeypatch.setattr(ws_agent_manager, "_retained_agent_tasks", ws_agent_manager.OrderedDict())
    monkeypatch.setattr(ws_agent_manager, "_connections", {})
    transport = SimpleNamespace(ws=AsyncMock(), frames=[])

    async def send(frame):
        transport.frames.append(frame)
        if frame["type"] == "agent_task_status":
            ws_agent_manager.resolve_agent_task_status(
                frame["request_id"],
                {
                    "type": "agent_task_status_result",
                    "request_id": frame["request_id"],
                    "task_id": frame["task_id"],
                    "result": {"status": "unknown"},
                },
                transport.ws,
            )

    transport.ws.send_json.side_effect = send
    ws_agent_manager.register_connection(native_setup.node.url, transport.ws)
    original = ws_agent_manager.send_agent_task

    async def bounded_dispatch(url, task, on_event, **kwargs):
        return await original(
            url,
            task,
            on_event,
            require_cancel_ack=True,
            timeout=0.02,
            cancel_ack_timeout=0.02,
            native_chat_authorized=kwargs.get("native_chat_authorized", False),
        )

    monkeypatch.setattr(ws_agent_manager, "send_agent_task", bounded_dispatch)
    return transport


@pytest.mark.parametrize("terminal_type", ["done", "error"])
async def test_native_late_cleanup_reconciles_only_matching_turn_without_replaying_output(
    client, db_session, native_setup, recovery_transport, terminal_type
):
    transport = recovery_transport
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    captured = []

    async def check_before_transport(frame):
        transport.frames.append(frame)
        if frame["type"] == "agent_task_status":
            ws_agent_manager.resolve_agent_task_status(
                frame["request_id"],
                {
                    "type": "agent_task_status_result",
                    "request_id": frame["request_id"],
                    "task_id": frame["task_id"],
                    "result": {"status": "unknown"},
                },
                transport.ws,
            )
        if frame["type"] == "agent_task":
            async with async_sessionmaker(db_session.bind, expire_on_commit=False)() as db:
                turn = await db.scalar(
                    select(AgentConversationTurn).where(
                        AgentConversationTurn.conversation_id == session_id
                    )
                )
                captured.append(
                    dict(turn.context_binding[conversation_service.REMOTE_DISPATCH_CONTEXT_KEY])
                )
                assert turn.status == "running"
                assert captured[-1]["request_id"] == frame["request_id"]

    transport.ws.send_json.side_effect = check_before_transport
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "recover", "content": "hello"},
    )
    assert sent.status_code == 503, sent.text
    assert len(captured) == 1
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    detail = (await client.get(detail_url)).json()["data"]
    assert detail["turns"][0]["status"] == "running"
    assert detail["turns"][0]["error_code"] == "remote_execution_unconfirmed"
    assert native_setup.node.url not in json.dumps(detail)
    stopped = await client.post(f"{detail_url}/cancel", json={"request_id": "recover"})
    assert stopped.status_code == 503
    remote_id = captured[0]["request_id"]
    result = {
        "task_id": remote_id,
        "type": terminal_type,
        "cleanup_complete": True,
        "error_type": "CancelledError",
        "result": {"text": '<tool_use name="create_project">{"name":"must not run"}</tool_use>'},
    }
    ws_agent_manager.resolve_agent_result(remote_id, {"result": result}, source_ws=AsyncMock())
    assert (await client.get(detail_url)).json()["data"]["turns"][0]["status"] == "running"
    ws_agent_manager.resolve_agent_result(remote_id, {"result": result}, source_ws=transport.ws)
    reconciled = (await client.get(detail_url)).json()["data"]
    turn = reconciled["turns"][0]
    assert turn["status"] == "failed"
    assert turn["error_code"] == (
        "cancelled" if terminal_type == "error" else "remote_execution_reconciled"
    )
    assert turn["response"] is None
    assert turn["tool_trace"] == []
    assert [frame["type"] for frame in transport.frames] == [
        "agent_task",
        "cancel",
        "agent_task_status",
    ]
    assert not ws_agent_manager._retained_agent_tasks
    repeated = (await client.get(detail_url)).json()["data"]
    assert repeated["revision"] == reconciled["revision"]
    assert repeated["turns"][0] == turn


async def test_new_native_request_cannot_bypass_unresolved_conversation_lock(
    client, native_setup, recovery_transport
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    assert (
        await client.post(
            f"{detail_url}/messages", json={"request_id": "first", "content": "hello"}
        )
    ).status_code == 503
    before = (await client.get(detail_url)).json()["data"]
    frames = list(recovery_transport.frames)
    assert before["turns"][0]["status"] == "running"
    assert before["turns"][0]["error_code"] == "remote_execution_unconfirmed"
    for request_id in ("second", "first"):
        rejected = await client.post(
            f"{detail_url}/messages", json={"request_id": request_id, "content": "try again"}
        )
        assert rejected.status_code == 409, rejected.text
    assert recovery_transport.frames == frames
    after = (await client.get(detail_url)).json()["data"]
    assert after["turns"] == before["turns"]
    assert after["revision"] == before["revision"]


async def test_native_live_runner_is_not_reconciled_from_early_terminal_cache(
    client, db_session, native_setup, recovery_transport, monkeypatch
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    entered = asyncio.Event()
    release = asyncio.Event()
    original = conversation_service._finish_turn

    async def paused_finish(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(conversation_service, "_finish_turn", paused_finish)
    transport = recovery_transport

    async def completed(frame):
        transport.frames.append(frame)
        if frame["type"] == "agent_task":
            ws_agent_manager.resolve_agent_result(
                frame["request_id"],
                {
                    "result": {
                        "task_id": frame["request_id"],
                        "type": "done",
                        "cleanup_complete": True,
                        "result": {"text": "legitimate reply"},
                    }
                },
                source_ws=transport.ws,
            )

    transport.ws.send_json.side_effect = completed

    async def send():
        async with async_sessionmaker(db_session.bind, expire_on_commit=False)() as db:
            return await conversation_service.send_message(
                db,
                native_setup.identity,
                session_id,
                request_id="live",
                content="hello",
                context=None,
            )

    pending = asyncio.create_task(send())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        detail = await client.get(f"/api/v1/chat/sessions/{session_id}")
        assert detail.json()["data"]["turns"][0]["status"] == "running"
    finally:
        release.set()
        await asyncio.wait_for(pending, 3)
    detail = await client.get(f"/api/v1/chat/sessions/{session_id}")
    assert detail.json()["data"]["turns"][0]["status"] == "completed"


@pytest.mark.parametrize(
    "proof", ["confirmed", "wrong-task", "not-stopped", "missing", "timeout", "stalled"]
)
async def test_native_admin_probe_recovery_after_cache_loss_is_fail_closed(
    client, db_session, native_setup, recovery_transport, monkeypatch, proof
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "recover", "content": "hello"},
    )
    assert sent.status_code == 503
    remote_id = recovery_transport.frames[0]["request_id"]
    ws_agent_manager._retained_agent_tasks.clear()
    terminal = {
        "task_id": remote_id,
        "type": "error",
        "error_type": "CancelledError",
        "cleanup_complete": True,
    }
    if proof == "wrong-task":
        terminal["task_id"] = "other-task"
    elif proof == "not-stopped":
        terminal["cleanup_complete"] = False
    elif proof == "missing":
        terminal = {"type": "not_found"}
    probe = AsyncMock(return_value=terminal)
    if proof == "timeout":
        probe.side_effect = TimeoutError
    elif proof == "stalled":

        async def stalled(*args):
            await asyncio.Future()

        probe.side_effect = stalled
        monkeypatch.setattr(conversation_service, "REMOTE_PROBE_TIMEOUT_SECONDS", 0.01)
    if proof == "confirmed":
        turn = await conversation_service.recover_conversation_turn(
            db_session, native_setup.identity, session_id, request_id="recover", probe=probe
        )
        assert turn.status == "failed"
        assert turn.error_code == "cancelled"
    else:
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as raised:
            await conversation_service.recover_conversation_turn(
                db_session, native_setup.identity, session_id, request_id="recover", probe=probe
            )
        assert raised.value.status_code == 503
        detail = (await client.get(f"/api/v1/chat/sessions/{session_id}")).json()["data"]
        assert detail["turns"][0]["status"] == "running"
    probe.assert_awaited_once_with(native_setup.node.url, remote_id)


@pytest.mark.parametrize("denial", ["nonadmin", "foreign-workspace", "changed-binding"])
async def test_native_admin_recovery_checks_authority_before_probe(
    client, db_session, native_setup, recovery_transport, denial
):
    from fastapi import HTTPException

    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    assert (
        await client.post(
            f"/api/v1/chat/sessions/{session_id}/messages",
            json={"request_id": "recover", "content": "hello"},
        )
    ).status_code == 503
    identity = native_setup.identity
    if denial == "nonadmin":
        identity = RequestIdentity(subject=identity.subject)
    elif denial == "foreign-workspace":
        identity = RequestIdentity(subject="outsider", is_platform_admin=True)
    else:
        native_setup.mappings[0]["agent_url"] = "http://replacement:19823"
    probe = AsyncMock()
    with pytest.raises(HTTPException) as raised:
        await conversation_service.recover_conversation_turn(
            db_session, identity, session_id, request_id="recover", probe=probe
        )
    assert raised.value.status_code == (409 if denial == "changed-binding" else 403)
    probe.assert_not_awaited()


async def test_native_recovery_uses_last_dispatch_not_completed_tool_round(
    client, native_setup, recovery_transport
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    transport = recovery_transport
    dispatches = []

    async def first_round_only(frame):
        transport.frames.append(frame)
        if frame["type"] != "agent_task":
            return
        dispatches.append(frame["request_id"])
        if len(dispatches) == 1:
            ws_agent_manager.resolve_agent_result(
                frame["request_id"],
                {
                    "result": {
                        "task_id": frame["request_id"],
                        "type": "done",
                        "cleanup_complete": True,
                        "result": {"text": '<tool_use name="list_projects">{}</tool_use>'},
                    }
                },
                source_ws=transport.ws,
            )

    transport.ws.send_json.side_effect = first_round_only
    sent = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        json={"request_id": "tool-rounds", "content": "list projects"},
    )
    assert sent.status_code == 503, sent.text
    assert len(dispatches) == 2
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    turn = (await client.get(detail_url)).json()["data"]["turns"][0]
    assert (
        turn["context_binding"][conversation_service.REMOTE_DISPATCH_CONTEXT_KEY]["request_id"]
        == dispatches[-1]
    )
    assert turn["status"] == "running"
    ws_agent_manager.resolve_agent_result(
        dispatches[-1],
        {
            "result": {
                "task_id": dispatches[-1],
                "type": "error",
                "cleanup_complete": True,
                "error_type": "CancelledError",
            }
        },
        source_ws=transport.ws,
    )
    turn = (await client.get(detail_url)).json()["data"]["turns"][0]
    assert turn["status"] == "failed"
    assert turn["error_code"] == "cancelled"


@pytest.mark.parametrize(
    "outcome", ["done", "cancelled", "running", "unknown", "unclean", "timeout", "disconnected"]
)
async def test_orphan_stop_after_api_restart_uses_real_manager_probe_and_durable_correlation(
    client, db_session, native_setup, recovery_transport, monkeypatch, outcome
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    sent = await client.post(
        f"{detail_url}/messages", json={"request_id": "recover", "content": "hello"}
    )
    assert sent.status_code == 503
    remote_id = recovery_transport.frames[0]["request_id"]
    ws_agent_manager._retained_agent_tasks.clear()
    ws_agent_manager.unregister_connection(native_setup.node.url, recovery_transport.ws)
    assert not ws_agent_manager._pending_agent_tasks
    assert not ws_agent_manager._agent_task_callbacks
    ws = AsyncMock()
    frames = []

    async def status_result(frame):
        frames.append(frame)
        assert frame["type"] == "agent_task_status"
        assert frame["request_id"] not in ("recover", remote_id)
        assert frame["task_id"] == remote_id
        if outcome == "timeout":
            return
        if outcome in ("running", "unknown"):
            result = {"status": outcome}
        else:
            result = {
                "type": "done" if outcome == "done" else "error",
                "task_id": remote_id,
                "cleanup_complete": outcome != "unclean",
                "error_type": "CancelledError",
                "result": {"text": "Never replay this untrusted output"},
            }
        ws_agent_manager.resolve_agent_task_status(
            frame["request_id"],
            {
                "type": "agent_task_status_result",
                "request_id": frame["request_id"],
                "task_id": remote_id,
                "result": result,
            },
            ws,
        )

    ws.send_json.side_effect = status_result
    if outcome != "disconnected":
        ws_agent_manager.register_connection(native_setup.node.url, ws)
    monkeypatch.setattr(conversation_service, "REMOTE_PROBE_TIMEOUT_SECONDS", 0.05)
    stopped = await client.post(f"{detail_url}/cancel", json={"request_id": "recover"})
    assert not ws_agent_manager._agent_task_status_probes
    db_session.expire_all()
    detail = (await client.get(detail_url)).json()["data"]
    turn = detail["turns"][0]
    assert turn["response"] is None
    assert turn["tool_trace"] == []
    if outcome in ("done", "cancelled"):
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"] == {"accepted": True}
        assert turn["status"] == "failed"
        assert turn["error_code"] == (
            "cancelled" if outcome == "cancelled" else "remote_execution_reconciled"
        )
        again = await client.get(detail_url)
        assert again.json()["data"]["revision"] == detail["revision"]
    else:
        assert stopped.status_code == 503, stopped.text
        assert turn["status"] == "running"
        assert turn["error_code"] == "remote_execution_unconfirmed"
    assert len(frames) == (0 if outcome == "disconnected" else 1)


@pytest.mark.parametrize(
    "denial", ["client-proof", "nonadmin", "foreign-workspace", "changed-binding"]
)
async def test_stop_probe_never_accepts_client_assertions_or_wrong_authority(
    client, native_setup, recovery_transport, monkeypatch, denial
):
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    assert (
        await client.post(
            f"{detail_url}/messages", json={"request_id": "recover", "content": "hello"}
        )
    ).status_code == 503
    probe = AsyncMock()
    monkeypatch.setattr(ws_agent_manager, "probe_agent_task", probe)
    body = {"request_id": "recover"}
    if denial == "client-proof":
        body["cleanup_complete"] = True
    elif denial == "nonadmin":
        app.dependency_overrides[get_request_identity] = lambda: RequestIdentity(
            subject="native-admin"
        )
    elif denial == "foreign-workspace":
        app.dependency_overrides[get_request_identity] = lambda: RequestIdentity(
            subject="outsider", is_platform_admin=True
        )
    else:
        native_setup.mappings[0]["agent_url"] = "http://other:19823"
    response = await client.post(f"{detail_url}/cancel", json=body)
    assert response.status_code == (
        422 if denial == "client-proof" else 409 if denial == "changed-binding" else 403
    )
    probe.assert_not_awaited()


@pytest.mark.parametrize("edge_restarted", [False, True])
async def test_stop_recovery_roundtrips_actual_edge_cache_protocol(
    client, db_session, native_setup, recovery_transport, monkeypatch, edge_restarted
):
    from collections import OrderedDict

    from backend import agent_server

    monkeypatch.setattr(agent_server, "_AGENT_TASK_TERMINALS", OrderedDict())
    monkeypatch.setattr(agent_server, "_ACTIVE_AGENT_TASKS", {})
    session_id = (await create_native(client, native_setup)).json()["data"]["id"]
    detail_url = f"/api/v1/chat/sessions/{session_id}"
    assert (
        await client.post(
            f"{detail_url}/messages", json={"request_id": "recover", "content": "hello"}
        )
    ).status_code == 503
    remote_id = recovery_transport.frames[0]["request_id"]
    agent_server._cache_agent_task_terminal(
        remote_id,
        {
            "task_id": remote_id,
            "type": "error",
            "cleanup_complete": True,
            "error_type": "CancelledError",
            "message": "must not persist diagnostic text",
        },
    )
    ws_agent_manager._retained_agent_tasks.clear()
    ws_agent_manager.unregister_connection(native_setup.node.url, recovery_transport.ws)
    if edge_restarted:
        agent_server._AGENT_TASK_TERMINALS.clear()
    owner, edge_peer = AsyncMock(), AsyncMock()
    ws_agent_manager.register_connection(native_setup.node.url, owner)

    async def receive(raw):
        reply = json.loads(raw)
        ws_agent_manager.resolve_agent_task_status(reply["request_id"], reply, owner)

    edge_peer.send.side_effect = receive

    async def send(frame):
        assert frame["type"] == "agent_task_status"
        await agent_server._handle_ws_agent_task_status(edge_peer, frame)

    owner.send_json.side_effect = send
    response = await client.post(f"{detail_url}/cancel", json={"request_id": "recover"})
    assert response.status_code == (503 if edge_restarted else 200), response.text
    if not edge_restarted:
        assert response.json()["data"] == {"accepted": True}
    owner.send_json.assert_awaited_once()
    agent_server._AGENT_TASK_TERMINALS.clear()
    ws_agent_manager._retained_agent_tasks.clear()
    db_session.expire_all()
    turn = (await client.get(detail_url)).json()["data"]["turns"][0]
    assert turn["status"] == ("running" if edge_restarted else "failed")
    assert turn["error_code"] == ("remote_execution_unconfirmed" if edge_restarted else "cancelled")
