"""Unit tests for backend/agent_server.py fleet-auth header attachment (ADR-0005)
and streaming agent-task dispatch (`_handle_ws_agent_task`).

Covers `_auth_headers()`, the Authorization header on `_register_with_center`'s
httpx POST calls, and the `additional_headers` -> `extra_headers` fallback in
`_register_via_ws`'s connect call.
"""

import asyncio
import json
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend import agent_runtime_dispatch, agent_server
from backend.agent_runtimes.base import RuntimeInvocationError


class _TerminalPath:
    def __init__(self, value: str) -> None:
        self.value = str(value)

    def is_absolute(self) -> bool:
        return self.value.startswith("/")

    def is_file(self) -> bool:
        return self.value in {"/runtime/codex", "/runtime/omp"}

    def resolve(self, *, strict: bool = False):
        del strict
        return self

    def is_relative_to(self, other) -> bool:
        return self.value.startswith(other.value.rstrip("/") + "/")

    def __eq__(self, other) -> bool:
        return isinstance(other, _TerminalPath) and self.value == other.value

    def __str__(self) -> str:
        return self.value


@pytest.fixture(autouse=True)
def _isolated_agent_task_tracking(monkeypatch):
    monkeypatch.setattr(agent_server, "_ACTIVE_AGENT_TASKS", {})
    monkeypatch.setattr(agent_server, "_AGENT_TASK_TERMINALS", OrderedDict())


# ── opencli binary resolution ────────────────────────────────────────────────


def test_resolve_bin_prefers_windows_cmd_shim(monkeypatch):
    resolved_cmd = r"C:\Users\Administrator\AppData\Roaming\npm\opencli.cmd"

    def fake_which(name: str) -> str | None:
        if name == "opencli.cmd":
            return resolved_cmd
        if name == "opencli.ps1":
            return r"C:\Users\Administrator\AppData\Roaming\npm\opencli.ps1"
        return None

    monkeypatch.setattr(agent_server, "_OPENCLI_BIN", "opencli")
    monkeypatch.setattr(agent_server.os, "name", "nt")
    monkeypatch.setattr(agent_server.shutil, "which", fake_which)

    assert agent_server._resolve_bin("cdp") == resolved_cmd


def test_resolve_bin_treats_empty_opencli_bin_as_default(monkeypatch):
    monkeypatch.setattr(agent_server, "_OPENCLI_BIN", "")
    monkeypatch.setattr(agent_server.os, "name", "posix")
    monkeypatch.setattr(agent_server.shutil, "which", lambda name: None)

    assert agent_server._resolve_bin("cdp") == "opencli"


def test_available_agent_runtimes_includes_packaged_script_host(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"capabilities": [{"runtime": "script-host"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent_server, "_RUNTIME_BUNDLE_MANIFEST", str(manifest))
    monkeypatch.setattr(agent_server, "available_runtimes", lambda: ["opentabs"])

    assert agent_server._available_agent_runtimes() == ["opentabs", "script-host"]


def test_manifest_runtime_defaults_to_opentabs(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"capabilities": [{"name": "tool"}]}), encoding="utf-8")
    monkeypatch.setattr(agent_server, "_RUNTIME_BUNDLE_MANIFEST", str(manifest))

    assert agent_server._bundle_declared_runtimes() == {"opentabs"}


# ── _auth_headers ────────────────────────────────────────────────────────────


def test_auth_headers_empty_when_no_token(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "")
    assert agent_server._auth_headers() == {}


def test_auth_headers_bearer_when_token_set(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "x")
    assert agent_server._auth_headers() == {"Authorization": "Bearer x"}


def test_collect_auth_fails_closed_and_accepts_exact_bearer(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "")
    with pytest.raises(HTTPException) as unset:
        agent_server._require_collect_auth(None)
    assert unset.value.status_code == 401

    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret")
    with pytest.raises(HTTPException):
        agent_server._require_collect_auth("Bearer wrong")
    agent_server._require_collect_auth("Bearer secret")


@pytest.mark.asyncio
async def test_direct_http_runtime_endpoint_rejects_codex(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret")
    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="codex",
        workflow="exec",
        instructions="inspect",
        input={"message": "inspect"},
        config={"cwd": "C:/controller/worktree"},
    )

    with pytest.raises(HTTPException) as blocked:
        await agent_server.invoke_runtime_http(request, "Bearer secret")

    assert blocked.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_http_runtime_endpoint_rejects_runtime_outside_installed_bundle(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret")
    monkeypatch.setattr(agent_server, "_bundle_declared_runtimes", lambda: {"script-host"})
    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="pi",
        workflow="inspect",
        instructions="inspect",
    )

    with pytest.raises(HTTPException) as blocked:
        await agent_server.invoke_runtime_http(request, "Bearer secret")

    assert blocked.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_http_runtime_endpoint_rejects_process_overrides(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret")
    monkeypatch.setattr(agent_server, "_bundle_declared_runtimes", lambda: {"script-host"})
    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="script-host",
        workflow="page.metadata",
        instructions="inspect",
        config={"pack": "page-basics", "binary": "python", "base_url": "https://attacker.example"},
    )

    with pytest.raises(HTTPException) as blocked:
        await agent_server.invoke_runtime_http(request, "Bearer secret")

    assert blocked.value.status_code == 400


