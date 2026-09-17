import asyncio
import json
import os
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException

from backend import agent_server
from backend.agent_runtimes.base import AgentTask
from backend.agent_runtimes.omp_adapter import OmpRuntimeAdapter
from backend.agent_runtimes.registry import get_runtime


@pytest.fixture
def paths(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    runner = tmp_path / "isolated-omp.exe"
    runner.write_text("test runner")
    runner.chmod(0o755)
    monkeypatch.setenv("AGENT_OMP_ISOLATED_RUNNER", str(runner))
    monkeypatch.setenv("AGENT_OMP_ALLOWED_ROOTS", json.dumps([str(root)]))
    return SimpleNamespace(root=root, runner=runner)


def make_task(paths, **config):
    return AgentTask(
        task_id="request-1",
        workflow="operator_chat",
        instructions="Return the governed XML response.",
        input={"message": "Earlier messages and current user input."},
        config={"cwd": str(paths.root), **config},
    )


def assistant(text="answer", **extra):
    return {"role": "assistant", "content": [{"type": "text", "text": text}], **extra}


class RpcProcess:
    def __init__(self, frames=None, *, exit_code=0, abort_reply=True, close_exits=True):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = self
        self.returncode = None
        self.exit_code = exit_code
        self.abort_reply = abort_reply
        self.close_exits = close_exits
        self.frames = (
            frames
            if frames is not None
            else [
                {
                    "type": "message_update",
                    "assistantMessageEvent": {"type": "text_delta", "delta": "answer"},
                },
                {"type": "message_end", "message": assistant()},
                {"type": "agent_end", "messages": [assistant()]},
            ]
        )
        self.requests = []
        self.prompted = asyncio.Event()
        self.aborted = asyncio.Event()
        self.exited = asyncio.Event()
        self.feed({"type": "ready", "protocolVersion": 1})

    def feed(self, frame):
        self.stdout.feed_data((json.dumps(frame) + "\n").encode())

    def write(self, data):
        frame = json.loads(data)
        self.requests.append(frame)
        if frame["type"] == "prompt":
            self.prompted.set()
            self.feed({"type": "response", "id": frame["id"], "command": "prompt", "success": True})
            for event in self.frames:
                if event is None:
                    self.finish(self.exit_code)
                elif isinstance(event, bytes):
                    self.stdout.feed_data(event)
                else:
                    self.feed(event)
        elif frame["type"] == "abort":
            self.aborted.set()
            if self.abort_reply:
                self.feed(
                    {"type": "response", "id": frame["id"], "command": "abort", "success": True}
                )

    async def drain(self):
        pass

    def close(self):
        if self.close_exits:
            self.finish(self.exit_code)

    def finish(self, code):
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.exited.set()

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


@pytest.fixture
def adapter(monkeypatch):
    instance = OmpRuntimeAdapter()
    monkeypatch.setattr(instance, "_probe_runner", Mock(return_value="18.1.15"))
    monkeypatch.setattr("backend.agent_runtimes.omp_adapter._SHUTDOWN_SECONDS", 0.02)
    return instance


def mock_spawn(monkeypatch, process):
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("backend.agent_runtimes.omp_adapter.asyncio.create_subprocess_exec", spawn)
    return spawn


async def test_rpc_inference_stream_is_ephemeral_and_does_not_inherit_secrets(
    paths, adapter, monkeypatch
):
    process = RpcProcess()
    process.stderr.feed_data(b"private-diagnostic API_KEY=secret")
    spawn = mock_spawn(monkeypatch, process)
    for name in (
        "AGENT_API_TOKEN",
        "OPENAI_API_KEY",
        "PI_CONFIG_FILES",
        "OMP_PROFILE",
        "CODEX_HOME",
    ):
        monkeypatch.setenv(name, "private-value")
    task = make_task(paths)
    events = [event async for event in adapter.invoke(task)]
    assert [event["type"] for event in events] == ["started", "text", "done"]
    assert events[-1]["result"] == {"runtime": "omp", "text": "answer", "exit_code": 0}
    assert process.exited.is_set()
    argv = spawn.call_args.args
    assert argv[0] == str(paths.runner)
    assert {
        "--operator-chat",
        "--no-tools",
        "--no-extensions",
        "--no-rules",
        "--no-skills",
        "--no-session",
    } <= set(argv)
    assert "--auto-approve" not in argv
    assert not any(argument.startswith(("--model", "--provider")) for argument in argv)
    assert "Earlier messages" in process.requests[0]["message"]
    assert all("private-value" != value for value in spawn.call_args.kwargs["env"].values())
    assert "private-diagnostic" not in json.dumps(events)
    assert spawn.call_args.kwargs["cwd"] == str(paths.root)
    assert "operator_chat" in adapter.capabilities.names()
    assert adapter.capabilities.resume_by_id is False


@pytest.mark.parametrize("field", ["provider", "model"])
async def test_unsupported_model_selection_fails_before_probe_or_spawn(
    paths, adapter, monkeypatch, field
):
    spawn = mock_spawn(monkeypatch, RpcProcess())
    task = make_task(paths)
    setattr(task, field, "unsupported-override")
    events = [event async for event in adapter.invoke(task)]
    assert events[-1]["error_type"] == "ConfigError"
    assert "model_selection" not in adapter.capabilities.names()
    adapter._probe_runner.assert_not_called()
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"binary": "omp"},
        {"env": {}},
        {"args": []},
        {"cwd": "."},
        {"permission_mode": "full_auto"},
        {"timeout_seconds": float("nan")},
    ],
)
async def test_forged_launch_configuration_never_spawns(paths, adapter, monkeypatch, override):
    spawn = mock_spawn(monkeypatch, RpcProcess())
    events = [event async for event in adapter.invoke(make_task(paths, **override))]
    assert events[-1]["error_type"] == "ConfigError"
    spawn.assert_not_called()


