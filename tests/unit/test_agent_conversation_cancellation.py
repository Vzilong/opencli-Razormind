import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from backend.api.v1 import chat
from backend.database import Base, get_db
from backend.llm.base import LlmAdapterError
from backend.main import app
from backend.models.agent_conversation import AgentConversation, AgentConversationTurn
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.provider import ModelProvider
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_conversation_cancellation as cancellation
from backend.services import agent_conversation_service as service
from backend.ws_agent_manager import AgentTaskUnresolvedError


@pytest.fixture
async def cancellation_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'cancellation.db').as_posix()}",
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_wal(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    identity = RequestIdentity(subject="cancel-operator")
    async with sessions() as db:
        workspace = Workspace(name="Cancellation", slug="cancellation")
        owner = User(subject=identity.subject)
        viewer = User(subject="cancel-viewer")
        db.add_all([workspace, owner, viewer])
        await db.flush()
        db.add_all(
            [
                WorkspaceMembership(
                    workspace_id=workspace.id, user_id=owner.id, role=WorkspaceRole.OPERATOR
                ),
                WorkspaceMembership(
                    workspace_id=workspace.id, user_id=viewer.id, role=WorkspaceRole.VIEWER
                ),
            ]
        )
        await db.commit()
        conversation = await service.create_conversation(
            db, identity, workspace_id=workspace.id, title=None, context=None
        )
    environment = SimpleNamespace(
        sessions=sessions, identity=identity, conversation=conversation, workspace=workspace
    )

    async def request_db():
        async with sessions() as db:
            yield db

    async def request_identity():
        return environment.identity

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_request_identity] = request_identity
    monkeypatch.setattr(cancellation, "POLL_INTERVAL_SECONDS", 0.01)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            environment.client = client
            yield environment
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        await engine.dispose()


async def _seed_turn(environment, *, request_id="pending", turn_status="running", sequence=1):
    async with environment.sessions() as db:
        turn = AgentConversationTurn(
            conversation_id=environment.conversation.id,
            workspace_id=environment.workspace.id,
            sequence=sequence,
            request_id=request_id,
            user_content="hello",
            context_binding={},
            tool_trace=[],
            status=turn_status,
            response={"type": "message", "content": "prior reply"}
            if turn_status == "completed"
            else None,
        )
        db.add(turn)
        await db.commit()
        return turn


async def _read_turn(environment, request_id="pending"):
    async with environment.sessions() as db:
        return await db.scalar(
            select(AgentConversationTurn).where(
                AgentConversationTurn.conversation_id == environment.conversation.id,
                AgentConversationTurn.request_id == request_id,
            )
        )


async def _cancel(environment, request_id="pending", **extra):
    return await environment.client.post(
        f"/api/v1/chat/sessions/{environment.conversation.id}/cancel",
        json={"request_id": request_id, **extra},
    )


async def _send(environment, runner, **kwargs):
    async with environment.sessions() as db:
        return await service.send_message(
            db,
            environment.identity,
            environment.conversation.id,
            request_id="pending",
            content="hello",
            context=None,
            chat_runner=runner,
            **kwargs,
        )


async def test_cancel_is_durable_and_only_final_after_runner_exits(cancellation_env, monkeypatch):
    environment = cancellation_env
    prior = await _seed_turn(environment, request_id="prior", turn_status="completed")
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_exit = asyncio.Event()
    runner_task = None

    async def runner(model_db, *_args, **kwargs):
        nonlocal runner_task
        runner_task = asyncio.current_task()
        provider = ModelProvider(name="committed operation")
        model_db.add(provider)
        await model_db.commit()
        provider.name = "uncommitted operation"
        kwargs["tool_trace"].append({"name": "read", "status": "completed"})
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_exit.wait()

    monkeypatch.setattr(chat, "run_chat_request", runner)
    send = asyncio.create_task(
        environment.client.post(
            f"/api/v1/chat/sessions/{environment.conversation.id}/messages",
            json={"request_id": "pending", "content": "hello"},
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 3)
        response = await _cancel(environment)
        assert response.status_code == 200
        assert response.json()["data"] == {"accepted": True}
        await asyncio.wait_for(cleanup_started.wait(), 3)
        pending = await _read_turn(environment)
        assert pending.status == "running"
        assert pending.error_code == "cancel_requested"
        assert not send.done()
        assert (await _cancel(environment)).json()["data"] == {"accepted": True}
        allow_exit.set()
        sent = await asyncio.wait_for(send, 3)
    finally:
        allow_exit.set()
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)
    assert sent.status_code == 200
    final = sent.json()["data"]["turn"]
    assert final["status"] == "failed"
    assert final["error_code"] == "cancelled"
    assert final["error_message"] == "已停止当前回复；已完成的操作不会撤销。"
    assert final["response"] is None
    assert final["tool_trace"] == [{"name": "read", "status": "completed"}]
    assert runner_task.done()
    assert (await _cancel(environment)).status_code == 409
    restored_prior = await _read_turn(environment, "prior")
    assert restored_prior.status == prior.status
    assert restored_prior.response == prior.response
    async with environment.sessions() as db:
        assert await db.scalar(select(ModelProvider.name)) == "committed operation"