# ── _register_with_center attaches Authorization header ────────────────────


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient capturing post() calls."""

    last_post_kwargs: dict = {}
    last_post_args: tuple = ()

    def __init__(self, **kwargs) -> None:
        self._kwargs = kwargs

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def post(self, url, **kwargs):
        _FakeAsyncClient.last_post_args = (url,)
        _FakeAsyncClient.last_post_kwargs = kwargs
        return _FakeResponse()


@pytest.mark.asyncio
async def test_register_with_center_attaches_authorization_header(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret-token")
    monkeypatch.setattr(agent_server, "_CENTRAL_API_URL", "http://center.example")
    monkeypatch.setattr(agent_server, "available_runtimes", lambda: ["opentabs"])
    monkeypatch.setattr(
        agent_server,
        "available_runtime_capabilities",
        lambda: {"opentabs": ["browser", "tool_events"]},
    )

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    await agent_server._register_with_center("http://agent.example:19823")

    assert _FakeAsyncClient.last_post_kwargs.get("headers") == {
        "Authorization": "Bearer secret-token"
    }
    assert _FakeAsyncClient.last_post_kwargs["json"]["runtimes"] == ["opentabs"]
    assert _FakeAsyncClient.last_post_kwargs["json"]["runtime_capabilities"] == {
        "opentabs": ["browser", "tool_events"]
    }
    assert _FakeAsyncClient.last_post_args == ("http://center.example/api/v1/nodes/register",)


@pytest.mark.asyncio
async def test_register_with_center_sends_empty_headers_without_token(monkeypatch):
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "")
    monkeypatch.setattr(agent_server, "_CENTRAL_API_URL", "http://center.example")
    monkeypatch.setattr(agent_server, "available_runtimes", lambda: [])
    monkeypatch.setattr(agent_server, "available_runtime_capabilities", lambda: {})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    await agent_server._register_with_center("http://agent.example:19823")

    assert _FakeAsyncClient.last_post_kwargs.get("headers") == {}


# ── _register_via_ws: additional_headers -> extra_headers fallback ─────────


class _FakeWsConnection:
    async def __aenter__(self):
        raise _StopTest("connected")

    async def __aexit__(self, *exc):
        return False


class _StopTest(BaseException):
    """Raised once the fake connect() context manager is entered, to short-
    circuit the reconnect loop after asserting the connect kwargs used.

    Deliberately subclasses BaseException (not Exception): _register_via_ws's
    reconnect loop catches `except Exception` and retries forever, which
    would swallow this and hang the test instead of stopping it.
    """


class _FakeWebsocketsModule:
    """Fake `websockets` module recording connect() kwargs and simulating the
    additional_headers TypeError path for older websockets versions."""

    def __init__(self, raise_on_additional_headers: bool) -> None:
        self.raise_on_additional_headers = raise_on_additional_headers
        self.calls: list[dict] = []

    def connect(self, uri, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_additional_headers and "additional_headers" in kwargs:
            raise TypeError("connect() got an unexpected keyword argument 'additional_headers'")
        return _FakeWsConnection()


@pytest.mark.asyncio
async def test_register_via_ws_uses_additional_headers_when_supported(monkeypatch):
    fake_ws = _FakeWebsocketsModule(raise_on_additional_headers=False)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret-token")
    monkeypatch.setattr(agent_server, "_CENTRAL_API_URL", "http://center.example")

    with pytest.raises(_StopTest):
        await agent_server._register_via_ws("http://agent.example:19823")

    assert len(fake_ws.calls) == 1
    assert fake_ws.calls[0]["additional_headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_register_via_ws_falls_back_to_extra_headers_on_typeerror(monkeypatch):
    fake_ws = _FakeWebsocketsModule(raise_on_additional_headers=True)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "secret-token")
    monkeypatch.setattr(agent_server, "_CENTRAL_API_URL", "http://center.example")

    with pytest.raises(_StopTest):
        await agent_server._register_via_ws("http://agent.example:19823")

    # First call attempted additional_headers and raised TypeError; second
    # (successful) call used extra_headers instead.
    assert len(fake_ws.calls) == 2
    assert "additional_headers" in fake_ws.calls[0]
    assert fake_ws.calls[1]["extra_headers"] == {"Authorization": "Bearer secret-token"}


@pytest.mark.asyncio
async def test_register_via_ws_no_headers_kwarg_without_token(monkeypatch):
    fake_ws = _FakeWebsocketsModule(raise_on_additional_headers=False)
    monkeypatch.setitem(__import__("sys").modules, "websockets", fake_ws)
    monkeypatch.setattr(agent_server, "_AGENT_API_TOKEN", "")
    monkeypatch.setattr(agent_server, "_CENTRAL_API_URL", "http://center.example")

    with pytest.raises(_StopTest):
        await agent_server._register_via_ws("http://agent.example:19823")

    assert len(fake_ws.calls) == 1
    assert "additional_headers" not in fake_ws.calls[0]
    assert "extra_headers" not in fake_ws.calls[0]


# ── _handle_ws_agent_task ────────────────────────────────────────────────────


class _FakeWs:
    """Minimal stand-in for the `websockets` connection: captures every
    `.send()` call as a parsed dict (mirrors real usage: agent_server always
    sends `json.dumps(...)`)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