@pytest.mark.parametrize("field,value", [("workflow", "exec"), ("session_id", "native-session")])
async def test_only_ephemeral_operator_chat_is_supported(paths, adapter, monkeypatch, field, value):
    spawn = mock_spawn(monkeypatch, RpcProcess())
    task = make_task(paths)
    setattr(task, field, value)
    assert [event async for event in adapter.invoke(task)][-1]["error_type"] == "ConfigError"
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "case", ["missing_runner", "missing_roots", "outside_root", "relative_root", "runner_in_root"]
)
async def test_isolation_boundary_rejects_untrusted_paths(paths, adapter, monkeypatch, case):
    task = make_task(paths)
    if case == "missing_runner":
        monkeypatch.delenv("AGENT_OMP_ISOLATED_RUNNER")
    elif case == "missing_roots":
        monkeypatch.delenv("AGENT_OMP_ALLOWED_ROOTS")
    elif case == "outside_root":
        task.config["cwd"] = str(paths.root.parent)
    elif case == "relative_root":
        monkeypatch.setenv("AGENT_OMP_ALLOWED_ROOTS", '["."]')
    else:
        monkeypatch.setenv("AGENT_OMP_ALLOWED_ROOTS", json.dumps([str(paths.root.parent)]))
    spawn = mock_spawn(monkeypatch, RpcProcess())
    assert [event async for event in adapter.invoke(task)][-1]["error_type"] == "ConfigError"
    spawn.assert_not_called()