@pytest.mark.parametrize("subject", ["cancel-viewer", "outsider"])
async def test_cancel_requires_stored_workspace_and_run_permission(cancellation_env, subject):
    environment = cancellation_env
    await _seed_turn(environment)
    environment.identity = RequestIdentity(subject=subject, is_platform_admin=True)
    response = await _cancel(environment)
    assert response.status_code == 403
    assert (await _read_turn(environment)).error_code is None


@pytest.mark.parametrize("turn_status", ["completed", "proposal", "failed"])
async def test_cancel_rejects_terminal_turns(cancellation_env, turn_status):
    await _seed_turn(cancellation_env, turn_status=turn_status)
    assert (await _cancel(cancellation_env)).status_code == 409
    assert (await _read_turn(cancellation_env)).error_code is None


async def test_cancel_without_runner_is_unresolved_not_accepted(cancellation_env):
    await _seed_turn(cancellation_env)
    response = await _cancel(cancellation_env)
    assert response.status_code == 503
    turn = await _read_turn(cancellation_env)
    assert turn.status == "running"
    assert turn.error_code is None


async def test_unresolved_send_persists_correlation_and_stop_preserves_marker(cancellation_env):
    async def runner(*args, **kwargs):
        raise AgentTaskUnresolvedError(
            "http://isolated:19823", "remote-request", "agent_disconnected"
        )

    with pytest.raises(HTTPException) as raised:
        await _send(cancellation_env, runner)
    assert raised.value.status_code == 503
    turn = await _read_turn(cancellation_env)
    assert turn.status == "running"
    assert turn.error_code == AgentTaskUnresolvedError.code
    assert (
        turn.context_binding[service.REMOTE_DISPATCH_CONTEXT_KEY]["request_id"] == "remote-request"
    )
    assert not cancellation.has_turn_runner(turn.id)
    assert (await _cancel(cancellation_env)).status_code == 503
    assert (await _read_turn(cancellation_env)).error_code == AgentTaskUnresolvedError.code


async def test_unresolved_turn_blocks_new_request_but_allows_completed_replay(cancellation_env):
    environment = cancellation_env
    prior = await _seed_turn(environment, request_id="prior", turn_status="completed")

    async def unresolved(*args, **kwargs):
        raise AgentTaskUnresolvedError(
            "http://isolated:19823", "remote-request", "agent_disconnected"
        )

    with pytest.raises(HTTPException) as raised:
        await _send(environment, unresolved)
    assert raised.value.status_code == 503
    blocked = await _read_turn(environment)
    assert not cancellation.has_turn_runner(blocked.id)
    runner = AsyncMock(return_value=chat.ChatReply(type="message", content="must not dispatch"))
    async with environment.sessions() as db:
        revision = await db.scalar(
            select(AgentConversation.revision).where(
                AgentConversation.id == environment.conversation.id
            )
        )
        with pytest.raises(HTTPException) as conflict:
            await service.send_message(
                db,
                environment.identity,
                environment.conversation.id,
                request_id="different-request",
                content="new request",
                context=None,
                chat_runner=runner,
            )
        assert conflict.value.status_code == 409
        assert (
            await db.scalar(
                select(AgentConversation.revision).where(
                    AgentConversation.id == environment.conversation.id
                )
            )
            == revision
        )
        _, replay = await service.send_message(
            db,
            environment.identity,
            environment.conversation.id,
            request_id="prior",
            content="replay",
            context=None,
            chat_runner=runner,
        )
        assert replay.id == prior.id
        assert replay.response == prior.response
        rows = list(
            await db.scalars(
                select(AgentConversationTurn).where(
                    AgentConversationTurn.conversation_id == environment.conversation.id
                )
            )
        )
        assert len(rows) == 2
    runner.assert_not_awaited()
    restored = await _read_turn(environment)
    assert restored.status == "running"
    assert restored.error_code == AgentTaskUnresolvedError.code
    assert restored.context_binding == blocked.context_binding