class _StubAdapter:
    """Fake RuntimeAdapter whose invoke() yields a fixed event sequence."""

    def __init__(
        self,
        events: list[dict] | None = None,
        raise_exc: Exception | None = None,
        config_errors: list[str] | None = None,
        readiness_status: str = "ready",
        readiness_reason: str | None = None,
    ) -> None:
        self._events = events or []
        self._raise_exc = raise_exc
        self._config_errors = config_errors or []
        self._readiness_status = readiness_status
        self._readiness_reason = readiness_reason
        self.invoked = False

    def validate_config(self, config):
        return self._config_errors

    async def readiness(self, config):
        from types import SimpleNamespace

        return SimpleNamespace(
            status=self._readiness_status,
            reason=self._readiness_reason,
            reason_code="missing_binary" if self._readiness_status != "ready" else None,
        )

    async def invoke(self, task):
        self.invoked = True
        if self._raise_exc is not None:
            raise self._raise_exc
        for event in self._events:
            yield event


def _agent_task_msg(**overrides) -> dict:
    msg = {
        "type": "agent_task",
        "request_id": "req-1",
        "runtime": "stub",
        "workflow": "w",
        "input": {"message": "hi"},
        "config": {},
        "session_id": None,
    }
    msg.update(overrides)
    return msg


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_readiness_cancellation_waits_for_cleanup_before_strict_result(
    monkeypatch, cleanup_fails
):
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()

    class ProbeAdapter(_StubAdapter):
        async def readiness(self, config):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()
                if cleanup_fails:
                    raise RuntimeInvocationError("process still running", "CleanupUnconfirmed")
                cleanup_finished.set()

    adapter = ProbeAdapter()
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)
    ws = _FakeWs()
    pending = asyncio.create_task(
        agent_server._handle_ws_agent_task(ws, _agent_task_msg(require_cancel_ack=True))
    )
    await asyncio.wait_for(started.wait(), 1)
    pending.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    try:
        for _attempt in range(3):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done()
        assert ws.sent == []
    finally:
        release_cleanup.set()
        await asyncio.gather(pending, return_exceptions=True)
    result = ws.sent[-1]["result"]
    assert result["task_id"] == "req-1"
    assert result["cleanup_complete"] is (not cleanup_fails)
    assert result["error_type"] == ("CleanupUnconfirmed" if cleanup_fails else "CancelledError")
    assert cleanup_finished.is_set() is (not cleanup_fails)
    assert adapter.invoked is False


async def test_failed_exit_after_done_never_acknowledges_cleanup(monkeypatch):
    class UnconfirmedAdapter(_StubAdapter):
        async def invoke(self, task):
            try:
                yield {"type": "done", "task_id": task.task_id, "result": {}}
            finally:
                raise RuntimeInvocationError("process still running", "CleanupUnconfirmed")

    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: UnconfirmedAdapter())
    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg(require_cancel_ack=True))
    results = [frame["result"] for frame in ws.sent if frame["type"] == "agent_result"]
    assert len(results) == 1
    assert results[0]["task_id"] == "req-1"
    assert results[0]["type"] == "error"
    assert results[0]["error_type"] == "CleanupUnconfirmed"
    assert results[0]["cleanup_complete"] is False


