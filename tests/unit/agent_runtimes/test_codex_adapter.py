"""Focused contract tests for the registered-Agent Codex runtime adapter."""

import asyncio
import json
import os
import signal
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent_runtimes import codex_adapter
from backend.agent_runtimes.base import AgentTask, RuntimeInvocationError
from backend.agent_runtimes.codex_adapter import CodexRuntimeAdapter
from backend.agent_runtimes.registry import get_runtime, list_runtime_types


@pytest.fixture(autouse=True)
def _permit_test_worktree(monkeypatch, tmp_path):
    runner = tmp_path.parent / f"isolated-codex-runner-{tmp_path.name}"
    runner.write_text("test runner", encoding="utf-8")
    runner.chmod(0o700)
    monkeypatch.setenv("AGENT_CODEX_ISOLATED_RUNNER", str(runner.resolve()))
    monkeypatch.setenv("AGENT_CODEX_ALLOWED_ROOTS", json.dumps([str(tmp_path)]))
    yield runner.resolve()
    runner.unlink(missing_ok=True)


class _CompletedProcess:
    def __init__(
        self, lines: list[dict] | None = None, *, returncode: int = 0, stderr: bytes = b""
    ):
        import json

        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        for line in lines or []:
            self.stdout.feed_data((json.dumps(line) + "\n").encode())
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        if stderr:
            self.stderr.feed_data(stderr)
        self.stderr.feed_eof()

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class _HangingProcess:
    def __init__(self):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()

    async def wait(self) -> int:
        return -15 if self.returncode is None else self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()


def _task(tmp_path: Path, **config) -> AgentTask:
    return AgentTask(
        task_id="task-1",
        workflow="exec",
        instructions="Inspect the repository.",
        input={"message": "Report the result."},
        config={"cwd": str(tmp_path), **config},
    )


def test_codex_json_events_become_closed_runtime_events():
    adapter = CodexRuntimeAdapter()

    call = adapter._translate_event(
        "task-1",
        {
            "type": "item.started",
            "item": {"id": "cmd-1", "type": "command_execution", "command": "python verify.py"},
        },
    )
    result = adapter._translate_event(
        "task-1",
        {
            "type": "item.completed",
            "item": {
                "id": "cmd-1",
                "type": "command_execution",
                "command": "python verify.py",
                "aggregated_output": "",
                "exit_code": 0,
            },
        },
    )
    final_text = adapter._translate_event(
        "task-1",
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"tests": []}'}},
    )

    assert call == {
        "type": "tool_call",
        "task_id": "task-1",
        "name": "python verify.py",
        "args": {"command": "python verify.py"},
        "call_id": "cmd-1",
    }
    assert result == {
        "type": "tool_result",
        "task_id": "task-1",
        "name": "python verify.py",
        "result": "",
        "call_id": "cmd-1",
        "is_error": False,
    }
    assert final_text == {"type": "text", "task_id": "task-1", "text": '{"tests": []}'}


def test_adapter_requires_controller_owned_worktree_and_rejects_launch_overrides(tmp_path):
    adapter = CodexRuntimeAdapter()

    assert adapter.validate_config({"timeout_seconds": 10}) == [
        "'cwd' must be a non-empty controller-owned worktree path"
    ]
    errors = adapter.validate_config(
        {
            "cwd": str(tmp_path),
            "binary": "python",
            "args": ["payload.py"],
            "project_root": str(tmp_path),
            "sandbox_mode": "danger-full-access",
        }
    )
    assert errors == [
        "unsupported controller runtime config: args, binary, project_root, sandbox_mode"
    ]


@pytest.mark.parametrize("permission_mode", ["observe_only", "suggest_changes", None])
def test_advisory_profiles_always_use_ephemeral_read_only_sandbox(tmp_path, permission_mode):
    adapter = CodexRuntimeAdapter()
    config = {"cwd": str(tmp_path)}
    if permission_mode is not None:
        config["permission_mode"] = permission_mode
    assert adapter.validate_config(config) == []

    argv = adapter._compose_argv("codex", tmp_path.resolve(), "Inspect")
    assert argv == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--ephemeral",
        "--ignore-user-config",
        "-c",
        'shell_environment_policy.inherit="core"',
        "-c",
        (
            'shell_environment_policy.include_only=["PATH","HOME","USERPROFILE",'
            '"TEMP","TMP","SYSTEMROOT","WINDIR","COMSPEC","PATHEXT","LANG","LC_*"]'
        ),
        "-c",
        "shell_environment_policy.ignore_default_excludes=false",
        "--sandbox",
        "read-only",
        "--cd",
        str(tmp_path.resolve()),
        "Inspect",
    ]
    assert "--approve-for-me" not in argv
    assert "danger-full-access" not in argv