@pytest.mark.parametrize("same_request", [False, True])
async def test_concurrent_requests_admit_one_turn_and_dispatch_once(
    cancellation_env, monkeypatch, same_request
):
    environment = cancellation_env
    barrier = asyncio.Barrier(2)
    release = asyncio.Event()
    dispatched = []
    insert = service._insert_running_turn

    async def gated_insert(*args, **kwargs):
        await barrier.wait()
        return await insert(*args, **kwargs)

    async def runner(*args, **kwargs):
        dispatched.append(kwargs["proposal_provenance"].turn_id)
        await release.wait()
        return chat.ChatReply(type="message", content="one reply")

    async def send(request_id):
        async with environment.sessions() as db:
            return await service.send_message(
                db,
                environment.identity,
                environment.conversation.id,
                request_id=request_id,
                content="hello",
                context=None,
                chat_runner=runner,
            )

    monkeypatch.setattr(service, "_insert_running_turn", gated_insert)
    tasks = [
        asyncio.create_task(send("first")),
        asyncio.create_task(send("first" if same_request else "second")),
    ]
    try:
        done, waiting = await asyncio.wait(tasks, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1
        assert len(waiting) == 1
        error = next(iter(done)).exception()
        assert isinstance(error, HTTPException)
        assert error.status_code == 409
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        monkeypatch.setattr(service, "_insert_running_turn", insert)
    assert len(dispatched) == 1
    assert sum(isinstance(result, tuple) for result in results) == 1
    async with environment.sessions() as db:
        rows = list(
            await db.scalars(
                select(AgentConversationTurn).where(
                    AgentConversationTurn.conversation_id == environment.conversation.id
                )
            )
        )
        assert len(rows) == 1
        assert rows[0].status == "completed"
        replay_runner = AsyncMock(return_value=chat.ChatReply(type="message", content="next reply"))
        _, replay = await service.send_message(
            db,
            environment.identity,
            environment.conversation.id,
            request_id=rows[0].request_id,
            content="same request",
            context=None,
            chat_runner=replay_runner,
        )
        assert replay.id == rows[0].id
        replay_runner.assert_not_awaited()
        _, following = await service.send_message(
            db,
            environment.identity,
            environment.conversation.id,
            request_id="following",
            content="new turn after completion",
            context=None,
            chat_runner=replay_runner,
        )
        assert following.status == "completed"
        assert following.sequence == 2
        replay_runner.assert_awaited_once()


async def test_cancel_requires_exact_request_and_conversation(cancellation_env):
    environment = cancellation_env
    pending = await _seed_turn(environment)
    assert (await _cancel(environment, request_id=pending.id)).status_code == 409
    assert (await _cancel(environment, request_id="different-request")).status_code == 409
    async with environment.sessions() as db:
        other = await service.create_conversation(
            db,
            environment.identity,
            workspace_id=environment.workspace.id,
            title=None,
            context=None,
        )
    wrong_conversation = await environment.client.post(
        f"/api/v1/chat/sessions/{other.id}/cancel", json={"request_id": pending.request_id}
    )
    assert wrong_conversation.status_code == 409
    missing = await environment.client.post(
        "/api/v1/chat/sessions/missing/cancel", json={"request_id": pending.request_id}
    )
    assert missing.status_code == 404
    assert (await _read_turn(environment)).error_code is None


@pytest.mark.parametrize("extra", [{"run_id": "arbitrary"}, {"workspace_id": "arbitrary"}])
async def test_cancel_rejects_forged_scope(cancellation_env, extra):
    await _seed_turn(cancellation_env)
    assert (await _cancel(cancellation_env, **extra)).status_code == 422
    assert (await _read_turn(cancellation_env)).error_code is None


@pytest.mark.parametrize("request_id", ["", " ", "x" * 65])
async def test_cancel_rejects_invalid_request_id(cancellation_env, request_id):
    assert (await _cancel(cancellation_env, request_id=request_id)).status_code == 422


@pytest.mark.parametrize("reply_type", ["message", "proposal"])
async def test_finished_runner_wins_cancel_race_and_never_executes_confirmation(
    cancellation_env, monkeypatch, reply_type
):
    environment = cancellation_env
    monkeypatch.setattr(cancellation, "POLL_INTERVAL_SECONDS", 60)
    execute = AsyncMock(side_effect=AssertionError("confirmation must never execute"))
    monkeypatch.setattr(service.agent_control_service, "execute_confirmed", execute)

    async def runner(*_args, **_kwargs):
        response = await _cancel(environment)
        assert response.status_code == 200
        if reply_type == "proposal":
            return chat.ChatReply(
                type="proposal",
                proposal=chat.Proposal(
                    tool="update_provider",
                    args={},
                    summary="Review change",
                    diff="change",
                    workspace_id=environment.workspace.id,
                    work_item_id="work-item",
                    proposal_version="version",
                ),
            )
        return chat.ChatReply(type="message", content="already finished")

    _, turn = await _send(environment, runner)
    assert turn.status == ("proposal" if reply_type == "proposal" else "completed")
    assert turn.response["type"] == reply_type
    assert turn.error_code is None
    execute.assert_not_awaited()


@pytest.mark.parametrize("disconnect", [False, True])
async def test_disconnect_or_task_cancellation_drains_runtime_and_persists_failure(
    cancellation_env, disconnect
):
    environment = cancellation_env
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_exit = asyncio.Event()
    runner_task = None

    async def runner(*_args, **_kwargs):
        nonlocal runner_task
        runner_task = asyncio.current_task()
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await allow_exit.wait()

    async def is_disconnected():
        return disconnect and started.is_set()

    send = asyncio.create_task(_send(environment, runner, is_disconnected=is_disconnected))
    try:
        await asyncio.wait_for(started.wait(), 3)
        if not disconnect:
            send.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 3)
        if not disconnect:
            send.cancel()
        assert not send.done()
        allow_exit.set()
        if disconnect:
            _, turn = await asyncio.wait_for(send, 3)
            assert turn.error_code == "cancelled"
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(send, 3)
    finally:
        allow_exit.set()
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)
    assert runner_task.done()
    final = await _read_turn(environment)
    assert final.status == "failed"
    assert final.error_code == "cancelled"