@pytest.mark.asyncio
async def test_handle_ws_agent_task_happy_path_events_then_result(monkeypatch):
    started = {"type": "started", "task_id": "req-1"}
    text = {"type": "text", "task_id": "req-1", "text": "hello"}
    done = {"type": "done", "task_id": "req-1", "result": {"text": "hello"}}
    adapter = _StubAdapter(events=[started, text, done])
    monkeypatch.setattr(agent_server, "get_runtime", lambda rt: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert len(ws.sent) == 4
    assert ws.sent[0] == {"type": "agent_event", "request_id": "req-1", "event": started}
    assert ws.sent[1] == {"type": "agent_event", "request_id": "req-1", "event": text}
    assert ws.sent[2] == {"type": "agent_event", "request_id": "req-1", "event": done}
    # Final frame is the agent_result carrying the terminal (done) event.
    assert ws.sent[3] == {"type": "agent_result", "request_id": "req-1", "result": done}


@pytest.mark.asyncio
async def test_handle_ws_agent_task_stops_before_cleanup_when_evidence_delivery_fails(
    monkeypatch,
):
    class _PreCleanupAdapter(_StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_started = False

        async def invoke(self, task):
            self.invoked = True
            yield {"type": "started", "task_id": task.task_id}
            yield {
                "type": "evidence",
                "task_id": task.task_id,
                "evidence": {"kind": "doubao.capture.pre_cleanup"},
            }
            self.cleanup_started = True
            yield {"type": "done", "task_id": task.task_id, "result": {}}

    class _EvidenceFailingWs(_FakeWs):
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            if (
                payload.get("type") == "agent_event"
                and payload.get("event", {}).get("type") == "evidence"
            ):
                raise ConnectionError("control-plane connection lost")
            self.sent.append(payload)

    adapter = _PreCleanupAdapter()
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)

    ws = _EvidenceFailingWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert adapter.cleanup_started is False
    assert ws.sent[-1]["type"] == "agent_result"
    assert ws.sent[-1]["result"]["type"] == "error"
    assert ws.sent[-1]["result"]["error_type"] == "AgentEventDeliveryError"


@pytest.mark.asyncio
async def test_handle_ws_agent_task_waits_for_persisted_ack_before_cleanup(monkeypatch):
    class _PreCleanupAdapter(_StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_started = False

        async def invoke(self, task):
            yield {
                "type": "evidence",
                "task_id": task.task_id,
                "evidence": {"kind": "doubao.capture.pre_cleanup"},
            }
            self.cleanup_started = True
            yield {"type": "done", "task_id": task.task_id, "result": {}}

    class _AcknowledgingWs(_FakeWs):
        async def send(self, raw: str) -> None:
            payload = json.loads(raw)
            self.sent.append(payload)
            if payload.get("ack_required") is True:
                asyncio.get_running_loop().call_soon(
                    agent_server._resolve_ws_agent_event_ack,
                    {
                        "type": "agent_event_ack",
                        "request_id": payload["request_id"],
                        "event_id": payload["event_id"],
                        "status": "persisted",
                    },
                )

    adapter = _PreCleanupAdapter()
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)

    ws = _AcknowledgingWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert adapter.cleanup_started is True
    assert ws.sent[0]["type"] == "agent_event"
    assert ws.sent[0]["ack_required"] is True
    assert ws.sent[0]["event_id"]
    assert ws.sent[-1]["type"] == "agent_result"
    assert ws.sent[-1]["result"]["type"] == "done"
    assert agent_server._PENDING_AGENT_EVENT_ACKS == {}


@pytest.mark.asyncio
async def test_handle_ws_agent_task_stops_before_cleanup_when_persisted_ack_times_out(
    monkeypatch,
):
    class _PreCleanupAdapter(_StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_started = False

        async def invoke(self, task):
            yield {
                "type": "evidence",
                "task_id": task.task_id,
                "evidence": {"kind": "doubao.capture.pre_cleanup"},
            }
            self.cleanup_started = True
            yield {"type": "done", "task_id": task.task_id, "result": {}}

    adapter = _PreCleanupAdapter()
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)
    monkeypatch.setattr(agent_server, "_AGENT_EVENT_ACK_TIMEOUT_SECONDS", 0.01)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert adapter.cleanup_started is False
    assert ws.sent[0]["ack_required"] is True
    assert ws.sent[-1]["type"] == "agent_result"
    assert ws.sent[-1]["result"]["error_type"] == "AgentEventAcknowledgementTimeout"
    assert agent_server._PENDING_AGENT_EVENT_ACKS == {}


@pytest.mark.asyncio
async def test_handle_ws_agent_task_unknown_runtime_sends_error_result(monkeypatch):
    def _raise_unknown(rt):
        raise ValueError(f"Unknown runtime type: {rt!r}. Available: []")

    monkeypatch.setattr(agent_server, "get_runtime", _raise_unknown)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg(runtime="nope"))

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "agent_result"
    assert frame["request_id"] == "req-1"
    assert frame["result"]["type"] == "error"
    assert frame["result"]["error_type"] == "ValueError"
    assert "nope" in frame["result"]["message"]


