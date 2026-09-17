"""Registered-Agent adapter for the local Codex CLI.

The adapter is deliberately an edge-side subprocess adapter. The control plane
sends an ``agent_task`` over the authenticated Agent transport; only the
registered Agent process imports this module and starts an administrator-owned
isolated runner. Direct Codex execution is intentionally unsupported because a
read-only Codex sandbox can still read host files. No shell, caller-selected
executable, caller-selected sandbox, or inherited Agent secret is accepted.

Codex ``exec --json`` emits JSONL.  The native protocol has changed names a few
times, so translation accepts the stable ``thread.*``, ``turn.*`` and
``item.*`` envelopes while keeping our event envelope closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable
from pathlib import Path
from typing import Any

from backend.agent_runtimes.base import (
    AgentTask,
    RuntimeAdapter,
    RuntimeCapabilities,
    RuntimeInvocationError,
    RuntimeReadiness,
    event_done,
    event_error,
    event_started,
    event_state,
    event_text,
    event_tool_call,
    event_tool_result,
)
from backend.agent_runtimes.registry import register_runtime

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 1800
_MAX_TIMEOUT_SECONDS = 3600
_VERSION_TIMEOUT_SECONDS = 5
_KILL_GRACE_SECONDS = 10
_STDERR_TAIL_BYTES = 2048
_PERMISSION_MODES = frozenset({"observe_only", "suggest_changes"})
_CAPABILITY_ID = "runtime.codex"
_ISOLATED_RUNNER_ENV = "AGENT_CODEX_ISOLATED_RUNNER"
_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "APPDATA",
        "CODEX_HOME",
        "COMSPEC",
        "HOME",
        "LANG",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USER",
        "USERPROFILE",
        "WINDIR",
    }
)
_SHELL_ENVIRONMENT_POLICY = (
    'shell_environment_policy.include_only=["PATH","HOME","USERPROFILE","TEMP",'
    '"TMP","SYSTEMROOT","WINDIR","COMSPEC","PATHEXT","LANG","LC_*"]'
)
_VERSION_RE = re.compile(r"\bcodex(?:[- ]cli)?(?:\s+version)?\s+([0-9][0-9A-Za-z.+-]*)\b", re.I)


async def _finish_cleanup(operation: Awaitable[Any]) -> Any:
    pending = asyncio.ensure_future(operation)
    cancellation: asyncio.CancelledError | None = None
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
    result = pending.result()
    if cancellation is not None:
        raise cancellation
    return result


@register_runtime
class CodexRuntimeAdapter(RuntimeAdapter):
    """Run ``codex exec --json`` on a registered local Agent node."""

    runtime_type = "codex"
    capabilities = RuntimeCapabilities(
        transport="stdio",
        streaming=True,
        resume_by_id=False,
        checkpoint="none",
        concurrent_sessions=True,
        features=frozenset({"operator_chat"}),
    )

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        unsupported = sorted(set(config) - {"cwd", "timeout_seconds", "permission_mode"})
        if unsupported:
            errors.append("unsupported controller runtime config: " + ", ".join(unsupported))

        cwd = config.get("cwd")
        if not isinstance(cwd, str) or not cwd.strip():
            errors.append("'cwd' must be a non-empty controller-owned worktree path")
        elif "\x00" in cwd:
            errors.append("'cwd' must not contain NUL bytes")

        permission_mode = config.get("permission_mode")
        if permission_mode is not None and permission_mode not in _PERMISSION_MODES:
            errors.append(
                "'permission_mode' must be one of " + ", ".join(sorted(_PERMISSION_MODES))
            )
        if "timeout_seconds" in config and config["timeout_seconds"] is not None:
            timeout = config["timeout_seconds"]
            if (
                not isinstance(timeout, (int, float))
                or isinstance(timeout, bool)
                or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
            ):
                errors.append(
                    f"'timeout_seconds' must be between 0 and {_MAX_TIMEOUT_SECONDS} when provided"
                )
        return errors

    async def health(self) -> bool:
        return self.is_available()

    @classmethod
    def is_available(cls) -> bool:
        """Fail-closed compatibility check used by Agent registration."""
        try:
            runner = cls._configured_runner()
            cls._configured_roots()
            return cls._probe_runner(runner)
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeInvocationError):
            return False

    async def readiness(self, config: dict[str, Any] | None = None) -> RuntimeReadiness:
        config = config or {}
        errors = self.validate_config(config)
        try:
            resolved_binary = self._configured_runner()
        except ValueError as exc:
            return RuntimeReadiness(
                runtime=self.runtime_type,
                capability_id=_CAPABILITY_ID,
                status="blocked",
                binary_present=False,
                reason_code="missing_isolated_runner",
                reason=str(exc),
            )
        if errors:
            return RuntimeReadiness(
                runtime=self.runtime_type,
                capability_id=_CAPABILITY_ID,
                status="blocked",
                binary_present=resolved_binary is not None,
                reason_code="invalid_config",
                reason="; ".join(errors),
            )

        try:
            cwd, permitted_root = self._resolve_cwd(config)
        except ValueError as exc:
            return RuntimeReadiness(
                runtime=self.runtime_type,
                capability_id=_CAPABILITY_ID,
                status="blocked",
                binary_present=True,
                permitted_project_root=self._display_path(config.get("cwd")),
                working_directory=self._display_path(config.get("cwd")),
                reason_code="invalid_path",
                reason=str(exc),
            )

        version = await self._detect_version(
            resolved_binary, timeout_seconds=_VERSION_TIMEOUT_SECONDS
        )
        if version is None:
            return RuntimeReadiness(
                runtime=self.runtime_type,
                capability_id=_CAPABILITY_ID,
                status="blocked",
                binary_present=True,
                permitted_project_root=str(permitted_root),
                working_directory=str(cwd),
                reason_code="isolated_runner_probe_failed",
                reason="isolated Codex runner did not complete a compatible version probe",
            )
        return RuntimeReadiness(
            runtime=self.runtime_type,
            capability_id=_CAPABILITY_ID,
            status="ready",
            binary_present=True,
            version=version,
            permitted_project_root=str(permitted_root),
            working_directory=str(cwd),
        )

    @staticmethod
    def _compose_argv(
        binary: str, cwd: Path, prompt: str = "", *, chat_only: bool = False
    ) -> list[str]:
        # Operations Agent profiles that may reach this adapter are advisory.
        # Workbench consumes the returned patch and applies it through its own
        # guarded pipeline, so the runtime itself never needs write access.
        argv = [
            binary,
            "exec",
            "--json",
            "--color",
            "never",
            "--ephemeral",
            "--ignore-user-config",
            "-c",
            'shell_environment_policy.inherit="core"',
            "-c",
            _SHELL_ENVIRONMENT_POLICY,
            "-c",
            "shell_environment_policy.ignore_default_excludes=false",
            "--sandbox",
            "read-only",
            "--cd",
            str(cwd),
        ]
        if chat_only:
            argv.extend(
                [
                    "--skip-git-repo-check",
                    "-c",
                    "features.shell_tool=false",
                    "-c",
                    "features.unified_exec=false",
                    "-c",
                    "features.multi_agent=false",
                    "-c",
                    "features.apps=false",
                    "-c",
                    "features.plugins=false",
                    "-c",
                    "features.skill_search=false",
                    "-c",
                    "features.skill_mcp_dependency_install=false",
                    "-c",
                    'web_search="disabled"',
                ]
            )
        return [*argv, "-" if chat_only else prompt]

    def _compose_prompt(self, task: AgentTask) -> str:
        payload = task.input if isinstance(task.input, dict) else {}
        message = payload.get("message") or payload.get("prompt") or ""
        if not isinstance(message, str):
            message = str(message)
        if task.instructions:
            return f"{task.instructions}\n\n{message}".strip()
        return message

    async def invoke(self, task: AgentTask) -> AsyncIterator[dict[str, Any]]:
        config = task.config or {}
        config_errors = self.validate_config(config)
        if config_errors:
            yield event_error(task.task_id, "; ".join(config_errors), error_type="ConfigError")
            return

        try:
            resolved_binary = self._configured_runner()
        except ValueError as exc:
            yield event_error(
                task.task_id,
                str(exc),
                error_type="FileNotFoundError",
            )
            return
        try:
            cwd, _permitted_root = self._resolve_cwd(config)
        except ValueError as exc:
            yield event_error(task.task_id, str(exc), error_type="PathError")
            return

        timeout_seconds = config.get("timeout_seconds") or _DEFAULT_TIMEOUT_SECONDS
        version = await self._detect_version(
            resolved_binary,
            timeout_seconds=min(timeout_seconds, _VERSION_TIMEOUT_SECONDS),
        )
        chat_only = task.workflow == "operator_chat"
        prompt = self._compose_prompt(task)
        argv = self._compose_argv(resolved_binary, cwd, prompt, chat_only=chat_only)

        try:
            proc = await self._spawn_owned(
                *argv,
                stdin=asyncio.subprocess.PIPE if chat_only else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=self._subprocess_env(),
                **self._process_group_kwargs(),
            )
        except FileNotFoundError as exc:
            yield event_error(task.task_id, "isolated Codex runner not found", type(exc).__name__)
            return
        except OSError as exc:
            yield event_error(task.task_id, f"failed to spawn codex: {exc}", type(exc).__name__)
            return

        stderr_tail = bytearray()
        stderr_drain = asyncio.create_task(self._drain_stderr(proc, stderr_tail))
        stream = self._stream_process(
            task, proc, timeout_seconds, version, stderr_tail, stderr_drain
        )
        try:
            if chat_only:
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
                proc.stdin.close()
            async for event in stream:
                yield event
        finally:

            async def cleanup() -> None:
                try:
                    await stream.aclose()
                finally:
                    try:
                        await self._stop_process(proc)
                    finally:
                        stderr_drain.cancel()
                        await asyncio.gather(stderr_drain, return_exceptions=True)

            await _finish_cleanup(cleanup())

    async def _spawn_owned(self, *argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        pending = asyncio.create_task(asyncio.create_subprocess_exec(*argv, **kwargs))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:

            async def cleanup() -> None:
                process = await pending
                await self._stop_process(process)

            await _finish_cleanup(cleanup())
            raise

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process, tail: bytearray) -> None:
        if proc.stderr is None:
            return
        while chunk := await proc.stderr.read(4096):
            tail.extend(chunk)
            del tail[:-_STDERR_TAIL_BYTES]

    async def _stream_process(
        self,
        task: AgentTask,
        proc: asyncio.subprocess.Process,
        timeout_seconds: float,
        version: str | None,
        stderr_tail: bytearray,
        stderr_drain: asyncio.Task[None],
    ) -> AsyncIterator[dict[str, Any]]:
        accumulated_text: list[str] = []
        native_error: str | None = None

        async def _read_events() -> AsyncIterator[dict[str, Any]]:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                stripped = line.decode(errors="replace").strip("\r\n")
                if not stripped:
                    continue
                try:
                    native = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.debug("codex_adapter: skipping non-JSON stdout line: %r", stripped[:200])
                    continue
                if not isinstance(native, dict):
                    continue
                translated = self._translate_event(task.task_id, native)
                if translated is not None:
                    yield translated

        yield event_started(task.task_id)
        yield event_state(
            task.task_id,
            {"runtime": self.runtime_type, "codex_version": version},
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                async for event in _read_events():
                    if event["type"] == "text":
                        accumulated_text.append(event.get("text", ""))
                    elif event["type"] == "error":
                        native_error = event.get("message") or "Codex reported an error"
                        break
                    yield event
                if native_error is None:
                    returncode = await proc.wait()
                    await stderr_drain
        except TimeoutError:
            yield event_error(
                task.task_id,
                f"codex run timed out after {timeout_seconds}s",
                error_type="TimeoutError",
            )
            return

        if native_error is not None:
            yield event_error(task.task_id, native_error, error_type="RuntimeInvocationError")
            return

        if returncode != 0:
            tail = stderr_tail.decode(errors="replace")
            detail = f": {tail}" if tail else ""
            yield event_error(
                task.task_id,
                f"codex exited with code {returncode}{detail}",
                error_type="ProcessExitError",
            )
            return

        yield event_done(
            task.task_id,
            result={
                "runtime": self.runtime_type,
                "codex_version": version,
                "exit_code": returncode,
                "text": "".join(accumulated_text),
            },
        )

    async def _detect_version(
        self,
        binary: str,
        timeout_seconds: float = _VERSION_TIMEOUT_SECONDS,
    ) -> str | None:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await self._spawn_owned(
                binary,
                "--version",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._subprocess_env(),
                **self._process_group_kwargs(),
            )
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        except TimeoutError:
            return None
        except OSError:
            return None
        except asyncio.CancelledError:
            raise
        finally:
            if proc is not None:
                await _finish_cleanup(self._stop_process(proc))
        line = stdout.decode(errors="replace").splitlines()[0].strip() if stdout else ""
        match = _VERSION_RE.search(line)
        return match.group(0) if match else None

    @classmethod
    def _probe_runner(cls, binary: str) -> bool:
        """Synchronously verify the isolated runner before Fleet publication."""

        proc: subprocess.Popen[bytes] | None = None
        try:
            proc = subprocess.Popen(
                [binary, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=cls._subprocess_env(),
                **cls._process_group_kwargs(),
            )
            stdout, _stderr = proc.communicate(timeout=_VERSION_TIMEOUT_SECONDS)
            if proc.returncode != 0:
                return False
            line = stdout.decode(errors="replace").splitlines()[0].strip() if stdout else ""
            return _VERSION_RE.search(line) is not None
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            if proc is not None:
                cls._stop_process_sync(proc)

    @staticmethod
    def _stop_process_sync(proc: subprocess.Popen[bytes]) -> None:
        """Confirm process-tree exit for the registration-time probe."""

        pid = proc.pid
        if os.name == "nt":
            termination = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=_KILL_GRACE_SECONDS,
            )
            if termination.returncode != 0:
                raise RuntimeInvocationError(
                    "Runtime process-tree termination was not confirmed", "CleanupUnconfirmed"
                )
            if proc.returncode is None:
                try:
                    proc.wait(timeout=_KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=_KILL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeInvocationError(
                            "Runtime process exit was not confirmed", "CleanupUnconfirmed"
                        ) from exc
            return

        for stop_signal in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pid, stop_signal)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + _KILL_GRACE_SECONDS
            while True:
                proc.poll()
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    return
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        raise RuntimeInvocationError(
            "Runtime process-group exit was not confirmed", "CleanupUnconfirmed"
        )

    async def _stop_process(self, proc: asyncio.subprocess.Process) -> None:
        pid = getattr(proc, "pid", None)
        if pid is None:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
                except TimeoutError:
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
                    except TimeoutError as exc:
                        raise RuntimeInvocationError(
                            "Runtime process exit was not confirmed", "CleanupUnconfirmed"
                        ) from exc
            return
        elif os.name == "nt":
            termination = await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=_KILL_GRACE_SECONDS,
            )
            if termination.returncode != 0:
                raise RuntimeInvocationError(
                    "Runtime process-tree termination was not confirmed", "CleanupUnconfirmed"
                )
        else:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                return

        if os.name != "nt":
            if not await self._wait_for_process_group_exit(pid, _KILL_GRACE_SECONDS):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    return
                if not await self._wait_for_process_group_exit(pid, _KILL_GRACE_SECONDS):
                    raise RuntimeInvocationError(
                        "Runtime process-group exit was not confirmed", "CleanupUnconfirmed"
                    )
        elif proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
            except TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
                except TimeoutError as exc:
                    raise RuntimeInvocationError(
                        "Runtime process exit was not confirmed", "CleanupUnconfirmed"
                    ) from exc

    @staticmethod
    async def _wait_for_process_group_exit(pid: int, timeout_seconds: float) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    @staticmethod
    def _process_group_kwargs() -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        """Return the minimal non-secret environment visible to the isolated runner."""

        return {
            key: value
            for key, value in os.environ.items()
            if key.upper() in _SAFE_ENVIRONMENT_KEYS or key.upper().startswith("LC_")
        }

    @staticmethod
    def _configured_runner() -> str:
        raw = os.environ.get(_ISOLATED_RUNNER_ENV, "").strip()
        if not raw:
            raise ValueError(
                "AGENT_CODEX_ISOLATED_RUNNER is required; direct Codex execution is disabled"
            )
        runner = Path(raw).expanduser()
        if not runner.is_absolute():
            raise ValueError("AGENT_CODEX_ISOLATED_RUNNER must be an absolute path")
        try:
            resolved = runner.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"isolated Codex runner is unavailable: {runner}") from exc
        if not resolved.is_file():
            raise ValueError(f"isolated Codex runner is not a file: {resolved}")
        if not os.access(resolved, os.X_OK):
            raise ValueError(f"isolated Codex runner is not executable: {resolved}")
        return str(resolved)

    @staticmethod
    def _configured_roots() -> list[Path]:
        raw = os.environ.get("AGENT_CODEX_ALLOWED_ROOTS", "").strip()
        if not raw:
            raise ValueError("AGENT_CODEX_ALLOWED_ROOTS is not configured on this Agent")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "AGENT_CODEX_ALLOWED_ROOTS must be a JSON array of absolute paths"
            ) from exc
        if not isinstance(values, list) or not values:
            raise ValueError("AGENT_CODEX_ALLOWED_ROOTS must be a non-empty JSON array")

        roots: list[Path] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("AGENT_CODEX_ALLOWED_ROOTS entries must be non-empty paths")
            root = Path(value).expanduser()
            if not root.is_absolute():
                raise ValueError("AGENT_CODEX_ALLOWED_ROOTS entries must be absolute paths")
            try:
                resolved = root.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"configured Codex worktree root is unavailable: {root}") from exc
            if not resolved.is_dir():
                raise ValueError(f"configured Codex worktree root is not a directory: {resolved}")
            roots.append(resolved)
        return roots

    @classmethod
    def _resolve_cwd(cls, config: dict[str, Any]) -> tuple[Path, Path]:
        cwd_raw = config.get("cwd")
        if not isinstance(cwd_raw, str) or not cwd_raw.strip():
            raise ValueError("controller-owned working directory is required")
        try:
            cwd = Path(cwd_raw).expanduser().resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"working directory is unavailable: {cwd_raw}") from exc
        if not cwd.is_dir():
            raise ValueError(f"working directory is not a directory: {cwd}")
        for root in cls._configured_roots():
            try:
                cwd.relative_to(root)
            except ValueError:
                continue
            return cwd, root
        raise ValueError("working directory is outside the Agent's permitted Codex roots")

    @staticmethod
    def _display_path(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        return str(Path(value).expanduser().resolve())

    def _translate_event(self, task_id: str, native: dict[str, Any]) -> dict[str, Any] | None:
        native_type = native.get("type")
        if native_type in {"error", "turn.error"}:
            return event_error(
                task_id, str(native.get("message") or native.get("error") or "Codex error")
            )
        if native_type == "thread.started":
            return event_state(task_id, {"thread_id": native.get("thread_id")})
        if native_type == "turn.started":
            return event_state(task_id, {"turn": "started"})
        if native_type == "turn.completed":
            state: dict[str, Any] = {"turn": "completed"}
            if isinstance(native.get("usage"), dict):
                state["usage"] = native["usage"]
            return event_state(task_id, state)

        item = native.get("item") if isinstance(native.get("item"), dict) else native
        item_type = item.get("type")
        if item_type in {"agent_message", "assistant_message", "text"}:
            text = item.get("text") or item.get("content") or native.get("text")
            return event_text(task_id, text) if isinstance(text, str) and text else None
        if item_type in {"command_execution", "tool_call", "function_call"}:
            if native_type in {"item.completed", "tool_result", "function_result"}:
                output = item.get("aggregated_output", item.get("output", item.get("result")))
                exit_code = item.get("exit_code")
                return event_tool_result(
                    task_id,
                    name=str(item.get("command") or item.get("name") or "codex_tool"),
                    result=output,
                    call_id=item.get("id") or item.get("call_id"),
                    is_error=bool(item.get("is_error")) or exit_code not in (None, 0),
                )
            return event_tool_call(
                task_id,
                name=str(item.get("command") or item.get("name") or "codex_tool"),
                args=item.get("arguments")
                if isinstance(item.get("arguments"), dict)
                else {"command": item.get("command", "")},
                call_id=item.get("id") or item.get("call_id"),
            )
        if native_type in {"text", "output_text.delta", "response.output_text.delta"}:
            text = native.get("text") or native.get("delta")
            return event_text(task_id, text) if isinstance(text, str) and text else None
        logger.debug("codex_adapter: skipping unmapped native event type %r", native_type)
        return None
