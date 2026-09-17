"""Inference-only OMP RPC on an administrator-owned isolated Fleet runner.

The runner's --operator-chat-probe must attest rpc-v1, operator_chat=true,
and native_tools/mcp/extensions/user_context=false. The runner, not the raw
OMP CLI, enforces the SDK restrictions and an auth-only isolated profile.
"""

import asyncio
import json
import math
import os
import re
import subprocess
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
    event_text,
)
from backend.agent_runtimes.codex_adapter import CodexRuntimeAdapter
from backend.agent_runtimes.registry import register_runtime

_MAX_FRAME_BYTES = 1024 * 1024
_SHUTDOWN_SECONDS = 2
_PROBE_TIMEOUT_SECONDS = 20
_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_SYSTEM_PROMPT = "You are an inference-only assistant. Follow the supplied conversation."


async def _finish_cleanup(operation: Awaitable[Any]) -> Any:
    pending = asyncio.ensure_future(operation)
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            continue
    return pending.result()


async def _owned_thread(function, *args):
    pending = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        await _finish_cleanup(pending)
        raise


@register_runtime
class OmpRuntimeAdapter(RuntimeAdapter):
    runtime_type = "omp"
    capabilities = RuntimeCapabilities(
        transport="stdio",
        streaming=True,
        resume_by_id=False,
        features=frozenset({"inference_only", "operator_chat"}),
    )
    _process_guard = CodexRuntimeAdapter()

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        errors = self._process_guard.validate_config(config)
        cwd = config.get("cwd")
        if isinstance(cwd, str) and cwd and not Path(cwd).is_absolute():
            errors.append("'cwd' must be absolute")
        timeout = config.get("timeout_seconds")
        if isinstance(timeout, (int, float)) and not math.isfinite(timeout):
            errors.append("'timeout_seconds' must be finite")
        return errors

    @staticmethod
    def _configured_paths() -> tuple[Path, list[Path]]:
        raw_runner = os.environ.get("AGENT_OMP_ISOLATED_RUNNER", "")
        runner = Path(raw_runner)
        if not raw_runner or not runner.is_absolute():
            raise ValueError("AGENT_OMP_ISOLATED_RUNNER must name an absolute isolated runner")
        runner = runner.resolve(strict=True)
        if not runner.is_file() or not os.access(runner, os.X_OK):
            raise ValueError("OMP isolated runner is not executable")
        if os.name == "nt" and runner.suffix.lower() in {".cmd", ".bat", ".ps1"}:
            raise ValueError("OMP isolated runner must not require a Windows shell")
        try:
            values = json.loads(os.environ.get("AGENT_OMP_ALLOWED_ROOTS", ""))
        except json.JSONDecodeError as exc:
            raise ValueError(
                "AGENT_OMP_ALLOWED_ROOTS must be a JSON array of absolute roots"
            ) from exc
        if not isinstance(values, list) or not values:
            raise ValueError("AGENT_OMP_ALLOWED_ROOTS must contain absolute roots")
        roots = []
        for value in values:
            if not isinstance(value, str) or not Path(value).is_absolute():
                raise ValueError("AGENT_OMP_ALLOWED_ROOTS must contain absolute roots")
            root = Path(value).resolve(strict=True)
            if not root.is_dir():
                raise ValueError("OMP allowed root is not a directory")
            if runner.is_relative_to(root):
                raise ValueError("OMP isolated runner must be outside the permitted worktrees")
            roots.append(root)
        return runner, roots

    @classmethod
    def _launch_paths(cls, config: dict[str, Any]) -> tuple[Path, Path]:
        runner, roots = cls._configured_paths()
        cwd = Path(config["cwd"]).resolve(strict=True)
        if not cwd.is_dir() or not any(cwd.is_relative_to(root) for root in roots):
            raise ValueError("working directory is outside the Agent's permitted OMP roots")
        return runner, cwd

    @classmethod
    def _subprocess_env(cls) -> dict[str, str]:
        return {
            key: value
            for key, value in cls._process_guard._subprocess_env().items()
            if key.upper() != "CODEX_HOME"
        }

    @classmethod
    def _probe_runner(cls, runner: Path) -> str | None:
        process = None
        try:
            process = subprocess.Popen(
                [str(runner), "--operator-chat-probe"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=str(cls._configured_paths()[1][0]),
                env=cls._subprocess_env(),
                **cls._process_guard._process_group_kwargs(),
            )
            stdout, _stderr = process.communicate(timeout=_PROBE_TIMEOUT_SECONDS)
            proof = json.loads(stdout)
            if not isinstance(proof, dict) or not isinstance(proof.get("version"), str):
                return None
            match = _VERSION_RE.fullmatch(proof["version"])
            safe = (
                proof.get("runtime") == "omp"
                and proof.get("protocol") == "rpc-v1"
                and proof.get("operator_chat") is True
                and all(
                    proof.get(control) is False
                    for control in ("native_tools", "mcp", "extensions", "user_context")
                )
            )
            if process.returncode == 0 and match and safe:
                if tuple(int(part) for part in match.groups()) >= (18, 1, 15):
                    return match.group(0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
        finally:
            if process is not None:
                cls._process_guard._stop_process_sync(process)
        return None

    @classmethod
    def is_available(cls) -> bool:
        try:
            runner, _roots = cls._configured_paths()
            return cls._probe_runner(runner) is not None
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeInvocationError):
            return False

    async def health(self) -> bool:
        return await _owned_thread(self.is_available)

    async def readiness(self, config: dict[str, Any] | None = None) -> RuntimeReadiness:
        config = config or {}
        errors = self.validate_config(config)
        version = None
        reason = "invalid_config" if errors else None
        if not reason:
            try:
                runner, _cwd = self._launch_paths(config)
                version = await _owned_thread(self._probe_runner, runner)
                if version is None:
                    reason = "isolated_runner_probe_failed"
            except (ValueError, OSError):
                reason = "invalid_isolated_runner_or_root"
        return RuntimeReadiness(
            runtime=self.runtime_type,
            capability_id="runtime.omp",
            status="blocked" if reason else "ready",
            binary_present=version is not None,
            version=version,
            reason_code=reason,
            reason="OMP requires a compatible isolated runner and permitted cwd"
            if reason
            else None,
        )

    @staticmethod
    def _compose_argv(runner: Path, task: AgentTask) -> list[str]:
        if task.provider is not None or task.model is not None:
            raise ValueError("OMP operator_chat does not support provider or model overrides")
        argv = [
            str(runner),
            "--operator-chat",
            "--mode",
            "rpc",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
            "--no-lsp",
            "--no-pty",
            "--no-title",
            "--no-prewalk",
            "--system-prompt",
            _SYSTEM_PROMPT,
        ]
        return argv

    @staticmethod
    async def _write_frame(process, frame: dict[str, Any]) -> None:
        payload = (json.dumps(frame, ensure_ascii=False) + "\n").encode()
        if len(payload) > _MAX_FRAME_BYTES:
            raise RuntimeInvocationError("OMP request exceeds the RPC frame limit", "FrameTooLarge")
        process.stdin.write(payload)
        await process.stdin.drain()

    @staticmethod
    async def _read_frame(process) -> dict[str, Any]:
        try:
            line = await process.stdout.readline()
            if not line:
                raise RuntimeInvocationError(
                    "OMP exited before its final response", "UnexpectedEOF"
                )
            if len(line) > _MAX_FRAME_BYTES:
                raise ValueError("frame too large")
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise ValueError("frame is not an object")
            return frame
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeInvocationError("Invalid OMP RPC frame", "ProtocolError") from exc

    @staticmethod
    async def _drain_stderr(process) -> None:
        while await process.stderr.read(8192):
            pass

    async def _shutdown(self, process, stderr_task, *, abort: bool) -> None:
        try:
            if process.returncode is None:
                async with asyncio.timeout(_SHUTDOWN_SECONDS):
                    if abort:
                        await self._write_frame(process, {"id": "stop", "type": "abort"})
                        while True:
                            frame = await self._read_frame(process)
                            if frame.get("type") == "response" and frame.get("id") == "stop":
                                break
                    process.stdin.close()
                    await process.wait()
        except (TimeoutError, OSError, RuntimeInvocationError):
            pass
        finally:
            try:
                await self._process_guard._stop_process(process)
            finally:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)

    @staticmethod
    def _assistant_text(message: Any) -> str:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return ""
        if message.get("stopReason") in {"error", "aborted"}:
            raise RuntimeInvocationError("OMP model response failed or was aborted", "ModelError")
        content = message.get("content", [])
        if not isinstance(content, list):
            raise RuntimeInvocationError("Invalid OMP assistant message", "ProtocolError")
        if any(isinstance(part, dict) and part.get("type") == "toolCall" for part in content):
            raise RuntimeInvocationError("OMP native tools are disabled", "NativeToolDenied")
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )

    async def invoke(self, task: AgentTask) -> AsyncIterator[dict[str, Any]]:
        errors = self.validate_config(task.config)
        if task.workflow != "operator_chat":
            errors.append("OMP only supports the operator_chat inference workflow")
        if task.session_id is not None:
            errors.append("OMP native resume is disabled; supply bounded conversation history")
        if errors:
            yield event_error(task.task_id, "; ".join(errors), "ConfigError")
            return
        try:
            runner, cwd = self._launch_paths(task.config)
            argv = self._compose_argv(runner, task)
            if not isinstance(task.input, dict):
                raise ValueError("OMP requires a text inference request")
            message = task.input.get("message", task.input.get("prompt", ""))
            if not isinstance(message, str) or not isinstance(task.instructions, str):
                raise ValueError("OMP requires a text inference request")
            prompt = f"Supplied conversation:\n{task.instructions}\n\n{message}"
            request = {"id": task.task_id, "type": "prompt", "message": prompt}
            if len(json.dumps(request, ensure_ascii=False).encode()) >= _MAX_FRAME_BYTES:
                raise ValueError("OMP request exceeds the RPC frame limit")
        except (ValueError, OSError):
            yield event_error(
                task.task_id, "Invalid isolated OMP inference configuration", "ConfigError"
            )
            return

        if await _owned_thread(self._probe_runner, runner) is None:
            yield event_error(
                task.task_id, "OMP inference isolation is not verified", "UnsafeRunner"
            )
            return

        pending_spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=self._subprocess_env(),
                limit=_MAX_FRAME_BYTES + 1,
                **self._process_guard._process_group_kwargs(),
            )
        )
        try:
            process = await asyncio.shield(pending_spawn)
        except asyncio.CancelledError:

            async def finish_spawn():
                spawned = await pending_spawn
                await self._process_guard._stop_process(spawned)

            await _finish_cleanup(finish_spawn())
            raise
        except OSError:
            yield event_error(task.task_id, "Could not start the isolated OMP runner", "SpawnError")
            return

        stderr_task = asyncio.create_task(self._drain_stderr(process))
        completed = False
        acknowledged = False
        text_parts: list[str] = []
        text_size = 0
        final_text = ""
        failure = None
        try:
            yield event_started(task.task_id)
            async with asyncio.timeout(task.config.get("timeout_seconds") or 1800):
                ready = await self._read_frame(process)
                if ready.get("type") != "ready" or ready.get("protocolVersion") != 1:
                    raise RuntimeInvocationError("Unsupported OMP RPC handshake", "ProtocolError")
                await self._write_frame(process, request)
                while not (completed and acknowledged):
                    frame = await self._read_frame(process)
                    kind = frame.get("type", "")
                    if kind == "response" and frame.get("id") == task.task_id:
                        if frame.get("success") is not True:
                            raise RuntimeInvocationError(
                                "OMP rejected the inference request", "ModelError"
                            )
                        acknowledged = True
                    elif kind == "extension_ui_request" or str(kind).startswith("tool_execution_"):
                        raise RuntimeInvocationError(
                            "OMP native tools are disabled", "NativeToolDenied"
                        )
                    elif kind == "message_update":
                        delta = frame.get("assistantMessageEvent")
                        if isinstance(delta, dict) and delta.get("type") == "text_delta":
                            text = delta.get("delta")
                            if isinstance(text, str) and text:
                                text_parts.append(text)
                                text_size += len(text)
                                if text_size > _MAX_FRAME_BYTES:
                                    raise RuntimeInvocationError(
                                        "OMP response is too large", "FrameTooLarge"
                                    )
                                yield event_text(task.task_id, text)
                    elif kind == "message_end":
                        final_text = self._assistant_text(frame.get("message")) or final_text
                    elif kind == "agent_end":
                        messages = frame.get("messages", [])
                        if not isinstance(messages, list):
                            raise RuntimeInvocationError(
                                "Invalid OMP final messages", "ProtocolError"
                            )
                        for native_message in messages:
                            final_text = self._assistant_text(native_message) or final_text
                        completed = True
                    elif kind in {"error", "extension_error"}:
                        raise RuntimeInvocationError(
                            "OMP runtime reported an error", "RuntimeError"
                        )
        except TimeoutError:
            failure = event_error(task.task_id, "OMP inference timed out", "TimeoutError")
        except (RuntimeInvocationError, OSError) as exc:
            failure = event_error(
                task.task_id,
                str(exc)
                if isinstance(exc, RuntimeInvocationError)
                else "OMP RPC connection failed",
                exc.error_type if isinstance(exc, RuntimeInvocationError) else "ConnectionError",
            )
        finally:
            await _finish_cleanup(self._shutdown(process, stderr_task, abort=not completed))

        if failure is not None:
            yield failure
        elif process.returncode != 0:
            yield event_error(task.task_id, "OMP runner did not exit cleanly", "ProcessExitError")
        elif not final_text and not text_parts:
            yield event_error(task.task_id, "OMP returned no assistant response", "EmptyResponse")
        else:
            yield event_done(
                task.task_id,
                {
                    "runtime": "omp",
                    "text": final_text or "".join(text_parts),
                    "exit_code": 0,
                },
            )