@pytest.mark.asyncio
async def test_handle_ws_agent_task_adapter_raises_sends_error_result(monkeypatch):
    adapter = _StubAdapter(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(agent_server, "get_runtime", lambda rt: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "agent_result"
    assert frame["result"]["type"] == "error"
    assert frame["result"]["error_type"] == "RuntimeError"
    assert "boom" in frame["result"]["message"]


@pytest.mark.asyncio
async def test_handle_ws_agent_task_rejects_invalid_config_before_invoke(monkeypatch):
    adapter = _StubAdapter(
        events=[{"type": "done", "task_id": "req-1", "result": {}}],
        config_errors=["read-only permission modes cannot load explicit extensions"],
    )
    monkeypatch.setattr(agent_server, "get_runtime", lambda rt: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(
        ws,
        _agent_task_msg(
            config={
                "permission_mode": "observe_only",
                "args": ["--extension=unsafe.ts"],
            }
        ),
    )

    assert len(ws.sent) == 1
    assert ws.sent[0]["result"]["type"] == "error"
    assert ws.sent[0]["result"]["error_type"] == "ConfigError"
    assert "explicit extensions" in ws.sent[0]["result"]["message"]


@pytest.mark.asyncio
async def test_handle_ws_agent_task_blocks_failed_readiness_before_invoke(monkeypatch):
    adapter = _StubAdapter(
        events=[{"type": "done", "task_id": "req-1", "result": {}}],
        readiness_status="blocked",
        readiness_reason="codex binary not found",
    )
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg(runtime="codex"))

    assert adapter.invoked is False
    assert ws.sent[0]["result"]["type"] == "error"
    assert ws.sent[0]["result"]["error_type"] == "missing_binary"


@pytest.mark.asyncio
async def test_handle_ws_agent_task_runtime_invocation_error_preserves_error_type(monkeypatch):
    adapter = _StubAdapter(raise_exc=RuntimeInvocationError("bad config", error_type="ConfigError"))
    monkeypatch.setattr(agent_server, "get_runtime", lambda rt: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["result"]["error_type"] == "ConfigError"
    assert "bad config" in frame["result"]["message"]


@pytest.mark.asyncio
async def test_handle_ws_agent_task_no_events_yielded_still_resolves(monkeypatch):
    """A well-behaved adapter always yields a terminal event, but a buggy one
    that yields nothing must not hang the center's pending future forever."""
    adapter = _StubAdapter(events=[])
    monkeypatch.setattr(agent_server, "get_runtime", lambda rt: adapter)

    ws = _FakeWs()
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())

    assert len(ws.sent) == 1
    frame = ws.sent[0]
    assert frame["type"] == "agent_result"
    assert frame["result"]["type"] == "error"
    assert frame["result"]["error_type"] == "RuntimeInvocationError"


@pytest.mark.asyncio
async def test_handle_ws_agent_task_one_task_crash_does_not_raise(monkeypatch):
    """Never raises out of the function — verified directly (the receive loop
    fires this via asyncio.create_task, so an uncaught exception here would
    otherwise surface only as a silently-logged task exception, never crashing
    the loop, but the contract is that _handle_ws_agent_task itself is safe)."""

    def _raise_get_runtime(rt):
        raise KeyError("totally unexpected")

    monkeypatch.setattr(agent_server, "get_runtime", _raise_get_runtime)

    ws = _FakeWs()
    # get_runtime raising something other than ValueError is not caught by the
    # explicit `except ValueError` — it propagates into the outer try/except
    # Exception block only if the call is inside that block. Assert it does
    # NOT raise out of _handle_ws_agent_task.
    await agent_server._handle_ws_agent_task(ws, _agent_task_msg())
    assert len(ws.sent) == 1
    assert ws.sent[0]["result"]["error_type"] == "KeyError"


@pytest.mark.asyncio
async def test_tracked_ws_agent_task_can_be_cancelled(monkeypatch):
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def handle(ws, msg):
        started.set()
        await blocked.wait()

    monkeypatch.setattr(agent_server, "_handle_ws_agent_task", handle)
    agent_server._start_ws_agent_task(_FakeWs(), _agent_task_msg())
    await started.wait()
    task = agent_server._ACTIVE_AGENT_TASKS["req-1"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "req-1" not in agent_server._ACTIVE_AGENT_TASKS


@pytest.mark.parametrize("terminal_type", ["done", "error"])
async def test_strict_terminal_cached_after_cleanup_before_failed_send(monkeypatch, terminal_type):
    cleaned = asyncio.Event()
    terminal = {
        "type": terminal_type,
        "task_id": "req-1",
        "error_type": "CancelledError",
        "message": "private-output",
        "result": {"api_key": "private-credential"},
    }

    class CleanedAdapter(_StubAdapter):
        async def invoke(self, task):
            try:
                yield terminal
            finally:
                cleaned.set()

    class BrokenResultWs(_FakeWs):
        async def send(self, raw):
            frame = json.loads(raw)
            if frame["type"] == "agent_result":
                assert cleaned.is_set()
                assert agent_server._AGENT_TASK_TERMINALS["req-1"][1]["cleanup_complete"] is True
                raise ConnectionError("control plane disconnected")
            assert "req-1" not in agent_server._AGENT_TASK_TERMINALS
            await super().send(raw)

    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: CleanedAdapter())
    await agent_server._handle_ws_agent_task(
        BrokenResultWs(), _agent_task_msg(require_cancel_ack=True)
    )
    ws = _FakeWs()
    await agent_server._handle_ws_agent_task_status(
        ws, {"request_id": "probe-2", "task_id": "req-1"}
    )
    assert ws.sent == [
        {
            "type": "agent_task_status_result",
            "request_id": "probe-2",
            "task_id": "req-1",
            "result": {
                "type": terminal_type,
                "task_id": "req-1",
                "error_type": "CancelledError",
                "cleanup_complete": True,
            },
        }
    ]
    assert "private-" not in repr(agent_server._AGENT_TASK_TERMINALS)


async def test_legacy_terminal_is_not_cached(monkeypatch):
    adapter = _StubAdapter(events=[{"type": "done", "task_id": "req-1", "result": {}}])
    monkeypatch.setattr(agent_server, "get_runtime", lambda _runtime: adapter)
    await agent_server._handle_ws_agent_task(_FakeWs(), _agent_task_msg())
    assert not agent_server._AGENT_TASK_TERMINALS


@pytest.mark.parametrize("cleaned", [None, False, True])
async def test_task_status_never_changes_running_task_and_only_cleaned_proof_wins(cleaned):
    pending = asyncio.create_task(asyncio.Event().wait())
    agent_server._ACTIVE_AGENT_TASKS["original"] = pending
    if cleaned is not None:
        agent_server._cache_agent_task_terminal(
            "original",
            {
                "type": "error",
                "task_id": "original",
                "cleanup_complete": cleaned,
                "error_type": "CancelledError" if cleaned else "CleanupUnconfirmed",
            },
        )
    ws = _FakeWs()
    try:
        await agent_server._handle_ws_agent_task_status(
            ws, {"request_id": "probe", "task_id": "original"}
        )
        result = ws.sent[-1]["result"]
        if cleaned is True:
            assert result["cleanup_complete"] is True
        else:
            assert result == {"status": "running"}
        assert not pending.done()
        assert pending.cancelling() == 0
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_unconfirmed_terminal_is_not_reported_as_cleaned():
    terminal = {
        "type": "error",
        "task_id": "original",
        "cleanup_complete": False,
        "error_type": "CleanupUnconfirmed",
    }
    agent_server._cache_agent_task_terminal("original", terminal)
    ws = _FakeWs()
    await agent_server._handle_ws_agent_task_status(
        ws, {"request_id": "probe", "task_id": "original"}
    )
    assert ws.sent[-1]["result"] == terminal


async def test_cache_is_bounded_and_expires_without_query_refresh(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(agent_server, "monotonic", lambda: now[0])
    for index in range(1025):
        task_id = f"task-{index}"
        agent_server._cache_agent_task_terminal(
            task_id,
            {
                "type": "done",
                "task_id": task_id,
                "cleanup_complete": True,
            },
        )
    assert len(agent_server._AGENT_TASK_TERMINALS) == 1024
    assert "task-0" not in agent_server._AGENT_TASK_TERMINALS
    ws = _FakeWs()
    now[0] = 3699.0
    await agent_server._handle_ws_agent_task_status(
        ws, {"request_id": "before-expiry", "task_id": "task-1024"}
    )
    assert ws.sent[-1]["result"]["cleanup_complete"] is True
    now[0] = 3700.0
    await agent_server._handle_ws_agent_task_status(
        ws, {"request_id": "at-expiry", "task_id": "task-1024"}
    )
    assert ws.sent[-1]["result"] == {"status": "unknown"}
    assert not agent_server._AGENT_TASK_TERMINALS


async def test_status_exact_id_and_empty_edge_state_fail_closed():
    agent_server._cache_agent_task_terminal(
        "original",
        {
            "type": "done",
            "task_id": "original",
            "cleanup_complete": True,
        },
    )
    ws = _FakeWs()
    for task_id in ("other", " original", "Original"):
        await agent_server._handle_ws_agent_task_status(
            ws, {"request_id": "original", "task_id": task_id}
        )
        assert ws.sent[-1]["task_id"] == task_id
        assert ws.sent[-1]["result"] == {"status": "unknown"}
    agent_server._AGENT_TASK_TERMINALS.clear()
    await agent_server._handle_ws_agent_task_status(
        ws, {"request_id": "restart-probe", "task_id": "original"}
    )
    assert ws.sent[-1]["result"] == {"status": "unknown"}


def test_terminal_cache_rejects_malformed_proofs_and_copies_only_metadata():
    for result in (
        {"type": "text", "task_id": "original", "cleanup_complete": True},
        {"type": "done", "task_id": "forged", "cleanup_complete": True},
        {"type": "done", "task_id": "original", "cleanup_complete": 1},
    ):
        agent_server._cache_agent_task_terminal("original", result)
    assert not agent_server._AGENT_TASK_TERMINALS
    source = {
        "type": "error",
        "task_id": "original",
        "cleanup_complete": True,
        "error_type": "API_KEY=private",
        "message": "private",
        "result": {"text": "private"},
    }
    agent_server._cache_agent_task_terminal("original", source)
    source["cleanup_complete"] = False
    assert agent_server._AGENT_TASK_TERMINALS["original"][1] == {
        "type": "error",
        "task_id": "original",
        "cleanup_complete": True,
    }


async def test_reusing_task_id_clears_stale_cleanup_proof(monkeypatch):
    agent_server._cache_agent_task_terminal(
        "req-1",
        {
            "type": "done",
            "task_id": "req-1",
            "cleanup_complete": True,
        },
    )
    monkeypatch.setattr(agent_server, "_handle_ws_agent_task", AsyncMock())
    agent_server._start_ws_agent_task(_FakeWs(), _agent_task_msg())
    assert "req-1" not in agent_server._AGENT_TASK_TERMINALS
    await agent_server._ACTIVE_AGENT_TASKS["req-1"]


async def test_ws_receive_loop_routes_status_without_execution(monkeypatch):
    class StatusWs(_FakeWs):
        delivered = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def recv(self):
            return json.dumps({"type": "registered"})

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.delivered:
                raise _StopTest()
            self.delivered = True
            return json.dumps(
                {
                    "type": "agent_task_status",
                    "request_id": "probe",
                    "task_id": "missing",
                }
            )

    ws = StatusWs()
    monkeypatch.setitem(
        __import__("sys").modules,
        "websockets",
        SimpleNamespace(connect=lambda *args, **kwargs: ws),
    )
    monkeypatch.setattr(agent_server, "_available_agent_runtimes", lambda: [])
    monkeypatch.setattr(agent_server, "available_runtime_capabilities", lambda: {})
    execute = Mock()
    monkeypatch.setattr(agent_server, "_start_ws_agent_task", execute)
    with pytest.raises(_StopTest):
        await agent_server._register_via_ws("http://agent.example:19823")
    execute.assert_not_called()
    assert ws.sent[-1] == {
        "type": "agent_task_status_result",
        "request_id": "probe",
        "task_id": "missing",
        "result": {"status": "unknown"},
    }


@pytest.mark.asyncio
async def test_runtime_invoke_routes_script_host_without_generic_adapter(monkeypatch):
    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="script-host",
        workflow="read-title",
        instructions="read-title",
        input={"selector": "h1"},
        config={"pack": "example"},
    )
    expected = {
        "result": {"title": "Example"},
        "page_before": {"url": "https://example.com"},
        "page_after": {"url": "https://example.com"},
    }

    async def invoke(req, *, cdp_endpoint):
        assert req is request
        assert cdp_endpoint == "http://localhost:9222"
        return expected

    monkeypatch.setattr(agent_runtime_dispatch, "invoke_script_host", invoke)
    monkeypatch.setattr(
        agent_runtime_dispatch,
        "get_runtime",
        lambda runtime: pytest.fail(f"generic adapter selected for {runtime}"),
    )

    assert (
        await agent_runtime_dispatch.invoke_runtime(
            "request-1",
            request,
            cdp_endpoint="http://localhost:9222",
        )
        == expected
    )


@pytest.mark.asyncio
async def test_runtime_invoke_rejects_truncated_nonterminal_stream(monkeypatch):
    class TruncatedAdapter:
        def validate_config(self, config):
            return []

        async def invoke(self, task):
            yield {"type": "started", "task_id": task.task_id}

    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="pi",
        workflow="inspect",
        instructions="inspect",
    )
    monkeypatch.setattr(agent_runtime_dispatch, "get_runtime", lambda runtime: TruncatedAdapter())

    with pytest.raises(HTTPException) as failed:
        await agent_runtime_dispatch.invoke_runtime(
            "request-1",
            request,
            cdp_endpoint="http://localhost:9222",
        )

    assert failed.value.status_code == 502
    assert "terminal event" in str(failed.value.detail)


@pytest.mark.asyncio
async def test_script_host_invocation_uses_persistent_extension_page(monkeypatch):
    request = agent_runtime_dispatch.RuntimeInvokeRequest(
        runtime="script-host",
        workflow="page.metadata",
        instructions="read metadata",
        config={"pack": "page-basics", "action": "page.metadata"},
    )
    target_url = "chrome-extension://stable-id/host.html"
    websocket_url = "ws://chrome:9222/devtools/page/host"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[dict]:
            return [
                {
                    "type": "page",
                    "url": target_url,
                    "webSocketDebuggerUrl": websocket_url,
                }
            ]

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 5}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def get(self, url):
            assert url == "http://chrome:9222/json/list"
            return Response()

    expressions: list[str] = []

    async def evaluate(url, expression):
        assert url == websocket_url
        expressions.append(expression)
        if expression == "chrome.runtime.getManifest().name":
            return "OpenCLI Script Host"
        return {
            "result": {"title": "Example Domain"},
            "page_before": {"url": "https://example.com"},
            "page_after": {"url": "https://example.com"},
        }

    monkeypatch.setattr(agent_runtime_dispatch.httpx, "AsyncClient", Client)
    monkeypatch.setattr(agent_runtime_dispatch, "_evaluate_cdp_target", evaluate)

    result = await agent_runtime_dispatch.invoke_script_host(
        request,
        cdp_endpoint="http://chrome:9222",
    )

    assert result["result"]["title"] == "Example Domain"
    assert len(expressions) == 2
    assert "opencliScriptHost.invoke" in expressions[1]