@pytest.mark.parametrize(
    "error",
    [
        HTTPException(status_code=409, detail="real conflict"),
        LlmAdapterError("real model failure", retryable=True),
        RuntimeError("real runtime failure"),
    ],
)
async def test_noncancel_errors_keep_existing_failure_semantics(cancellation_env, error):
    async def runner(*_args, **_kwargs):
        raise error

    with pytest.raises(HTTPException) as raised:
        await _send(cancellation_env, runner)
    assert raised.value.status_code == (409 if isinstance(error, HTTPException) else 502)
    turn = await _read_turn(cancellation_env)
    assert turn.status == "failed"
    assert turn.error_code == (
        "model_unavailable" if isinstance(error, LlmAdapterError) else "model_error"
    )
    assert "real" in turn.error_message


async def test_poll_failure_drains_runner_and_propagates_original_error(monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()
    error = RuntimeError("poll failed")

    async def watcher(*_args, **_kwargs):
        await started.wait()
        raise error

    async def runner():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(cancellation, "_watch_cancellation", watcher)
    with pytest.raises(RuntimeError) as raised:
        await cancellation.run_with_cancellation(
            runner(), None, conversation_id="conversation", workspace_id="workspace", turn_id="turn"
        )
    assert raised.value is error
    assert stopped.is_set()


@pytest.mark.parametrize("trigger", ["parent", "watcher", "poll_error"])
async def test_unconfirmed_remote_cleanup_survives_all_cancellation_paths(monkeypatch, trigger):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    allow_exit = asyncio.Event()
    unresolved = AgentTaskUnresolvedError(
        "http://agent:19823", "remote-request", "cancel_ack_timeout"
    )

    async def watcher(*_args, **_kwargs):
        await started.wait()
        if trigger == "poll_error":
            raise RuntimeError("poll failed")
        if trigger == "parent":
            await asyncio.Event().wait()

    async def runner():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_started.set()
            await allow_exit.wait()
            raise unresolved from None

    monkeypatch.setattr(cancellation, "_watch_cancellation", watcher)
    pending = asyncio.create_task(
        cancellation.run_with_cancellation(
            runner(), None, conversation_id="conversation", workspace_id="workspace", turn_id="turn"
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        if trigger == "parent":
            pending.cancel()
        await asyncio.wait_for(cleanup_started.wait(), 1)
        if trigger == "parent":
            pending.cancel()
        assert not pending.done()
        allow_exit.set()
        with pytest.raises(AgentTaskUnresolvedError) as raised:
            await asyncio.wait_for(pending, 1)
        assert raised.value is unresolved
    finally:
        allow_exit.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_remote_disconnect_propagates_without_becoming_conversation_cancellation(monkeypatch):
    unresolved = AgentTaskUnresolvedError(
        "http://agent:19823", "remote-request", "agent_disconnected"
    )

    async def watcher(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def runner():
        raise unresolved

    monkeypatch.setattr(cancellation, "_watch_cancellation", watcher)
    with pytest.raises(AgentTaskUnresolvedError) as raised:
        await cancellation.run_with_cancellation(
            runner(), None, conversation_id="conversation", workspace_id="workspace", turn_id="turn"
        )
    assert raised.value is unresolved


async def test_runner_error_during_cancel_is_not_mislabeled_cancelled(cancellation_env):
    started = asyncio.Event()

    async def runner(*_args, **_kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed") from None

    send = asyncio.create_task(_send(cancellation_env, runner))
    try:
        await asyncio.wait_for(started.wait(), 3)
        assert (await _cancel(cancellation_env)).status_code == 200
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(send, 3)
        assert raised.value.status_code == 502
    finally:
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)
    assert (await _read_turn(cancellation_env)).error_code == "model_error"


async def test_parent_cancel_after_reply_commit_preserves_completed_outcome(
    cancellation_env, monkeypatch
):
    committed = asyncio.Event()
    allow_finish = asyncio.Event()
    original_finish = service._finish_turn

    async def finish(*args, **kwargs):
        result = await original_finish(*args, **kwargs)
        if kwargs["turn_status"] == "completed":
            committed.set()
            await allow_finish.wait()
        return result

    monkeypatch.setattr(service, "_finish_turn", finish)
    runner = AsyncMock(return_value=chat.ChatReply(type="message", content="done"))
    send = asyncio.create_task(_send(cancellation_env, runner))
    try:
        await asyncio.wait_for(committed.wait(), 3)
        send.cancel()
        allow_finish.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(send, 3)
    finally:
        allow_finish.set()
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)
    final = await _read_turn(cancellation_env)
    assert final.status == "completed"
    assert final.error_code is None
    assert final.response == {"type": "message", "content": "done"}


async def test_latest_detail_restores_running_then_cancelled_turn_beyond_first_fifty(
    cancellation_env,
):
    environment = cancellation_env
    async with environment.sessions() as db:
        db.add_all(
            AgentConversationTurn(
                conversation_id=environment.conversation.id,
                workspace_id=environment.workspace.id,
                sequence=sequence,
                request_id=f"prior-{sequence}",
                user_content="prior message",
                response={"type": "message", "content": "prior reply"},
                context_binding={},
                tool_trace=[],
                status="completed",
            )
            for sequence in range(1, 51)
        )
        await db.commit()
    started = asyncio.Event()

    async def runner(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    detail_url = f"/api/v1/chat/sessions/{environment.conversation.id}"
    send = asyncio.create_task(_send(environment, runner))
    try:
        await asyncio.wait_for(started.wait(), 3)
        default = await environment.client.get(detail_url)
        assert default.status_code == 200
        assert [turn["sequence"] for turn in default.json()["data"]["turns"]] == list(range(1, 51))
        latest = await environment.client.get(detail_url, params={"latest": True, "limit": 50})
        assert latest.status_code == 200
        turns = latest.json()["data"]["turns"]
        assert [turn["sequence"] for turn in turns] == list(range(2, 52))
        assert turns[-1]["request_id"] == "pending"
        assert turns[-1]["status"] == "running"
        assert (await _cancel(environment)).status_code == 200
        await asyncio.wait_for(send, 3)
    finally:
        if not send.done():
            send.cancel()
        await asyncio.gather(send, return_exceptions=True)
    restored = await environment.client.get(detail_url, params={"latest": True, "limit": 3})
    turns = restored.json()["data"]["turns"]
    assert [turn["sequence"] for turn in turns] == [49, 50, 51]
    assert turns[-1]["status"] == "failed"
    assert turns[-1]["error_code"] == "cancelled"
    for suffix in ("", "/replay"):
        incremental = await environment.client.get(
            f"{detail_url}{suffix}", params={"after_sequence": 50}
        )
        assert incremental.status_code == 200
        assert [turn["sequence"] for turn in incremental.json()["data"]["turns"]] == [51]


@pytest.mark.parametrize("after_sequence", [1, 50])
async def test_latest_detail_rejects_incremental_cursor(cancellation_env, after_sequence):
    response = await cancellation_env.client.get(
        f"/api/v1/chat/sessions/{cancellation_env.conversation.id}",
        params={"latest": True, "after_sequence": after_sequence},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == (
        "latest cannot be combined with after_sequence greater than zero"
    )


async def test_latest_detail_still_requires_workspace_access(cancellation_env):
    cancellation_env.identity = RequestIdentity(subject="outsider")
    response = await cancellation_env.client.get(
        f"/api/v1/chat/sessions/{cancellation_env.conversation.id}", params={"latest": True}
    )
    assert response.status_code == 403


@pytest.mark.parametrize("stage", ["post_commit_refresh", "model_session_setup", "commit"])
async def test_cancel_during_turn_setup_finalizes_committed_turn_without_starting_runner(
    cancellation_env, monkeypatch, stage
):
    environment = cancellation_env
    prior = await _seed_turn(environment, request_id="prior", turn_status="completed")
    entered = asyncio.Event()
    release = asyncio.Event()
    runner = AsyncMock(return_value=chat.ChatReply(type="message", content="must not run"))
    async with environment.sessions() as db:
        original_refresh = db.refresh
        original_commit = db.commit
        original_model_session = service._model_session

        async def gated_refresh(instance, *args, **kwargs):
            if isinstance(instance, AgentConversationTurn) and not entered.is_set():
                await original_refresh(instance, *args, **kwargs)
                db.expire_all()
                entered.set()
                await release.wait()
                return
            return await original_refresh(instance, *args, **kwargs)

        async def gated_commit():
            await original_commit()
            if not entered.is_set():
                entered.set()
                await release.wait()

        async def gated_model_session(session):
            entered.set()
            await release.wait()
            return await original_model_session(session)

        if stage == "post_commit_refresh":
            monkeypatch.setattr(db, "refresh", gated_refresh)
        elif stage == "commit":
            monkeypatch.setattr(db, "commit", gated_commit)
        else:
            monkeypatch.setattr(service, "_model_session", gated_model_session)
        send = asyncio.create_task(
            service.send_message(
                db,
                environment.identity,
                environment.conversation.id,
                request_id="pending",
                content="hello",
                context=None,
                chat_runner=runner,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert (await _read_turn(environment)).status == "running"
            send.cancel()
            if stage == "commit":
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(send, 3)
        finally:
            release.set()
            if not send.done():
                send.cancel()
            await asyncio.gather(send, return_exceptions=True)
    runner.assert_not_awaited()
    final = await _read_turn(environment)
    assert final.status == "failed"
    assert final.error_code == "cancelled"
    assert final.error_message == cancellation.CANCELLED_MESSAGE
    assert (await _cancel(environment)).status_code == 409
    restored_prior = await _read_turn(environment, "prior")
    assert restored_prior.status == prior.status
    assert restored_prior.response == prior.response
