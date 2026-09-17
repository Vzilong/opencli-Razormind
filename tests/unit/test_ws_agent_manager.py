"""Unit tests for backend/ws_agent_manager.py."""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import backend.ws_agent_manager as mgr


@pytest.fixture(autouse=True)
def clear_state():
    """Ensure module-level dicts are clean before each test."""
    mgr._connections.clear()
    mgr._pending.clear()
    mgr._pending_agent_tasks.clear()
    mgr._agent_task_callbacks.clear()
    mgr._agent_task_terminal_results.clear()
    mgr._task_owners.clear()
    mgr._retained_agent_tasks.clear()
    mgr._agent_task_status_probes.clear()
    mgr._terminal_control_requests.clear()
    mgr._terminal_attachments.clear()
    mgr._terminal_routes.clear()
    yield
    mgr._connections.clear()
    mgr._pending.clear()
    mgr._pending_agent_tasks.clear()
    mgr._agent_task_callbacks.clear()
    mgr._agent_task_terminal_results.clear()
    mgr._task_owners.clear()
    mgr._retained_agent_tasks.clear()
    mgr._agent_task_status_probes.clear()
    mgr._terminal_control_requests.clear()
    mgr._terminal_attachments.clear()
    mgr._terminal_routes.clear()


# ── register / unregister / queries ───────────────────────────────────────────


def test_register_and_is_connected():
    ws = MagicMock()
    mgr.register_connection("http://agent:19823", ws)
    assert mgr.is_connected("http://agent:19823") is True


def test_unregister_removes_connection():
    ws = MagicMock()
    mgr.register_connection("http://agent:19823", ws)
    mgr.unregister_connection("http://agent:19823")
    assert mgr.is_connected("http://agent:19823") is False


def test_unregister_nonexistent_no_error():
    mgr.unregister_connection("http://nonexistent:19823")  # must not raise


def test_list_connected_returns_urls():
    mgr.register_connection("http://a:1", MagicMock())
    mgr.register_connection("http://b:2", MagicMock())
    connected = mgr.list_connected()
    assert "http://a:1" in connected
    assert "http://b:2" in connected
    assert len(connected) == 2


def test_is_connected_false_for_unknown():
    assert mgr.is_connected("http://nobody:19823") is False


def test_terminal_binary_envelope_is_bounded_and_strict():
    session_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())
    frame = mgr.encode_terminal_frame(1, session_id, attachment_id, 7, b"hello")
    assert mgr.decode_terminal_frame(frame) == (1, session_id, attachment_id, 7, b"hello")
    with pytest.raises(ValueError, match="malformed"):
        mgr.decode_terminal_frame(frame[:20])
    with pytest.raises(ValueError, match="payload"):
        mgr.encode_terminal_frame(1, session_id, attachment_id, 0, b"")
    with pytest.raises(ValueError, match="payload"):
        mgr.encode_terminal_frame(1, session_id, attachment_id, 0, b"x" * (64 * 1024 + 1))


async def test_terminal_attach_routes_replay_input_resize_and_takeover_only_for_owner():
    agent_url = "http://terminal-agent:19823"
    session_id = str(uuid.uuid4())
    controller_id = str(uuid.uuid4())
    owner = AsyncMock()
    foreign = AsyncMock()
    frames = []
    mgr.register_connection(agent_url, owner)

    async def reply(message):
        frames.append(message)
        if message["type"] in {"terminal_attach", "terminal_takeover"}:
            mgr.resolve_terminal_response(
                {
                    "request_id": message["request_id"],
                    "response": {"status": "active", "controls": True},
                },
                owner,
            )

    owner.send_json.side_effect = reply
    attachment = await mgr.attach_native_terminal(
        agent_url,
        session_id,
        controller_id,
        native_chat_authorized=True,
    )
    assert frames[0]["type"] == "terminal_attach"
    assert frames[0]["attachment_id"] == attachment.attachment_id

    await mgr.resolve_terminal_event(
        {
            "session_id": session_id,
            "attachment_id": attachment.attachment_id,
            "event": {"type": "snapshot_begin", "sequence": 3},
        },
        foreign,
    )
    assert attachment.queue.empty()
    await mgr.resolve_terminal_event(
        {
            "session_id": session_id,
            "attachment_id": attachment.attachment_id,
            "event": {"type": "snapshot_begin", "sequence": 3},
        },
        owner,
    )
    assert await attachment.queue.get() == {"type": "snapshot_begin", "sequence": 3}

    output = mgr.encode_terminal_frame(
        2, session_id, attachment.attachment_id, 4, b"ansi-output"
    )
    await mgr.resolve_terminal_binary(output, foreign)
    assert attachment.queue.empty()
    await mgr.resolve_terminal_binary(output, owner)
    assert await attachment.queue.get() == b"ansi-output"

    await mgr.send_native_terminal_input(attachment, b"input")
    binary = owner.send_bytes.await_args.args[0]
    assert mgr.decode_terminal_frame(binary) == (
        1,
        session_id,
        attachment.attachment_id,
        0,
        b"input",
    )
    await mgr.resize_native_terminal(attachment, cols=120, rows=40)
    assert frames[-1]["type"] == "terminal_resize"
    takeover = await mgr.takeover_native_terminal(attachment, controller_id)
    assert takeover["controls"] is True