@pytest.mark.parametrize(
    ("runtime", "runner_env", "roots_env", "runner", "required", "forbidden"),
    [
        (
            "codex",
            "AGENT_CODEX_ISOLATED_RUNNER",
            "AGENT_CODEX_ALLOWED_ROOTS",
            "/runtime/codex",
            {"--terminal", "--sandbox", "read-only", "--ask-for-approval", "never"},
            {"--skip-git-repo-check", "--ignore-user-config"},
        ),
        (
            "omp",
            "AGENT_OMP_ISOLATED_RUNNER",
            "AGENT_OMP_ALLOWED_ROOTS",
            "/runtime/omp",
            {
                "--terminal",
                "--no-session",
                "--no-tools",
                "--no-lsp",
                "--no-extensions",
                "--no-skills",
                "--no-rules",
            },
            {"--no-prompt-templates", "--no-context-files", "--no-approve", "--tui-mode"},
        ),
    ],
)
def test_terminal_runner_command_uses_supported_cli_flags_and_prompt(
    monkeypatch,
    runtime,
    runner_env,
    roots_env,
    runner,
    required,
    forbidden,
):
    prompt = "只回复 TUI-OK，不要调用工具。"
    monkeypatch.setattr(agent_server.os, "name", "posix")
    monkeypatch.setattr(agent_server, "Path", _TerminalPath)
    monkeypatch.setenv(runner_env, runner)
    monkeypatch.setenv(roots_env, json.dumps(["/workspace"]))

    command = agent_server._terminal_runner_command(runtime, "/workspace/job", prompt)

    assert command[0] == runner
    assert command[-1] == prompt
    assert required.issubset(command)
    assert forbidden.isdisjoint(command)
    if runtime == "codex":
        assert command[command.index("--cd") + 1] == "/workspace/job"