@pytest.mark.parametrize(
    "frames,error_type",
    [
        (
            [{"type": "response", "id": "request-1", "success": False, "error": "private-key"}],
            "ModelError",
        ),
        ([{"type": "tool_execution_start", "toolName": "bash"}], "NativeToolDenied"),
        ([{"type": "extension_ui_request", "method": "confirm"}], "NativeToolDenied"),
        (
            [
                {
                    "type": "message_end",
                    "message": assistant(stopReason="error", errorMessage="private-key"),
                }
            ],
            "ModelError",
        ),
        (
            [{"type": "message_end", "message": assistant(content=[{"type": "toolCall"}])}],
            "NativeToolDenied",
        ),
        ([None], "UnexpectedEOF"),
        ([b"not-json\n"], "ProtocolError"),
        ([{"type": "agent_end", "messages": []}], "EmptyResponse"),
    ],
)
async def test_native_failures_never_become_success_or_confirmation_bypass(
    paths, adapter, monkeypatch, frames, error_type
):
    process = RpcProcess(frames)
    mock_spawn(monkeypatch, process)
    events = [event async for event in adapter.invoke(make_task(paths))]
    assert events[-1]["error_type"] == error_type
    assert sum(event["type"] in {"done", "error"} for event in events) == 1
    assert "private-key" not in json.dumps(events)
    assert process.exited.is_set()


async def test_rpc_ack_is_not_completion_and_timeout_aborts(paths, adapter, monkeypatch):
    process = RpcProcess([])
    mock_spawn(monkeypatch, process)
    events = [event async for event in adapter.invoke(make_task(paths, timeout_seconds=0.01))]
    assert events[-1]["error_type"] == "TimeoutError"
    assert process.aborted.is_set()
    assert process.exited.is_set()


async def test_task_cancellation_aborts_and_waits_for_exit(paths, adapter, monkeypatch):
    process = RpcProcess([])
    mock_spawn(monkeypatch, process)

    async def consume():
        return [event async for event in adapter.invoke(make_task(paths))]

    pending = asyncio.create_task(consume())
    await process.prompted.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.aborted.is_set()
    assert process.exited.is_set()


async def test_closing_generator_before_prompt_stops_runner(paths, adapter, monkeypatch):
    process = RpcProcess([])
    mock_spawn(monkeypatch, process)
    stream = adapter.invoke(make_task(paths))
    assert (await anext(stream))["type"] == "started"
    await stream.aclose()
    assert process.exited.is_set()
    assert not process.prompted.is_set()


async def test_cancel_during_spawn_waits_for_owned_process_cleanup(paths, adapter, monkeypatch):
    process = RpcProcess([])
    spawning = asyncio.Event()
    release = asyncio.Event()

    async def spawn(*args, **kwargs):
        spawning.set()
        await release.wait()
        return process

    monkeypatch.setattr("backend.agent_runtimes.omp_adapter.asyncio.create_subprocess_exec", spawn)

    async def consume():
        return [event async for event in adapter.invoke(make_task(paths))]

    pending = asyncio.create_task(consume())
    await spawning.wait()
    pending.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert process.exited.is_set()


async def test_failed_exit_is_not_success(paths, adapter, monkeypatch):
    process = RpcProcess(exit_code=1)
    mock_spawn(monkeypatch, process)
    assert [event async for event in adapter.invoke(make_task(paths))][-1][
        "error_type"
    ] == "ProcessExitError"


def safety_proof(**overrides):
    return {
        "runtime": "omp",
        "version": "18.1.15",
        "protocol": "rpc-v1",
        "operator_chat": True,
        "native_tools": False,
        "mcp": False,
        "extensions": False,
        "user_context": False,
        **overrides,
    }


@pytest.mark.parametrize(
    "proof,allowed",
    [
        (safety_proof(), True),
        (safety_proof(mcp=True), False),
        (safety_proof(native_tools=True), False),
        (safety_proof(extensions=True), False),
        (safety_proof(user_context=True), False),
        (safety_proof(operator_chat=False), False),
        (safety_proof(version="17.3.4"), False),
        ({"version": "18.1.15"}, False),
    ],
)
def test_advertisement_requires_runner_safety_attestation(paths, monkeypatch, proof, allowed):
    process = SimpleNamespace(
        returncode=0, communicate=Mock(return_value=(json.dumps(proof).encode(), None))
    )
    spawn = Mock(return_value=process)
    monkeypatch.setattr("backend.agent_runtimes.omp_adapter.subprocess.Popen", spawn)
    cleanup = Mock()
    monkeypatch.setattr(OmpRuntimeAdapter._process_guard, "_stop_process_sync", cleanup)
    assert OmpRuntimeAdapter.is_available() is allowed
    assert spawn.call_args.args[0] == [str(paths.runner), "--operator-chat-probe"]
    assert spawn.call_args.kwargs["cwd"] == str(paths.root)
    process.communicate.assert_called_once_with(timeout=20)
    cleanup.assert_called_once_with(process)