async def test_terminal_attach_requires_explicit_authorized_binding():
    with pytest.raises(PermissionError):
        await mgr.attach_native_terminal(
            "http://agent",
            str(uuid.uuid4()),
            str(uuid.uuid4()),
        )


async def test_terminal_attach_accepts_stopping_session_for_observation():
    agent_url = "http://terminal-agent:19823"
    session_id = str(uuid.uuid4())
    owner = AsyncMock()
    mgr.register_connection(agent_url, owner)

    async def reply(message):
        mgr.resolve_terminal_response(
            {
                "request_id": message["request_id"],
                "response": {"status": "stopping", "controls": True},
            },
            owner,
        )

    owner.send_json.side_effect = reply
    attachment = await mgr.attach_native_terminal(
        agent_url,
        session_id,
        str(uuid.uuid4()),
        native_chat_authorized=True,
    )

    assert attachment.session_id == session_id


async def test_terminal_attachment_count_is_bounded(monkeypatch):
    session_id = str(uuid.uuid4())
    monkeypatch.setattr(mgr, "_TERMINAL_ATTACHMENTS_PER_SESSION", 1)
    mgr._terminal_attachments["existing"] = mgr.TerminalAttachment(
        agent_url="http://terminal-agent:19823",
        session_id=session_id,
        attachment_id="existing",
        queue=asyncio.Queue(),
    )

    with pytest.raises(RuntimeError, match="limit"):
        await mgr.attach_native_terminal(
            "http://terminal-agent:19823",
            session_id,
            str(uuid.uuid4()),
            native_chat_authorized=True,
        )


async def test_terminal_exit_event_releases_center_route():
    agent_url = "http://terminal-agent:19823"
    session_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())
    owner = AsyncMock()
    mgr.register_connection(agent_url, owner)
    attachment = mgr.TerminalAttachment(
        agent_url=agent_url,
        session_id=session_id,
        attachment_id=attachment_id,
        queue=asyncio.Queue(),
    )
    mgr._terminal_attachments[attachment_id] = attachment
    mgr._terminal_routes[session_id] = agent_url

    await mgr.resolve_terminal_event(
        {
            "session_id": session_id,
            "event": {"type": "exit", "status": "exited", "cleanup_complete": True},
        },
        owner,
    )

    assert session_id not in mgr._terminal_routes
    assert (await attachment.queue.get())["type"] == "exit"


async def test_slow_terminal_attachment_is_detached_instead_of_blocking_fleet():
    agent_url = "http://terminal-agent:19823"
    session_id = str(uuid.uuid4())
    attachment_id = str(uuid.uuid4())
    owner = AsyncMock()
    mgr.register_connection(agent_url, owner)
    attachment = mgr.TerminalAttachment(
        agent_url=agent_url,
        session_id=session_id,
        attachment_id=attachment_id,
        queue=asyncio.Queue(maxsize=1),
    )
    mgr._terminal_attachments[attachment_id] = attachment
    mgr._terminal_routes[session_id] = agent_url
    attachment.queue.put_nowait(b"stale")

    await mgr.resolve_terminal_binary(
        mgr.encode_terminal_frame(2, session_id, attachment_id, 2, b"new"), owner
    )

    assert attachment_id not in mgr._terminal_attachments
    assert await attachment.queue.get() == {
        "type": "transport",
        "status": "reconnecting",
        "recoverable": True,
    }
    owner.send_json.assert_awaited_once_with(
        {
            "type": "terminal_detach",
            "session_id": session_id,
            "attachment_id": attachment_id,
        }
    )


def _status_reply(frame, result):
    return {
        "type": "agent_task_status_result",
        "request_id": frame["request_id"],
        "task_id": frame["task_id"],
        "result": result,
    }


@pytest.mark.parametrize("outcome", ["done", "cancelled", "running", "unknown", "unclean"])
async def test_status_probe_returns_only_authenticated_bounded_evidence(outcome):
    owner = AsyncMock()
    mgr.register_connection("http://agent", owner)
    if outcome in ("running", "unknown"):
        result = {"status": outcome, "private": "must not escape"}
        expected = {"status": outcome}
    else:
        result = {
            "type": "done" if outcome == "done" else "error",
            "task_id": "original",
            "cleanup_complete": outcome != "unclean",
            "error_type": "CancelledError",
            "result": {"text": "private output"},
            "message": "private diagnostic",
        }
        expected = {
            key: result[key] for key in ("type", "task_id", "cleanup_complete", "error_type")
        }
        if outcome == "unclean":
            expected = {"status": "unknown"}

    async def reply(frame):
        assert frame["type"] == "agent_task_status"
        assert frame["task_id"] == "original"
        assert frame["request_id"] != "original"
        mgr.resolve_agent_task_status(frame["request_id"], _status_reply(frame, result), owner)

    owner.send_json.side_effect = reply
    assert await mgr.probe_agent_task("http://agent", "original") == expected
    assert not mgr._agent_task_status_probes
    assert not mgr._pending_agent_tasks
    owner.send_json.assert_awaited_once()