def test_terminal_runner_command_rejects_relative_allowed_root(monkeypatch):
    monkeypatch.setattr(agent_server.os, "name", "posix")
    monkeypatch.setattr(agent_server, "Path", _TerminalPath)
    monkeypatch.setenv("AGENT_CODEX_ISOLATED_RUNNER", "/runtime/codex")
    monkeypatch.setenv("AGENT_CODEX_ALLOWED_ROOTS", json.dumps(["workspace"]))

    with pytest.raises(RuntimeError, match="roots are invalid"):
        agent_server._terminal_runner_command("codex", "/workspace/job", "prompt")


@pytest.mark.asyncio
async def test_terminal_detach_releases_controller_attachment(monkeypatch):
    session_id = "11111111-1111-4111-8111-111111111111"
    attachment_id = "22222222-2222-4222-8222-222222222222"
    session = agent_server._NativeTerminalSession(
        session_id=session_id,
        runtime="codex",
        process=SimpleNamespace(),
        master_fd=-1,
        controller_id="33333333-3333-4333-8333-333333333333",
        controller_attachment_id=attachment_id,
        attachments={attachment_id: "33333333-3333-4333-8333-333333333333"},
    )
    monkeypatch.setattr(agent_server, "_NATIVE_TERMINALS", {session_id: session})

    await agent_server._handle_terminal_detach(
        {"session_id": session_id, "attachment_id": attachment_id}
    )

    assert session.attachments == {}
    assert session.controller_attachment_id is None