@pytest.mark.parametrize("cold_start_seconds", [10, 21])
def test_probe_allows_cold_start_but_enforces_deadline(paths, monkeypatch, cold_start_seconds):
    process = SimpleNamespace(returncode=None)

    def communicate(*, timeout):
        if cold_start_seconds > timeout:
            raise subprocess.TimeoutExpired("isolated-omp-probe", timeout)
        process.returncode = 0
        return json.dumps(safety_proof()).encode(), None

    process.communicate = Mock(side_effect=communicate)
    monkeypatch.setattr(
        "backend.agent_runtimes.omp_adapter.subprocess.Popen", Mock(return_value=process)
    )
    cleanup = Mock()
    monkeypatch.setattr(OmpRuntimeAdapter._process_guard, "_stop_process_sync", cleanup)
    assert OmpRuntimeAdapter.is_available() is (cold_start_seconds <= 20)
    process.communicate.assert_called_once_with(timeout=20)
    cleanup.assert_called_once_with(process)


async def test_invoke_rechecks_attestation(paths, adapter, monkeypatch):
    adapter._probe_runner.return_value = None
    spawn = mock_spawn(monkeypatch, RpcProcess())
    assert [event async for event in adapter.invoke(make_task(paths))][-1][
        "error_type"
    ] == "UnsafeRunner"
    spawn.assert_not_called()


async def test_agent_cancel_result_is_sent_after_runtime_cleanup(paths, adapter, monkeypatch):
    process = RpcProcess([], abort_reply=False)
    mock_spawn(monkeypatch, process)
    monkeypatch.setattr(agent_server, "get_runtime", lambda runtime: adapter)
    sent = []

    async def send(payload):
        frame = json.loads(payload)
        if frame["type"] == "agent_result":
            assert process.exited.is_set()
        sent.append(frame)

    websocket = SimpleNamespace(send=send)
    task = make_task(paths)
    pending = asyncio.create_task(
        agent_server._handle_ws_agent_task(
            websocket,
            {
                "request_id": task.task_id,
                "runtime": "omp",
                "workflow": task.workflow,
                "config": task.config,
                "input": task.input,
                "require_cancel_ack": True,
            },
        )
    )
    await process.prompted.wait()
    pending.cancel()
    await process.aborted.wait()
    assert not any(frame["type"] == "agent_result" for frame in sent)
    pending.cancel()
    process.feed({"type": "response", "id": "stop", "success": True})
    await pending
    terminal = [frame["result"] for frame in sent if frame["type"] == "agent_result"]
    assert len(terminal) == 1
    assert terminal[0]["error_type"] == "CancelledError"
    assert terminal[0]["task_id"] == task.task_id
    assert terminal[0]["cleanup_complete"] is True


async def test_omp_is_registered_but_direct_http_dispatch_is_forbidden(monkeypatch):
    assert isinstance(get_runtime("omp"), OmpRuntimeAdapter)
    monkeypatch.setattr(agent_server, "_require_collect_auth", lambda value: None)
    request = agent_server.RuntimeInvokeRequest(
        runtime="omp", workflow="operator_chat", instructions="test"
    )
    with pytest.raises(HTTPException) as raised:
        await agent_server.invoke_runtime_http(request)
    assert raised.value.status_code == 403


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree cleanup")
async def test_windows_cleanup_uses_taskkill_full_tree(adapter, monkeypatch):
    process = SimpleNamespace(pid=987654, returncode=0)
    terminate = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr("backend.agent_runtimes.codex_adapter.subprocess.run", terminate)
    await adapter._process_guard._stop_process(process)
    assert terminate.call_args.args[0] == ["taskkill", "/PID", "987654", "/T", "/F"]