@pytest.mark.parametrize(
    "forgery", ["foreign", "old", "outer-task", "inner-task", "probe-id", "request-id", "boolean"]
)
async def test_status_probe_rejects_wrong_owner_or_correlation(forgery):
    owner, foreign = AsyncMock(), AsyncMock()
    mgr.register_connection("http://agent", owner)
    frames = []
    sent = asyncio.Event()

    async def reply(frame):
        frames.append(frame)
        result = {"type": "done", "task_id": "original", "cleanup_complete": True}
        message = _status_reply(frame, result)
        source = owner
        request_id = frame["request_id"]
        if forgery == "foreign":
            source = foreign
        elif forgery == "old":
            mgr._connections["http://agent"] = foreign
        elif forgery == "outer-task":
            message["task_id"] = "another-task"
        elif forgery == "inner-task":
            result["task_id"] = "another-task"
        elif forgery == "probe-id":
            message["request_id"] = "another-probe"
        elif forgery == "request-id":
            request_id = "another-probe"
        else:
            result["cleanup_complete"] = 1
        mgr.resolve_agent_task_status(request_id, message, source)
        sent.set()

    owner.send_json.side_effect = reply
    probe = asyncio.create_task(mgr.probe_agent_task("http://agent", "original", timeout=1))
    await asyncio.wait_for(sent.wait(), 1)
    if forgery == "boolean":
        assert await probe == {"status": "unknown"}
    else:
        assert not probe.done()
        mgr._connections["http://agent"] = owner
        frame = frames[0]
        mgr.resolve_agent_task_status(
            frame["request_id"], _status_reply(frame, {"status": "running"}), owner
        )
        assert await probe == {"status": "running"}
    assert not mgr._agent_task_status_probes


@pytest.mark.parametrize(
    "failure",
    ["timeout", "blocked-send", "send-error", "disconnect", "replacement", "local-cancel"],
)
async def test_status_probe_failure_keeps_execution_unconfirmed_and_releases_waiter(failure):
    owner = AsyncMock()
    mgr.register_connection("http://agent", owner)
    sent = asyncio.Event()
    frames = []

    async def send(frame):
        frames.append(frame)
        sent.set()
        if failure == "blocked-send":
            await asyncio.Future()
        elif failure == "send-error":
            raise OSError("disconnected")

    owner.send_json.side_effect = send
    probe = asyncio.create_task(mgr.probe_agent_task("http://agent", "original", timeout=0.02))
    await asyncio.wait_for(sent.wait(), 1)
    if failure == "disconnect":
        mgr.unregister_connection("http://agent", owner)
    elif failure == "replacement":
        mgr.register_connection("http://agent", AsyncMock())
    elif failure == "local-cancel":
        probe.cancel()
    with pytest.raises(
        asyncio.CancelledError if failure == "local-cancel" else mgr.AgentTaskUnresolvedError
    ):
        await asyncio.wait_for(probe, 1)
    assert len(frames) == 1
    assert frames[0]["type"] == "agent_task_status"
    assert not mgr._agent_task_status_probes
    assert not mgr._retained_agent_tasks


async def test_status_probe_disconnected_and_capacity_fail_closed(monkeypatch):
    with pytest.raises(mgr.AgentTaskUnresolvedError):
        await mgr.probe_agent_task("http://missing", "original")
    owner = AsyncMock()
    mgr.register_connection("http://agent", owner)
    monkeypatch.setattr(mgr, "_STATUS_PROBE_LIMIT", 0)
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await mgr.probe_agent_task("http://agent", "original")
    assert raised.value.reason == "status_probe_capacity"
    owner.send_json.assert_not_awaited()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf")])
async def test_status_probe_rejects_unbounded_timeouts(timeout):
    with pytest.raises(ValueError):
        await mgr.probe_agent_task("http://agent", "original", timeout=timeout)


# ── dispatch_collect: not connected ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_collect_raises_when_not_connected():
    with pytest.raises(RuntimeError, match="No active WS connection"):
        await mgr.dispatch_collect("http://missing:19823", "site", "cmd", {}, [], "json", "bridge")