@pytest.mark.parametrize("permission_mode", ["low_risk_automatic", "full_auto", "read_only"])
def test_unpublished_permission_modes_are_rejected(tmp_path, permission_mode):
    errors = CodexRuntimeAdapter().validate_config(
        {"cwd": str(tmp_path), "permission_mode": permission_mode}
    )
    assert errors == ["'permission_mode' must be one of observe_only, suggest_changes"]


async def test_readiness_reports_fixed_binary_version_and_confined_path(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))

    readiness = await adapter.readiness({"cwd": str(tmp_path)})

    assert readiness.status == "ready"
    assert readiness.runtime == "codex"
    assert readiness.capability_id == "runtime.codex"
    assert readiness.binary_present is True
    assert readiness.version == "codex-cli 1.2.3"
    assert readiness.permitted_project_root == str(tmp_path.resolve())
    assert readiness.working_directory == str(tmp_path.resolve())
    assert not hasattr(readiness, "api_key")


async def test_readiness_blocks_when_isolated_runner_probe_fails(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value=None))

    readiness = await adapter.readiness({"cwd": str(tmp_path)})

    assert readiness.status == "blocked"
    assert readiness.reason_code == "isolated_runner_probe_failed"
    assert readiness.binary_present is True


def test_non_executable_isolated_runner_is_unavailable(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX execute permission bits")
    runner = tmp_path / "non-executable-runner"
    runner.write_text("#!/bin/sh\n", encoding="utf-8")
    runner.chmod(0o600)
    monkeypatch.setenv("AGENT_CODEX_ISOLATED_RUNNER", str(runner))

    assert CodexRuntimeAdapter.is_available() is False


async def test_readiness_blocks_missing_or_invalid_worktree(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()

    missing_config = await adapter.readiness({})
    invalid_path = await adapter.readiness({"cwd": str(tmp_path / "missing")})

    assert missing_config.status == "blocked"
    assert missing_config.reason_code == "invalid_config"
    assert missing_config.binary_present is True
    assert invalid_path.status == "blocked"
    assert invalid_path.reason_code == "invalid_path"


async def test_readiness_fails_closed_without_server_owned_roots(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    monkeypatch.delenv("AGENT_CODEX_ALLOWED_ROOTS")

    readiness = await adapter.readiness({"cwd": str(tmp_path)})

    assert readiness.status == "blocked"
    assert readiness.reason_code == "invalid_path"
    assert "AGENT_CODEX_ALLOWED_ROOTS" in (readiness.reason or "")


async def test_readiness_rejects_sibling_of_permitted_root(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    sibling = tmp_path / "sibling"
    allowed.mkdir()
    sibling.mkdir()
    monkeypatch.setenv("AGENT_CODEX_ALLOWED_ROOTS", json.dumps([str(allowed)]))

    readiness = await CodexRuntimeAdapter().readiness({"cwd": str(sibling)})

    assert readiness.status == "blocked"
    assert "outside" in (readiness.reason or "")


async def test_readiness_rejects_symlink_escape(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    link = allowed / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require host support")
    monkeypatch.setenv("AGENT_CODEX_ALLOWED_ROOTS", json.dumps([str(allowed)]))

    readiness = await CodexRuntimeAdapter().readiness({"cwd": str(link)})

    assert readiness.status == "blocked"
    assert "outside" in (readiness.reason or "")


def test_availability_requires_isolated_runner_and_server_owned_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        CodexRuntimeAdapter,
        "_probe_runner",
        classmethod(lambda cls, binary: True),
    )
    assert CodexRuntimeAdapter.is_available() is True

    monkeypatch.delenv("AGENT_CODEX_ALLOWED_ROOTS")
    assert CodexRuntimeAdapter.is_available() is False

    monkeypatch.setenv("AGENT_CODEX_ALLOWED_ROOTS", json.dumps([str(tmp_path)]))
    monkeypatch.delenv("AGENT_CODEX_ISOLATED_RUNNER")
    assert CodexRuntimeAdapter.is_available() is False


def test_availability_rejects_runner_that_fails_compatibility_probe(monkeypatch):
    monkeypatch.setattr(
        CodexRuntimeAdapter,
        "_probe_runner",
        classmethod(lambda cls, binary: False),
    )

    assert CodexRuntimeAdapter.is_available() is False


@pytest.mark.parametrize(
    ("stdout", "returncode", "expected"),
    [
        (b"codex-cli 1.2.3\n", 0, True),
        (b"Python 3.13.0\n", 0, False),
        (b"codex-cli 1.2.3\n", 1, False),
    ],
)
def test_registration_probe_requires_successful_codex_version(
    monkeypatch, stdout, returncode, expected
):
    cleaned: list[object] = []

    class ProbeProcess:
        pid = 12345

        def __init__(self):
            self.returncode = returncode

        def communicate(self, *, timeout):
            assert timeout > 0
            return stdout, b""

    process = ProbeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        CodexRuntimeAdapter,
        "_stop_process_sync",
        staticmethod(lambda candidate: cleaned.append(candidate)),
    )

    assert CodexRuntimeAdapter._probe_runner("/opt/codex-isolated-runner") is expected
    assert cleaned == [process]


def test_registration_probe_timeout_fails_closed_and_cleans_tree(monkeypatch):
    cleaned: list[object] = []

    class HangingProbeProcess:
        pid = 12345
        returncode = None

        def communicate(self, *, timeout):
            raise subprocess.TimeoutExpired("codex-isolated-runner", timeout)

    process = HangingProbeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(
        CodexRuntimeAdapter,
        "_stop_process_sync",
        staticmethod(lambda candidate: cleaned.append(candidate)),
    )

    assert CodexRuntimeAdapter._probe_runner("/opt/codex-isolated-runner") is False
    assert cleaned == [process]


async def test_missing_isolated_runner_is_terminal_error(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CODEX_ISOLATED_RUNNER")

    events = [event async for event in CodexRuntimeAdapter().invoke(_task(tmp_path))]

    assert events == [
        {
            "type": "error",
            "task_id": "task-1",
            "message": (
                "AGENT_CODEX_ISOLATED_RUNNER is required; direct Codex execution is disabled"
            ),
            "error_type": "FileNotFoundError",
        }
    ]


async def test_readiness_fails_closed_without_isolated_runner(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_CODEX_ISOLATED_RUNNER")

    readiness = await CodexRuntimeAdapter().readiness({"cwd": str(tmp_path)})

    assert readiness.status == "blocked"
    assert readiness.reason_code == "missing_isolated_runner"
    assert readiness.binary_present is False


async def test_version_probe_uses_only_fixed_binary(monkeypatch):
    captured: list[tuple] = []

    class VersionProcess:
        returncode = 0

        async def communicate(self):
            return b"codex-cli 1.2.3\n", b""

    async def spawn(*argv, **kwargs):
        captured.append(argv)
        return VersionProcess()

    monkeypatch.setattr(
        "backend.agent_runtimes.codex_adapter.asyncio.create_subprocess_exec",
        spawn,
    )

    version = await CodexRuntimeAdapter()._detect_version("C:/tools/codex.exe")

    assert version == "codex-cli 1.2.3"
    assert captured == [("C:/tools/codex.exe", "--version")]


async def test_event_normalization_and_terminal_result(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _CompletedProcess(
        [
            {"type": "thread.started", "thread_id": "thread-1"},
            {
                "type": "item.started",
                "item": {
                    "id": "call-1",
                    "type": "command_execution",
                    "command": "git status",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "call-1",
                    "type": "command_execution",
                    "command": "git status",
                    "aggregated_output": "clean",
                    "exit_code": 0,
                },
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ]
    )
    captured: dict = {}

    async def spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setenv("API_AUTH_TOKEN", "fleet-secret")
    monkeypatch.setenv("AGENT_API_TOKEN", "agent-secret")
    monkeypatch.setattr(
        "backend.agent_runtimes.codex_adapter.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))

    events = [event async for event in adapter.invoke(_task(tmp_path))]

    assert [event["type"] for event in events] == [
        "started",
        "state",
        "state",
        "tool_call",
        "tool_result",
        "text",
        "state",
        "done",
    ]
    assert events[1]["state"] == {"runtime": "codex", "codex_version": "codex-cli 1.2.3"}
    assert "working_directory" not in events[1]["state"]
    assert events[-1]["result"] == {
        "runtime": "codex",
        "codex_version": "codex-cli 1.2.3",
        "exit_code": 0,
        "text": "hello",
    }
    assert captured["kwargs"]["cwd"] == str(tmp_path.resolve())
    expected_group_key = "creationflags" if __import__("os").name == "nt" else "start_new_session"
    assert expected_group_key in captured["kwargs"]
    assert captured["argv"][0] == os.environ["AGENT_CODEX_ISOLATED_RUNNER"]
    assert "--ephemeral" in captured["argv"]
    assert "--ignore-user-config" in captured["argv"]
    assert captured["kwargs"]["env"].get("API_AUTH_TOKEN") is None
    assert captured["kwargs"]["env"].get("AGENT_API_TOKEN") is None
    assert captured["kwargs"]["env"].get("PATH") == os.environ.get("PATH")
    assert sum(event["type"] in {"done", "error"} for event in events) == 1


async def test_timeout_stops_process_and_yields_typed_error(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _HangingProcess()

    async def spawn(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(
        "backend.agent_runtimes.codex_adapter.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))

    events = [event async for event in adapter.invoke(_task(tmp_path, timeout_seconds=0.01))]

    assert process.returncode == -15
    assert events[-1]["type"] == "error"
    assert events[-1]["error_type"] == "TimeoutError"
    assert sum(event["type"] in {"done", "error"} for event in events) == 1


async def test_closing_stream_after_started_event_stops_process_tree(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _HangingProcess()

    async def spawn(*_argv, **_kwargs):
        return process

    monkeypatch.setattr(
        "backend.agent_runtimes.codex_adapter.asyncio.create_subprocess_exec",
        spawn,
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))

    stream = adapter.invoke(_task(tmp_path))
    assert (await anext(stream))["type"] == "started"
    await stream.aclose()

    assert process.returncode == -15


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group cleanup contract")
async def test_cleanup_targets_process_group_after_leader_has_exited(monkeypatch):
    adapter = CodexRuntimeAdapter()
    process = _CompletedProcess()
    process.pid = 4242
    signals: list[int] = []

    def kill_group(pid: int, sent_signal: int) -> None:
        assert pid == 4242
        signals.append(sent_signal)
        if sent_signal == 0:
            raise ProcessLookupError

    monkeypatch.setattr("backend.agent_runtimes.codex_adapter.os.killpg", kill_group)

    await adapter._stop_process(process)

    assert signals == [signal.SIGTERM, 0]


def test_codex_registered():
    assert "codex" in list_runtime_types()
    assert get_runtime("codex").runtime_type == "codex"


@pytest.mark.parametrize("phase", ["invoke", "version", "spawn", "invoke_spawn"])
async def test_repeated_cancellation_drains_owned_cleanup(monkeypatch, tmp_path, phase):
    adapter = CodexRuntimeAdapter()
    process = _HangingProcess()
    entered = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    spawn_release = asyncio.Event()

    async def spawn(*_args, **_kwargs):
        if phase.endswith("spawn"):
            entered.set()
            await spawn_release.wait()
        return process

    async def communicate():
        entered.set()
        await asyncio.Event().wait()

    async def cleanup(_process):
        cleanup_started.set()
        await release_cleanup.wait()
        process.terminate()

    process.communicate = communicate
    stop = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(adapter, "_stop_process", stop)
    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", spawn)

    async def consume():
        async for event in adapter.invoke(_task(tmp_path)):
            if event["type"] == "started":
                entered.set()

    if phase.startswith("invoke"):
        monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))
        pending = asyncio.create_task(consume())
    else:
        pending = asyncio.create_task(adapter._detect_version("isolated-runner"))
    await asyncio.wait_for(entered.wait(), 1)
    pending.cancel()
    spawn_release.set()
    await asyncio.wait_for(cleanup_started.wait(), 1)
    try:
        for _attempt in range(3):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done()
        assert process.returncode is None
    finally:
        release_cleanup.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert pending.cancelled()
    assert process.returncode == -15
    stop.assert_awaited_once_with(process)


@pytest.mark.parametrize("cancel_count", [1, 3])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancel_during_successful_probe_cleanup_never_starts_inference(
    monkeypatch, tmp_path, cancel_count, cleanup_fails
):
    adapter = CodexRuntimeAdapter()
    process = _CompletedProcess()
    process.communicate = AsyncMock(return_value=(b"codex-cli 1.2.3\n", b""))
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    failure = RuntimeInvocationError("process exit unconfirmed", "CleanupUnconfirmed")

    async def cleanup(_process):
        cleanup_started.set()
        await release_cleanup.wait()
        cleanup_finished.set()
        if cleanup_fails:
            raise failure

    spawn = AsyncMock(return_value=process)
    stop = AsyncMock(side_effect=cleanup)
    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adapter, "_stop_process", stop)

    async def consume():
        return [event async for event in adapter.invoke(_task(tmp_path))]

    pending = asyncio.create_task(consume())
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert pending.cancelling() == 0
    try:
        for _attempt in range(cancel_count):
            pending.cancel()
            await asyncio.sleep(0)
        assert not pending.done()
        assert not cleanup_finished.is_set()
    finally:
        release_cleanup.set()
        outcomes = await asyncio.gather(pending, return_exceptions=True)
    assert cleanup_finished.is_set()
    if cleanup_fails:
        assert outcomes == [failure]
        assert not pending.cancelled()
    else:
        assert isinstance(outcomes[0], asyncio.CancelledError)
        assert pending.cancelled()
    spawn.assert_awaited_once()
    assert spawn.call_args.args[-1] == "--version"
    stop.assert_awaited_once_with(process)


async def test_stderr_is_drained_concurrently_in_bounded_chunks(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _HangingProcess()

    class FloodedStderr:
        reads = 0

        async def read(self, size=-1):
            assert 0 < size <= 4096
            self.reads += 1
            if self.reads <= 100:
                return b"discarded-diagnostic".ljust(size, b"x")
            if self.reads == 101:
                return b"final diagnostic"
            process.returncode = 17
            process.stdout.feed_eof()
            return b""

    process.stderr = FloodedStderr()
    monkeypatch.setattr(adapter, "_stop_process", AsyncMock())
    monkeypatch.setattr(
        codex_adapter.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))
    events = [event async for event in adapter.invoke(_task(tmp_path, timeout_seconds=0.02))]

    assert events[-1]["error_type"] == "ProcessExitError"
    assert events[-1]["message"].endswith("final diagnostic")
    assert "discarded-diagnostic" not in events[-1]["message"]
    assert len(events[-1]["message"]) <= codex_adapter._STDERR_TAIL_BYTES + 40
    assert process.stderr.reads == 102


async def test_stderr_drain_is_closed_when_cancelled(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _HangingProcess()
    started = asyncio.Event()
    drained = asyncio.Event()

    class PendingStderr:
        async def read(self, size=-1):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

    process.stderr = PendingStderr()
    monkeypatch.setattr(
        codex_adapter.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))
    monkeypatch.setattr(adapter, "_stop_process", AsyncMock())

    async def consume():
        return [event async for event in adapter.invoke(_task(tmp_path))]

    pending = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(started.wait(), 1)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
    assert drained.is_set()
    assert pending.cancelled()


async def test_stderr_eof_wait_is_within_run_timeout(monkeypatch, tmp_path):
    adapter = CodexRuntimeAdapter()
    process = _CompletedProcess()
    process.stderr = asyncio.StreamReader()
    monkeypatch.setattr(
        codex_adapter.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))
    events = [event async for event in adapter.invoke(_task(tmp_path, timeout_seconds=0.01))]
    assert events[-1]["error_type"] == "TimeoutError"


async def test_cleanup_rejects_unconfirmed_process_group_exit(monkeypatch):
    adapter = CodexRuntimeAdapter()
    process = SimpleNamespace(pid=4242, returncode=0)
    kill_group = Mock()
    monkeypatch.setattr(codex_adapter, "os", SimpleNamespace(name="posix", killpg=kill_group))
    monkeypatch.setattr(codex_adapter, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    wait_exit = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_wait_for_process_group_exit", wait_exit)

    with pytest.raises(RuntimeInvocationError) as raised:
        await adapter._stop_process(process)

    assert raised.value.error_type == "CleanupUnconfirmed"
    assert wait_exit.await_count == 2
    assert kill_group.call_args_list[-1].args == (4242, 9)


async def test_cleanup_rejects_failed_windows_tree_termination(monkeypatch):
    adapter = CodexRuntimeAdapter()
    process = SimpleNamespace(pid=4242, returncode=0)
    monkeypatch.setattr(codex_adapter, "os", SimpleNamespace(name="nt"))
    terminate = Mock(return_value=SimpleNamespace(returncode=128))
    monkeypatch.setattr(codex_adapter.subprocess, "run", terminate)

    with pytest.raises(RuntimeInvocationError) as raised:
        await adapter._stop_process(process)

    assert raised.value.error_type == "CleanupUnconfirmed"
    assert terminate.call_args.args[0] == ["taskkill", "/PID", "4242", "/T", "/F"]


def test_sync_probe_cleanup_rejects_unconfirmed_process_group_exit(monkeypatch):
    process = SimpleNamespace(pid=4242, returncode=0, poll=Mock(return_value=0))
    kill_group = Mock()
    monkeypatch.setattr(codex_adapter, "os", SimpleNamespace(name="posix", killpg=kill_group))
    monkeypatch.setattr(codex_adapter, "signal", SimpleNamespace(SIGTERM=15, SIGKILL=9))
    monkeypatch.setattr(codex_adapter, "_KILL_GRACE_SECONDS", 0)

    with pytest.raises(RuntimeInvocationError) as raised:
        CodexRuntimeAdapter._stop_process_sync(process)

    assert raised.value.error_type == "CleanupUnconfirmed"
    assert [call.args for call in kill_group.call_args_list] == [
        (4242, 15),
        (4242, 0),
        (4242, 9),
        (4242, 0),
    ]
    assert process.poll.call_count == 2


async def test_cleanup_rejects_leader_that_never_exits(monkeypatch):
    process = SimpleNamespace(
        returncode=None, terminate=Mock(), kill=Mock(), wait=AsyncMock(side_effect=TimeoutError)
    )
    with pytest.raises(RuntimeInvocationError) as raised:
        await CodexRuntimeAdapter()._stop_process(process)

    assert raised.value.error_type == "CleanupUnconfirmed"
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert process.wait.await_count == 2


@pytest.mark.parametrize(
    "error",
    [
        RuntimeInvocationError("process still running", "CleanupUnconfirmed"),
        subprocess.TimeoutExpired("taskkill", 1),
    ],
)
def test_unconfirmed_probe_cleanup_never_advertises_runtime(monkeypatch, error):
    monkeypatch.setattr(CodexRuntimeAdapter, "_probe_runner", Mock(side_effect=error))
    assert CodexRuntimeAdapter.is_available() is False


async def test_operator_chat_disables_native_tools_and_passes_prompt_only_on_stdin(
    monkeypatch, tmp_path
):
    adapter = CodexRuntimeAdapter()
    process = _CompletedProcess()
    process.stdin = SimpleNamespace(write=Mock(), drain=AsyncMock(), close=Mock())
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(codex_adapter.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(adapter, "_detect_version", AsyncMock(return_value="codex-cli 1.2.3"))
    task = _task(tmp_path)
    task.workflow = "operator_chat"
    task.input = {"message": "private conversation content"}

    events = [event async for event in adapter.invoke(task)]

    argv = spawn.call_args.args
    assert argv[-1] == "-"
    assert not any("private conversation content" in argument for argument in argv)
    assert not any(task.instructions in argument for argument in argv)
    for feature in (
        "shell_tool",
        "unified_exec",
        "multi_agent",
        "apps",
        "plugins",
        "skill_search",
        "skill_mcp_dependency_install",
    ):
        index = argv.index(f"features.{feature}=false")
        assert argv[index - 1] == "-c"
    assert argv[argv.index('web_search="disabled"') - 1] == "-c"
    assert "--ignore-user-config" in argv
    assert "--ephemeral" in argv
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE
    process.stdin.write.assert_called_once_with(adapter._compose_prompt(task).encode())
    process.stdin.drain.assert_awaited_once()
    process.stdin.close.assert_called_once()
    assert events[-1]["type"] == "done"