@pytest.mark.asyncio
async def test_terminal_process_group_cleanup_waits_for_confirmed_exit(monkeypatch):
    session = agent_server._NativeTerminalSession(
        session_id="11111111-1111-4111-8111-111111111111",
        runtime="codex",
        process=SimpleNamespace(pid=1234),
        master_fd=-1,
    )
    calls = []

    def killpg(pid, sig):
        calls.append((pid, sig))
        if sig == 0:
            raise ProcessLookupError

    monkeypatch.setattr(agent_server.os, "name", "posix")
    monkeypatch.setattr(agent_server.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(agent_server.signal, "SIGKILL", 9, raising=False)

    assert await agent_server._cleanup_terminal_process_group(session) is True
    assert calls == [(1234, agent_server.signal.SIGKILL), (1234, 0)]


@pytest.mark.asyncio
async def test_terminal_process_group_cleanup_fails_closed_when_group_survives(monkeypatch):
    session = agent_server._NativeTerminalSession(
        session_id="11111111-1111-4111-8111-111111111111",
        runtime="codex",
        process=SimpleNamespace(pid=1234),
        master_fd=-1,
    )
    calls = []

    def killpg(pid, sig):
        calls.append((pid, sig))

    monkeypatch.setattr(agent_server.os, "name", "posix")
    monkeypatch.setattr(agent_server.os, "killpg", killpg, raising=False)
    monkeypatch.setattr(agent_server.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(agent_server, "_TERMINAL_CLEANUP_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(agent_server, "_TERMINAL_CLEANUP_POLL_INTERVAL_SECONDS", 0)

    assert await agent_server._cleanup_terminal_process_group(session) is False
    assert calls == [(1234, agent_server.signal.SIGKILL), (1234, 0), (1234, 0)]