# ── dispatch_collect: success ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_collect_success():
    """dispatch_collect sends JSON to WS and resolves the future when result arrives."""
    ws = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)

    result_payload = {"success": True, "items": [{"id": 1}], "error": None}

    async def fake_send_json(payload):
        # Simulate agent responding right away
        request_id = payload["request_id"]
        asyncio.get_running_loop().call_soon(mgr.resolve_response, request_id, result_payload)

    ws.send_json = AsyncMock(side_effect=fake_send_json)

    result = await mgr.dispatch_collect(
        "http://agent:19823", "bilibili", "hot", {}, [], "json", "bridge", timeout=5.0
    )

    assert result["success"] is True
    assert result["items"] == [{"id": 1}]
    ws.send_json.assert_awaited_once()
    sent = ws.send_json.call_args[0][0]
    assert sent["type"] == "collect"
    assert sent["site"] == "bilibili"
    assert sent["command"] == "hot"


# ── dispatch_collect: timeout ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_collect_timeout():
    """dispatch_collect raises TimeoutError when agent does not respond."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()  # send succeeds but no resolve comes back
    mgr.register_connection("http://agent:19823", ws)

    with pytest.raises(TimeoutError, match="did not respond"):
        await mgr.dispatch_collect(
            "http://agent:19823", "site", "cmd", {}, [], "json", "bridge", timeout=0.05
        )


# ── dispatch_collect: pending cleaned up on timeout ──────────────────────────


@pytest.mark.asyncio
async def test_dispatch_collect_pending_cleaned_up_on_timeout():
    """After a timeout, the pending future is removed from _pending."""
    ws = AsyncMock()
    ws.send_json = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)

    with pytest.raises(TimeoutError):
        await mgr.dispatch_collect(
            "http://agent:19823", "s", "c", {}, [], "json", "bridge", timeout=0.05
        )

    assert len(mgr._pending) == 0


# ── resolve_response: unknown request_id ──────────────────────────────────────


def test_resolve_response_unknown_request_id_no_error():
    """resolve_response with unknown request_id must not raise."""
    mgr.resolve_response("nonexistent-id", {"success": True, "items": []})


# ── resolve_response: already-done future ─────────────────────────────────────


def test_resolve_response_already_done_future_no_error():
    """resolve_response on a future that's already resolved must not raise."""
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    fut.set_result({"done": True})
    mgr._pending["req-done"] = fut
    # Should log a warning but not raise
    mgr.resolve_response("req-done", {"success": True})
    loop.close()


# ── send_agent_task: not connected ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_agent_task_raises_when_not_connected():
    with pytest.raises(RuntimeError, match="No active WS connection"):
        await mgr.send_agent_task("http://missing:19823", {"runtime": "pi"}, lambda e: None)


# ── send_agent_task: happy path, N events then result (sync on_event) ──────


@pytest.mark.asyncio
async def test_send_agent_task_happy_path_sync_on_event_order_preserved():
    ws = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)

    received_events = []
    sent_frames = []

    async def fake_send(payload):
        sent_frames.append(payload)
        if payload["type"] != "agent_task":
            return
        request_id = payload["request_id"]

        async def drive_events():
            await mgr.resolve_agent_event(
                request_id, {"event": {"type": "started", "task_id": request_id}}
            )
            await mgr.resolve_agent_event(
                request_id, {"event": {"type": "text", "task_id": request_id, "text": "hi"}}
            )
            mgr.resolve_agent_result(
                request_id, {"result": {"type": "done", "task_id": request_id, "result": {}}}
            )

        asyncio.ensure_future(drive_events())

    ws.send_json = AsyncMock(side_effect=fake_send)

    def on_event(event):
        received_events.append(event)

    result = await mgr.send_agent_task(
        "http://agent:19823", {"runtime": "pi", "workflow": "w"}, on_event, timeout=5.0
    )

    assert result["type"] == "done"
    assert [e["type"] for e in received_events] == ["started", "text"]
    assert sent_frames[0]["type"] == "agent_task"
    assert sent_frames[0]["runtime"] == "pi"
    assert "request_id" in sent_frames[0]


# ── send_agent_task: happy path with async on_event ─────────────────────────


@pytest.mark.asyncio
async def test_send_agent_task_supports_async_on_event():
    ws = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)

    received_events = []

    async def on_event(event):
        received_events.append(event)

    async def fake_send(payload):
        if payload["type"] == "agent_task":
            request_id = payload["request_id"]
            await mgr.resolve_agent_event(
                request_id,
                {"event": {"type": "started", "task_id": request_id}},
            )
            mgr.resolve_agent_result(
                request_id,
                {
                    "result": {
                        "type": "done",
                        "task_id": request_id,
                        "result": {"ok": True},
                    }
                },
            )

    ws.send_json = AsyncMock(side_effect=fake_send)

    result = await mgr.send_agent_task(
        "http://agent:19823", {"runtime": "pi"}, on_event, timeout=5.0
    )

    assert result["result"] == {"ok": True}
    assert len(received_events) == 1
    assert received_events[0]["type"] == "started"