@pytest.mark.skipif(os.name != "nt", reason="Windows shell launcher rejection")
@pytest.mark.parametrize("suffix", [".cmd", ".bat", ".ps1"])
def test_windows_shell_launchers_are_not_accepted(paths, monkeypatch, suffix):
    runner = paths.runner.with_suffix(suffix)
    runner.write_text("not executable")
    monkeypatch.setenv("AGENT_OMP_ISOLATED_RUNNER", str(runner))
    assert OmpRuntimeAdapter.is_available() is False


@pytest.mark.parametrize("case", ["done", "model_error", "unknown", "readiness", "spawn", "config"])
async def test_all_strict_terminals_include_matching_cleanup_proof(
    paths, adapter, monkeypatch, case
):
    process = (
        RpcProcess()
        if case != "model_error"
        else RpcProcess(
            [
                {"type": "response", "id": "request-1", "success": False},
            ]
        )
    )
    spawn = mock_spawn(monkeypatch, process)
    monkeypatch.setattr(agent_server, "get_runtime", lambda runtime: adapter)
    task = make_task(paths)
    if case == "unknown":
        monkeypatch.setattr(agent_server, "get_runtime", Mock(side_effect=ValueError("unknown")))
    elif case == "readiness":
        adapter._probe_runner.return_value = None
    elif case == "spawn":
        spawn.side_effect = OSError("private diagnostic")
    elif case == "config":
        task.config["binary"] = "untrusted"
    frames = []

    async def send(payload):
        frame = json.loads(payload)
        if frame["type"] == "agent_result" and case in {"done", "model_error"}:
            assert process.exited.is_set()
        frames.append(frame)

    await agent_server._handle_ws_agent_task(
        SimpleNamespace(send=send),
        {
            "request_id": task.task_id,
            "runtime": "omp",
            "workflow": task.workflow,
            "config": task.config,
            "input": task.input,
            "require_cancel_ack": True,
        },
    )
    terminal = [frame["result"] for frame in frames if frame["type"] == "agent_result"]
    assert len(terminal) == 1
    assert terminal[0]["type"] == ("done" if case == "done" else "error")
    assert terminal[0]["cleanup_complete"] is True
    assert terminal[0]["task_id"] == task.task_id


async def test_failed_cleanup_never_confirms_remote_stop(paths, adapter, monkeypatch):
    process = RpcProcess()
    mock_spawn(monkeypatch, process)
    monkeypatch.setattr(adapter, "_shutdown", AsyncMock(side_effect=RuntimeError("cleanup failed")))
    monkeypatch.setattr(agent_server, "get_runtime", lambda runtime: adapter)
    frames = []

    async def send(payload):
        frames.append(json.loads(payload))

    task = make_task(paths)
    await agent_server._handle_ws_agent_task(
        SimpleNamespace(send=send),
        {
            "request_id": task.task_id,
            "runtime": "omp",
            "workflow": task.workflow,
            "config": task.config,
            "input": task.input,
            "require_cancel_ack": True,
        },
    )
    terminal = next(frame["result"] for frame in frames if frame["type"] == "agent_result")
    assert terminal["cleanup_complete"] is False
    process.finish(-9)


async def test_unexpected_exception_propagates_after_process_cleanup(paths, adapter, monkeypatch):
    process = RpcProcess([])
    mock_spawn(monkeypatch, process)
    monkeypatch.setattr(
        adapter,
        "_read_frame",
        AsyncMock(
            side_effect=[
                RuntimeError("unexpected read failure"),
                {"type": "response", "id": "stop", "success": True},
            ]
        ),
    )
    with pytest.raises(RuntimeError, match="unexpected read failure"):
        _events = [event async for event in adapter.invoke(make_task(paths))]
    assert process.exited.is_set()