@pytest.mark.asyncio
async def test_ack_required_event_is_acknowledged_only_after_callback_persists():
    ws = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)
    persisted = asyncio.Event()
    sent_frames = []

    async def on_event(event):
        assert event["evidence"]["kind"] == "doubao.capture.pre_cleanup"
        await asyncio.sleep(0)
        persisted.set()

    async def fake_send(payload):
        sent_frames.append(payload)
        if payload["type"] == "agent_event_ack":
            assert persisted.is_set()
            return
        if payload["type"] != "agent_task":
            return
        request_id = payload["request_id"]

        async def drive_task():
            await mgr.resolve_agent_event(
                request_id,
                {
                    "event_id": "event-1",
                    "ack_required": True,
                    "event": {
                        "type": "evidence",
                        "evidence": {"kind": "doubao.capture.pre_cleanup"},
                    },
                },
                source_ws=ws,
            )
            mgr.resolve_agent_result(
                request_id,
                {"result": {"type": "done", "task_id": request_id, "result": {}}},
            )

        asyncio.create_task(drive_task())

    ws.send_json = AsyncMock(side_effect=fake_send)

    result = await mgr.send_agent_task(
        "http://agent:19823", {"runtime": "bbx"}, on_event, timeout=5.0
    )

    assert result["type"] == "done"
    assert [frame["type"] for frame in sent_frames] == ["agent_task", "agent_event_ack"]
    assert sent_frames[1] == {
        "type": "agent_event_ack",
        "request_id": sent_frames[0]["request_id"],
        "event_id": "event-1",
        "status": "persisted",
    }


@pytest.mark.asyncio
async def test_ack_required_event_callback_failure_cancels_without_acknowledging():
    ws = AsyncMock()
    mgr.register_connection("http://agent:19823", ws)
    sent_frames = []

    def on_event(_event):
        raise OSError("evidence spool unavailable")

    async def fake_send(payload):
        sent_frames.append(payload)
        if payload["type"] != "agent_task":
            return
        request_id = payload["request_id"]

        async def deliver_evidence():
            await mgr.resolve_agent_event(
                request_id,
                {
                    "event_id": "event-1",
                    "ack_required": True,
                    "event": {
                        "type": "evidence",
                        "evidence": {"kind": "doubao.capture.pre_cleanup"},
                    },
                },
                source_ws=ws,
            )

        asyncio.create_task(deliver_evidence())

    ws.send_json = AsyncMock(side_effect=fake_send)

    with pytest.raises(OSError, match="spool unavailable"):
        await mgr.send_agent_task("http://agent:19823", {"runtime": "bbx"}, on_event, timeout=5.0)

    assert [frame["type"] for frame in sent_frames] == ["agent_task", "cancel"]


# ── send_agent_task: timeout ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_agent_task_timeout():
    ws = AsyncMock()
    ws.send_json = AsyncMock()  # send succeeds, but no agent_result ever arrives
    mgr.register_connection("http://agent:19823", ws)

    with pytest.raises(TimeoutError, match="did not complete agent_task"):
        await mgr.send_agent_task(
            "http://agent:19823",
            {"runtime": "pi"},
            lambda e: None,
            timeout=0.05,
        )

    sent_frames = [call.args[0] for call in ws.send_json.await_args_list]
    assert [frame["type"] for frame in sent_frames] == ["agent_task", "cancel"]
    assert sent_frames[1]["request_id"] == sent_frames[0]["request_id"]
    # Bookkeeping cleaned up after timeout.
    assert len(mgr._pending_agent_tasks) == 0
    assert len(mgr._agent_task_callbacks) == 0


# ── send_agent_task: disconnect fails pending fast ──────────────────────────


@pytest.mark.asyncio
async def test_unregister_connection_fails_pending_agent_tasks_fast():
    ws = AsyncMock()
    ws.send_json = AsyncMock()  # send succeeds, agent then disconnects with no result
    mgr.register_connection("http://agent:19823", ws)

    async def disconnect_soon():
        await asyncio.sleep(0.01)
        mgr.unregister_connection("http://agent:19823")

    disconnect_task = asyncio.ensure_future(disconnect_soon())
    result = await mgr.send_agent_task(
        "http://agent:19823", {"runtime": "pi"}, lambda e: None, timeout=5.0
    )
    await disconnect_task

    assert result["type"] == "error"
    assert result["error_type"] == "AgentDisconnected"


@pytest.mark.asyncio
async def test_unregister_connection_leaves_other_agents_pending_tasks_alone():
    ws_a = AsyncMock()
    ws_b = AsyncMock()
    mgr.register_connection("http://agent-a:1", ws_a)
    mgr.register_connection("http://agent-b:2", ws_b)

    ws_b.send_json = AsyncMock()  # never resolves — held pending deliberately
    task_b = asyncio.ensure_future(
        mgr.send_agent_task("http://agent-b:2", {"runtime": "pi"}, lambda e: None, timeout=5.0)
    )
    await asyncio.sleep(0.01)  # let task_b register its pending future

    mgr.unregister_connection("http://agent-a:1")

    assert not task_b.done()
    task_b.cancel()
    try:
        await task_b
    except asyncio.CancelledError:
        pass


def test_unregister_connection_no_pending_agent_tasks_no_error():
    mgr.unregister_connection("http://nonexistent:19823")  # must not raise


# ── resolve_agent_event / resolve_agent_result: unknown request_id ─────────


@pytest.mark.asyncio
async def test_resolve_agent_event_unknown_request_id_no_error():
    await mgr.resolve_agent_event("nonexistent-id", {"event": {"type": "text"}})


def test_resolve_agent_result_unknown_request_id_no_error():
    mgr.resolve_agent_result("nonexistent-id", {"result": {"type": "done"}})


def test_resolve_agent_result_already_done_future_no_error():
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    fut.set_result({"type": "done"})
    mgr._pending_agent_tasks["req-done"] = fut
    mgr.resolve_agent_result("req-done", {"result": {"type": "done"}})
    loop.close()


@pytest.mark.asyncio
async def test_resolve_agent_event_callback_exception_fails_pending_task():
    def bad_on_event(event):
        raise ValueError("boom")

    fut = asyncio.get_running_loop().create_future()
    mgr._pending_agent_tasks["req-1"] = fut
    mgr._agent_task_callbacks["req-1"] = (bad_on_event, "http://agent:1")
    await mgr.resolve_agent_event("req-1", {"event": {"type": "text"}})

    with pytest.raises(ValueError, match="boom"):
        await fut


@pytest.mark.asyncio
async def test_agent_event_rejects_foreign_socket_before_persist_or_ack(monkeypatch):
    from unittest.mock import AsyncMock

    callback = AsyncMock()
    owner = AsyncMock()
    foreign = AsyncMock()
    monkeypatch.setitem(mgr._connections, "owner-agent", owner)
    monkeypatch.setitem(mgr._agent_task_callbacks, "owned-request", (callback, "owner-agent"))
    frame = {"event": {"type": "evidence"}, "ack_required": True, "event_id": "event-1"}
    await mgr.resolve_agent_event("owned-request", frame, source_ws=foreign)
    callback.assert_not_awaited()
    foreign.send_json.assert_not_awaited()
    owner.send_json.assert_not_awaited()
    await mgr.resolve_agent_event("owned-request", frame, source_ws=owner)
    callback.assert_awaited_once_with(frame["event"])
    owner.send_json.assert_awaited_once()


@pytest.fixture
async def confirmed_agent():
    agent = SimpleNamespace(
        url="http://confirmed-agent:19823",
        ws=AsyncMock(),
        frames=[],
        started=asyncio.Event(),
        cancel_sent=asyncio.Event(),
        task=None,
    )

    async def send_json(frame):
        agent.frames.append(frame)
        if frame["type"] == "agent_task":
            agent.started.set()
        elif frame["type"] == "cancel":
            agent.cancel_sent.set()

    agent.ws.send_json.side_effect = send_json
    mgr.register_connection(agent.url, agent.ws)
    yield agent
    if agent.task is not None:
        if not agent.task.done():
            agent.task.cancel()
            mgr.unregister_connection(agent.url, agent.ws)
        await asyncio.gather(agent.task, return_exceptions=True)


async def _start_confirmed_dispatch(agent, **kwargs):
    agent.task = asyncio.create_task(
        mgr.send_agent_task(
            agent.url,
            {"runtime": "codex"},
            kwargs.pop("on_event", lambda event: None),
            require_cancel_ack=True,
            cancel_ack_timeout=0.1,
            **kwargs,
        )
    )
    await asyncio.wait_for(agent.started.wait(), 1)
    return agent.task


def _confirm_remote_terminal(agent, *, source_ws=None, **fields):
    request_id = agent.frames[0]["request_id"]
    result = {"type": "done", "task_id": request_id, "cleanup_complete": True, **fields}
    mgr.resolve_agent_result(
        request_id, {"result": result}, source_ws=agent.ws if source_ws is None else source_ws
    )
    return result


async def test_confirmed_dispatch_returns_normal_completed_terminal(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    expected = _confirm_remote_terminal(agent, result={"text": "completed"})
    assert await asyncio.wait_for(pending, 1) == expected
    assert agent.frames[0]["require_cancel_ack"] is True
    assert [frame["type"] for frame in agent.frames] == ["agent_task"]
    assert not mgr._agent_task_terminal_results


async def test_late_confirmed_cleanup_is_retained_without_native_output(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent, timeout=0.01)
    with pytest.raises(mgr.AgentTaskUnresolvedError):
        await pending
    request_id = agent.frames[0]["request_id"]
    agent_key = mgr.agent_task_key(agent.url)
    assert mgr.confirmed_agent_terminal(agent_key, request_id) is None
    _confirm_remote_terminal(agent, source_ws=AsyncMock())
    _confirm_remote_terminal(agent, task_id="foreign-request")
    _confirm_remote_terminal(agent, cleanup_complete=False)
    assert mgr.confirmed_agent_terminal(agent_key, request_id) is None
    _confirm_remote_terminal(
        agent, type="error", error_type="CancelledError", result={"text": "private" * 10000}
    )
    expected = {
        "task_id": request_id,
        "type": "error",
        "cleanup_complete": True,
        "error_type": "CancelledError",
    }
    assert mgr.confirmed_agent_terminal(agent_key, request_id) == expected
    assert mgr.confirmed_agent_terminal("other-node", request_id) is None
    mgr.forget_agent_terminal("other-node", request_id)
    assert mgr.confirmed_agent_terminal(agent_key, request_id) == expected
    mgr.forget_agent_terminal(agent_key, request_id)
    assert mgr.confirmed_agent_terminal(agent_key, request_id) is None


async def test_replaced_socket_cannot_supply_late_cleanup_proof(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent, timeout=0.01)
    with pytest.raises(mgr.AgentTaskUnresolvedError):
        await pending
    mgr.unregister_connection(agent.url, agent.ws)
    replacement = AsyncMock()
    mgr.register_connection(agent.url, replacement)
    _confirm_remote_terminal(agent)
    _confirm_remote_terminal(agent, source_ws=replacement)
    assert (
        mgr.confirmed_agent_terminal(mgr.agent_task_key(agent.url), agent.frames[0]["request_id"])
        is None
    )


async def test_late_cleanup_cache_is_bounded_and_expiry_never_confirms(
    confirmed_agent, monkeypatch
):
    agent = confirmed_agent
    monkeypatch.setattr(mgr, "_RETAINED_TASK_LIMIT", 2)
    request_ids = []
    for _attempt in range(3):
        agent.started.clear()
        pending = await _start_confirmed_dispatch(agent)
        frame = agent.frames[-1]
        request_id = frame["request_id"]
        request_ids.append(request_id)
        mgr.resolve_agent_result(
            request_id,
            {
                "result": {
                    "type": "done",
                    "task_id": request_id,
                    "cleanup_complete": True,
                }
            },
            source_ws=agent.ws,
        )
        await pending
    assert len(mgr._retained_agent_tasks) == 2
    assert request_ids[0] not in mgr._retained_agent_tasks
    for entry in mgr._retained_agent_tasks.values():
        entry.expires_at = 0
    assert mgr.confirmed_agent_terminal(mgr.agent_task_key(agent.url), request_ids[-1]) is None
    assert not mgr._retained_agent_tasks


async def test_strict_dispatch_persists_correlation_before_sending(confirmed_agent):
    agent = confirmed_agent
    observed = []

    async def observer(url, request_id):
        assert agent.frames == []
        observed.append((url, request_id))

    with mgr.observe_agent_dispatch(observer):
        pending = await _start_confirmed_dispatch(agent)
    assert observed == [(agent.url, agent.frames[0]["request_id"])]
    _confirm_remote_terminal(agent)
    await pending
    assert mgr._dispatch_observer.get() is None


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_correlation_persistence_never_dispatches_or_cancels(
    confirmed_agent, cancelled
):
    agent = confirmed_agent

    async def observer(url, request_id):
        if cancelled:
            raise asyncio.CancelledError
        raise RuntimeError("persistence failed")

    with mgr.observe_agent_dispatch(observer):
        with pytest.raises(asyncio.CancelledError if cancelled else RuntimeError):
            await mgr.send_agent_task(agent.url, {}, lambda event: None, require_cancel_ack=True)
    assert agent.frames == []
    assert not mgr._pending_agent_tasks
    assert not mgr._retained_agent_tasks


async def test_confirmed_cancel_waits_for_cleanup_even_after_repeated_cancellation(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    request_id = agent.frames[0]["request_id"]
    remote_future = mgr._pending_agent_tasks[request_id]
    pending.cancel()
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    assert not remote_future.cancelled()
    assert request_id in mgr._agent_task_callbacks
    _confirm_remote_terminal(agent, type="error", error_type="CancelledError")
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(pending, 1)
    assert [frame["type"] for frame in agent.frames] == ["agent_task", "cancel"]
    assert not mgr._pending_agent_tasks
    assert not mgr._agent_task_terminal_results


@pytest.mark.parametrize("terminal_type", ["done", "error"])
async def test_confirmed_completion_wins_local_cancel_race(confirmed_agent, terminal_type):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    pending.cancel()
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    expected = _confirm_remote_terminal(agent, type=terminal_type, error_type="ModelError")
    assert await asyncio.wait_for(pending, 1) == expected


@pytest.mark.parametrize(
    "invalid_terminal",
    [
        None,
        {"cleanup_complete": False},
        {"cleanup_complete": 1},
        {"task_id": "another-task"},
        {"type": "state"},
        {"type": []},
    ],
)
async def test_unconfirmed_cancel_is_unresolved_not_cancelled(confirmed_agent, invalid_terminal):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    request_id = agent.frames[0]["request_id"]
    pending.cancel()
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    if invalid_terminal is not None:
        _confirm_remote_terminal(agent, **invalid_terminal)
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value.code == "remote_execution_unconfirmed"
    assert raised.value.reason == "cancel_ack_timeout"
    assert raised.value.request_id == request_id
    assert raised.value.agent_url == agent.url
    assert "may still be running" in str(raised.value)
    assert not mgr._agent_task_terminal_results


async def test_cancel_rejects_foreign_socket_and_streamed_terminal_as_cleanup_ack(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    pending.cancel()
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    result = _confirm_remote_terminal(agent, source_ws=AsyncMock())
    request_id = agent.frames[0]["request_id"]
    await mgr.resolve_agent_event(request_id, {"event": result}, source_ws=agent.ws)
    assert not mgr._agent_task_terminal_results[request_id].done()
    with pytest.raises(mgr.AgentTaskUnresolvedError):
        await asyncio.wait_for(pending, 1)


async def test_confirmed_dispatch_timeout_without_ack_is_unresolved(confirmed_agent):
    pending = await _start_confirmed_dispatch(confirmed_agent, timeout=0.01)
    await asyncio.wait_for(confirmed_agent.cancel_sent.wait(), 1)
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value.reason == "cancel_ack_timeout"


async def test_confirmed_dispatch_timeout_is_terminal_only_after_cleanup_ack(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent, timeout=0.01)
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    assert not pending.done()
    _confirm_remote_terminal(agent, type="error", error_type="CancelledError")
    with pytest.raises(TimeoutError, match="did not complete agent_task"):
        await asyncio.wait_for(pending, 1)


async def test_stalled_cancel_transport_is_bounded_and_unresolved(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    blocked = asyncio.Event()

    async def stalled_send(frame):
        agent.cancel_sent.set()
        await blocked.wait()

    agent.ws.send_json.side_effect = stalled_send
    pending.cancel()
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value.reason == "cancel_ack_timeout"


async def test_normal_terminal_without_cleanup_proof_cannot_finish_strict_dispatch(confirmed_agent):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    request_id = agent.frames[0]["request_id"]
    mgr.resolve_agent_result(
        request_id, {"result": {"type": "done", "task_id": request_id}}, source_ws=agent.ws
    )
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    with pytest.raises(mgr.AgentTaskUnresolvedError):
        await asyncio.wait_for(pending, 1)


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_confirmed_dispatch_disconnect_never_confirms_stop(confirmed_agent, cancel_first):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    if cancel_first:
        pending.cancel()
        await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    mgr.unregister_connection(agent.url, agent.ws)
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value.reason == "agent_disconnected"


@pytest.mark.parametrize("send_failure", [OSError("socket closed"), asyncio.CancelledError()])
async def test_confirmed_cancel_send_failure_is_unresolved(confirmed_agent, send_failure):
    agent = confirmed_agent
    pending = await _start_confirmed_dispatch(agent)
    agent.ws.send_json.side_effect = send_failure
    pending.cancel()
    with pytest.raises(mgr.AgentTaskUnresolvedError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value.reason == "cancel_transport_failed"


async def test_callback_failure_retains_independent_cleanup_future(confirmed_agent):
    agent = confirmed_agent
    failure = ValueError("persistence failed")
    pending = await _start_confirmed_dispatch(agent, on_event=AsyncMock(side_effect=failure))
    request_id = agent.frames[0]["request_id"]
    await mgr.resolve_agent_event(request_id, {"event": {"type": "text"}}, source_ws=agent.ws)
    await asyncio.wait_for(agent.cancel_sent.wait(), 1)
    assert not pending.done()
    _confirm_remote_terminal(agent, type="error", error_type="CancelledError")
    with pytest.raises(ValueError) as raised:
        await asyncio.wait_for(pending, 1)
    assert raised.value is failure


async def test_legacy_cancel_does_not_require_cleanup_ack(confirmed_agent):
    agent = confirmed_agent
    agent.task = asyncio.create_task(mgr.send_agent_task(agent.url, {}, lambda event: None))
    await asyncio.wait_for(agent.started.wait(), 1)
    agent.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(agent.task, 1)
    assert "require_cancel_ack" not in agent.frames[0]
    assert [frame["type"] for frame in agent.frames] == ["agent_task", "cancel"]


@pytest.mark.parametrize("duration", [0, -1, float("inf"), float("nan"), True])
@pytest.mark.parametrize("parameter", ["timeout", "cancel_ack_timeout"])
async def test_confirmed_dispatch_requires_bounded_deadlines(confirmed_agent, parameter, duration):
    agent = confirmed_agent
    with pytest.raises(ValueError, match="positive finite"):
        await mgr.send_agent_task(
            agent.url, {}, lambda event: None, require_cancel_ack=True, **{parameter: duration}
        )
    assert agent.frames == []
    assert not mgr._pending_agent_tasks
