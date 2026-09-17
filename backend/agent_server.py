"""OpenCLI Agent Server — runs on LAN/NAT edge nodes.

Accepts HTTP POST /collect requests from the center API, executes opencli
locally (pointing at the node's own Chrome instance), and returns results.

Registration modes (AGENT_REGISTER):
  http  — LAN mode: agent POSTs its URL to center; center calls back via HTTP.
           Requires the agent to be reachable from the center.
  ws    — NAT/reverse-channel mode: agent initiates a persistent WebSocket to
           the center's /api/v1/nodes/ws endpoint.  The center pushes
           collect tasks down the WS connection; agent returns results in-band.
           Use this when the center cannot reach the agent (NAT, firewall, etc.).
  off   — Disable auto-registration entirely.

Usage on the edge node:
    pip install fastapi uvicorn httpx pyyaml websockets
    python -m backend.agent_server
    # or standalone:
    uvicorn backend.agent_server:app --host 0.0.0.0 --port 19823

Environment variables:
    AGENT_PORT              HTTP port to listen on (default: 19823)
    AGENT_ADVERTISE_URL     Canonical URL the center uses to identify this agent
                            (default: auto-detected from outbound IP)
    AGENT_MODE              Collection mode reported to center: bridge | cdp (default: bridge)
    AGENT_LABEL             Human-readable label for this agent (default: hostname)
    AGENT_REGISTER          Registration mode: http | ws | off (default: http)
    CENTRAL_API_URL         Center API base URL for self-registration
                            e.g. http://192.168.1.1:8031
                            Leave empty to skip auto-registration.
    HTTP_PROXY              HTTP proxy for outbound requests (agent → center)
    HTTPS_PROXY             HTTPS proxy for outbound requests (agent → center)
    AGENT_API_TOKEN         Fleet auth bearer token (ADR-0005) attached to both the
                            HTTP register call and the WS reverse-channel handshake.
                            Preferred name for this process; takes priority.
    API_AUTH_TOKEN          Fallback token env var — a node sharing the center's
                            process environment (e.g. same .env) just works without
                            a separate AGENT_API_TOKEN. Ignored if AGENT_API_TOKEN is set.
    OPENCLI_BRIDGE_BIN      Path to opencli 1.0 binary (default: /opt/opencli-bridge/bin/opencli)
    OPENCLI_CDP_BIN         Path to opencli 0.9 binary (default: /opt/opencli-cdp/bin/opencli)
    OPENCLI_CDP_ENDPOINT    Default Chrome CDP endpoint (default: http://localhost:19222)
    OPENCLI_DAEMON_PORT     Bridge daemon port (default: 19825)
    OPENCLI_TIMEOUT         opencli subprocess timeout in seconds (default: 120)
    AGENT_CODEX_ISOLATED_RUNNER Absolute path to an administrator-owned, externally
                            isolated Codex-compatible runner. Direct CLI execution
                            is disabled.
    AGENT_CODEX_ALLOWED_ROOTS JSON array of server-owned roots allowed for Codex cwd.
                            Empty or invalid configuration disables Codex dispatch.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse
from urllib.request import proxy_bypass

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from backend.agent_runtime_dispatch import (
    RuntimeInvokeRequest,
    cleanup_cdp_tabs,
    invoke_runtime,
    parse_output,
    snapshot_tab_ids,
)

# Imported directly from the registry submodule (not the `backend.agent_runtimes`
# package __init__) so this module's import graph is pinned to what registry.py
# itself pulls in — stdlib only, verified against pi_adapter.py's imports (also
# stdlib-only). agent_server.py runs standalone on edge nodes with a minimal
# dependency set (see module docstring); this avoids depending on the package
# __init__ staying lightweight as more adapters are added later.
from backend.agent_runtimes.base import AgentTask, RuntimeInvocationError
from backend.agent_runtimes.registry import (
    available_runtime_capabilities,
    available_runtimes,
    get_runtime,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("agent_server")

if os.name != "nt":
    import fcntl
    import pty
    import termios

_OPENCLI_BIN = os.environ.get("OPENCLI_BIN") or "opencli"


def _resolve_bin(mode: str) -> str:  # noqa: ARG001
    configured = _OPENCLI_BIN or "opencli"
    if os.path.isabs(configured) or os.path.dirname(configured):
        return configured
    if os.name == "nt" and not os.path.splitext(configured)[1]:
        for suffix in (".cmd", ".bat", ".exe", ".ps1"):
            resolved = shutil.which(f"{configured}{suffix}")
            if resolved:
                return resolved
    return shutil.which(configured) or configured


_DEFAULT_CDP = os.environ.get("OPENCLI_CDP_ENDPOINT", "http://localhost:19222")
_BROWSER_PROFILE_KIND = os.environ.get("OPENCLI_BROWSER_PROFILE_KIND", "authenticated")
_DAEMON_PORT = int(os.environ.get("OPENCLI_DAEMON_PORT", "19825"))
_AGENT_PORT = int(os.environ.get("AGENT_PORT", "19823"))
_CENTRAL_API_URL = os.environ.get("CENTRAL_API_URL", "").rstrip("/")
_AGENT_ADVERTISE_URL = os.environ.get("AGENT_ADVERTISE_URL", "")
_AGENT_MODE = os.environ.get("AGENT_MODE", "cdp")
# Deployment/startup type reported to center:
# "docker" (container) | "shell" (native process).
_AGENT_DEPLOY_TYPE = os.environ.get("AGENT_DEPLOY_TYPE", "docker")
# True when the image was built with INSTALL_CHROME=true (Chrome bundled inside container).
# False → Chrome runs on the host; localhost must be remapped to host.docker.internal.
_AGENT_HAS_CHROME = os.environ.get("AGENT_HAS_CHROME", "false").lower() == "true"
_AGENT_EVENT_ACK_TIMEOUT_SECONDS = 30.0
_RUNTIME_BUNDLE_MANIFEST = os.environ.get(
    "BROWSER_RUNTIME_BUNDLE_MANIFEST",
    "/opt/browser-runtime-bundles/opencli-default/1/manifest.json",
)


def _bundle_declared_runtimes() -> set[str]:
    try:
        with open(_RUNTIME_BUNDLE_MANIFEST, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, ValueError, TypeError):
        return set()
    if not isinstance(manifest, dict):
        return set()
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list):
        return set()
    declared: set[str] = set()
    for capability in capabilities:
        if not isinstance(capability, dict):
            continue
        runtime = capability.get("runtime", "opentabs")
        if isinstance(runtime, str):
            declared.add(runtime)
    return declared


def _available_agent_runtimes() -> list[str]:
    runtimes = list(available_runtimes())
    declared = _bundle_declared_runtimes()
    if "script-host" in declared and "script-host" not in runtimes:
        runtimes.append("script-host")
    return runtimes


_EDGE_RUNTIME_TASK_CONFIG_KEYS = frozenset(
    {
        "action",
        "agent_id",
        "base_delay",
        "breaker_threshold",
        "chrome",
        "input_wait_seconds",
        "local",
        "max_attempts",
        "model",
        "pack",
        "permission_mode",
        "plugin",
        "poll_interval",
        "provider",
        "response_timeout_seconds",
        "settle_seconds",
        "suggested_wait_seconds",
        "tab_id",
        "timeout_seconds",
    }
)


_AGENT_LABEL = os.environ.get("AGENT_LABEL", socket.gethostname())
# Registration mode:
#   http — LAN mode: agent POSTs its URL to center, center calls back via HTTP (default)
#   ws   — NAT/reverse-channel mode: agent opens WS to center, then
#          registers through the WS handshake.
#   off  — disable auto-registration entirely
_AGENT_REGISTER = os.environ.get("AGENT_REGISTER", "http").lower()
# opencli subprocess execution timeout in seconds
_OPENCLI_TIMEOUT = int(os.environ.get("OPENCLI_TIMEOUT", "120"))
# Outbound proxy for agent → center communication (optional)
_HTTP_PROXY = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
_HTTPS_PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
# Fleet auth token (ADR-0005): AGENT_API_TOKEN preferred, API_AUTH_TOKEN accepted
# as a fallback for nodes that share the center's environment.
_AGENT_API_TOKEN = os.environ.get("AGENT_API_TOKEN") or os.environ.get("API_AUTH_TOKEN") or ""
_OHMYOPENCLI_ROOT = os.environ.get("OHMYOPENCLI_ROOT", "/opt/ohmyopencli")
_ACTIVE_COLLECTS: dict[str, asyncio.subprocess.Process] = {}


def _auth_headers() -> dict[str, str]:
    """Bearer-auth header for center requests, or {} when no token is configured.

    Reads the module global at call time (rather than closing over it) so
    tests can monkeypatch backend.agent_server._AGENT_API_TOKEN directly.
    """
    if not _AGENT_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {_AGENT_API_TOKEN}"}


def _require_collect_auth(authorization: str | None) -> None:
    """Fail closed when the edge node has no inbound fleet secret."""
    import hmac

    expected = f"Bearer {_AGENT_API_TOKEN}" if _AGENT_API_TOKEN else ""
    if not expected or not authorization or not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid or unconfigured agent bearer token")


def _process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
    except (OSError, ProcessLookupError):
        proc.kill()
    await proc.wait()


async def _runtime_lineage(bin_path: str) -> dict[str, str]:
    """Measure the binaries/source used by this node; never echo declarations."""

    async def output(*argv: str, cwd: str | None = None) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd
            )
            stdout, _ = await proc.communicate()
            return stdout.decode(errors="replace").strip() if proc.returncode == 0 else ""
        except (OSError, ValueError):
            return ""

    repo_commit = await output("git", "rev-parse", "HEAD", cwd=_OHMYOPENCLI_ROOT)
    source_commit = await output(
        "git",
        "log",
        "-1",
        "--format=%H",
        "--",
        "adapters/official-site/observe.js",
        cwd=_OHMYOPENCLI_ROOT,
    )
    version_text = await output(bin_path, "--version")
    match = re.search(r"\d+\.\d+\.\d+(?:[-+][\w.-]+)?", version_text)
    return {
        "ohmyopencli_repo_commit": repo_commit,
        "capability_source_commit": source_commit,
        "opencli_version": match.group(0) if match else version_text,
    }


def _detect_advertise_url() -> str:
    """Auto-detect the IP this node would use to reach the center, then build agent URL."""
    if _AGENT_ADVERTISE_URL:
        return _AGENT_ADVERTISE_URL.rstrip("/")
    try:
        # Use center host to detect outbound IP
        target = urlparse(_CENTRAL_API_URL).hostname or "8.8.8.8"
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = socket.gethostbyname(socket.gethostname())
    return f"http://{ip}:{_AGENT_PORT}"


def _center_proxy() -> str | None:
    """Return the configured outbound proxy unless the center host is bypassed."""
    center_host = urlparse(_CENTRAL_API_URL).hostname
    if center_host and proxy_bypass(center_host):
        return None
    return _HTTPS_PROXY or _HTTP_PROXY or None


def _build_proxies() -> dict:
    """Build the legacy httpx proxy map for center registration."""
    proxy = _center_proxy()
    return {"https://": proxy, "http://": proxy} if proxy else {}


async def _register_with_center(advertise_url: str) -> None:
    """POST agent registration to the center API. Retries up to 5 times."""
    import httpx

    url = f"{_CENTRAL_API_URL}/api/v1/nodes/register"
    payload = {
        "agent_url": advertise_url,
        "mode": _AGENT_MODE,
        "node_type": _AGENT_DEPLOY_TYPE,
        "label": _AGENT_LABEL,
        "agent_protocol": "http",
        "runtimes": _available_agent_runtimes(),
        "runtime_capabilities": available_runtime_capabilities(),
        "profile_kind": _BROWSER_PROFILE_KIND,
    }
    proxies = _build_proxies()

    for attempt in range(1, 6):
        try:
            # httpx >= 0.28 removed 'proxies'; use 'proxy' (single URL) or mounts
            client_kwargs: dict = {"timeout": 10}
            headers = _auth_headers()
            if proxies:
                proxy_url = proxies.get("https://") or proxies.get("http://")
                try:
                    client_kwargs["proxy"] = proxy_url
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
                except TypeError:
                    # Older httpx: fall back to 'proxies'
                    client_kwargs.pop("proxy", None)
                    client_kwargs["proxies"] = proxies
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        resp = await client.post(url, json=payload, headers=headers)
                        resp.raise_for_status()
            else:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    resp.raise_for_status()
            logger.info("Registered with center %s as %s", _CENTRAL_API_URL, advertise_url)
            return
        except Exception as exc:
            wait = attempt * 3
            logger.warning(
                "Registration attempt %d failed: %s — retrying in %ds",
                attempt,
                exc,
                wait,
            )
            await asyncio.sleep(wait)
    logger.error("Could not register with center after 5 attempts")


async def _handle_ws_collect(ws, msg: dict) -> None:
    """Execute a collect task received over the WS channel and send back the result."""
    request_id = msg.get("request_id", "")
    req = CollectRequest(
        site=msg.get("site", ""),
        command=msg.get("command", ""),
        args=msg.get("args", {}),
        positional_args=msg.get("positional_args", []),
        format=msg.get("format", "json"),
        mode=msg.get("mode", "bridge"),
        execution_id=request_id,
    )
    try:
        result = await collect(req)
    except Exception as exc:
        logger.exception("WS collect error for request_id=%s: %s", request_id, exc)
        result = {"success": False, "items": [], "error": str(exc)}
    result["type"] = "result"
    result["request_id"] = request_id
    try:
        await ws.send(json.dumps(result))
    except Exception as exc:
        logger.error("WS: failed to send result for request_id=%s: %s", request_id, exc)


_PENDING_AGENT_EVENT_ACKS: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}


def _requires_durable_event_ack(event: dict[str, Any]) -> bool:
    evidence = event.get("evidence")
    return (
        event.get("type") == "evidence"
        and isinstance(evidence, dict)
        and evidence.get("kind") == "doubao.capture.pre_cleanup"
    )


def _resolve_ws_agent_event_ack(msg: dict[str, Any]) -> None:
    request_id = msg.get("request_id")
    event_id = msg.get("event_id")
    if not isinstance(request_id, str) or not isinstance(event_id, str):
        logger.warning("WS: malformed agent_event_ack frame")
        return
    future = _PENDING_AGENT_EVENT_ACKS.get((request_id, event_id))
    if future is None or future.done():
        logger.warning(
            "WS: unexpected agent_event_ack request_id=%s event_id=%s",
            request_id,
            event_id,
        )
        return
    future.set_result(msg)


async def _send_ws_agent_event(
    ws,
    *,
    request_id: str,
    event: dict[str, Any],
) -> None:
    frame: dict[str, Any] = {
        "type": "agent_event",
        "request_id": request_id,
        "event": event,
    }
    if not _requires_durable_event_ack(event):
        await ws.send(json.dumps(frame))
        return

    event_id = str(uuid.uuid4())
    frame.update({"event_id": event_id, "ack_required": True})
    key = (request_id, event_id)
    acknowledgement = asyncio.get_running_loop().create_future()
    _PENDING_AGENT_EVENT_ACKS[key] = acknowledgement
    try:
        await ws.send(json.dumps(frame))
        try:
            receipt = await asyncio.wait_for(
                acknowledgement,
                timeout=_AGENT_EVENT_ACK_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise RuntimeInvocationError(
                "control plane did not acknowledge durable capture before cleanup",
                error_type="AgentEventAcknowledgementTimeout",
            ) from exc
        if receipt.get("status") != "persisted":
            raise RuntimeInvocationError(
                "control plane rejected durable capture before cleanup",
                error_type="AgentEventAcknowledgementRejected",
            )
    finally:
        _PENDING_AGENT_EVENT_ACKS.pop(key, None)


async def _complete_agent_cleanup(operation):
    pending = asyncio.ensure_future(operation)
    while not pending.done():
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            continue
    return pending.result()


async def _handle_ws_agent_task(ws, msg: dict) -> None:
    """Execute an agent_task received over the WS channel: run the requested
    runtime adapter, streaming each RuntimeEvent back as an 'agent_event'
    frame, and finish with exactly one 'agent_result' frame carrying the
    terminal done/error event.

    Never raises out of this function — a single task crashing must not kill
    the WS receive loop (the caller fires this via asyncio.create_task).
    """
    request_id = msg.get("request_id", "")

    async def _send_result(result: dict) -> None:
        if msg.get("require_cancel_ack") is True:
            result = {
                **result,
                "task_id": request_id,
                "cleanup_complete": cleanup_complete,
            }
            _cache_agent_task_terminal(request_id, result)
        try:
            await ws.send(
                json.dumps(
                    {
                        "type": "agent_result",
                        "request_id": request_id,
                        "result": result,
                    }
                )
            )
        except Exception as exc:
            logger.error("WS: failed to send agent_result for request_id=%s: %s", request_id, exc)

    cleanup_complete = True
    runtime_type = msg.get("runtime", "")
    terminal_event: dict | None = None
    # Everything below is one outer try/except: get_runtime() lookup, adapter
    # construction of AgentTask, and the invoke() stream are all treated the
    # same way — any exception, of any type, must resolve the center's
    # pending future with an error result rather than propagate. The caller
    # fires this coroutine via asyncio.create_task, so an uncaught exception
    # here would otherwise vanish into an unretrieved task exception and the
    # center would hang until its own send_agent_task timeout.
    try:
        try:
            adapter = get_runtime(runtime_type)
        except ValueError as exc:
            logger.warning(
                "WS agent_task request_id=%s: unknown runtime %r: %s", request_id, runtime_type, exc
            )
            await _send_result(
                {
                    "type": "error",
                    "task_id": request_id,
                    "message": str(exc),
                    "error_type": "ValueError",
                }
            )
            return

        task = AgentTask(
            task_id=request_id,
            workflow=msg.get("workflow", ""),
            instructions=msg.get("instructions", ""),
            input=msg.get("input") or {},
            config=msg.get("config") or {},
            session_id=msg.get("session_id"),
            provider=msg.get("provider"),
            model=msg.get("model"),
            required_capabilities=tuple(msg.get("required_capabilities") or ()),
            permissions=msg.get("permissions") or {},
            budget=msg.get("budget") or {},
            evidence_requirements=tuple(msg.get("evidence_requirements") or ()),
        )
        config_errors = adapter.validate_config(task.config)
        if config_errors:
            raise RuntimeInvocationError("; ".join(config_errors), error_type="ConfigError")
        cleanup_complete = False
        readiness_task = asyncio.create_task(adapter.readiness(task.config))
        try:
            readiness = await asyncio.shield(readiness_task)
        except asyncio.CancelledError:
            readiness_task.cancel()
            try:
                await _complete_agent_cleanup(readiness_task)
            except asyncio.CancelledError:
                cleanup_complete = True
            else:
                cleanup_complete = True
            raise
        else:
            cleanup_complete = True
        if readiness.status != "ready":
            raise RuntimeInvocationError(
                readiness.reason or f"runtime {runtime_type!r} is not ready",
                error_type=readiness.reason_code or "RuntimeNotReady",
            )

        stream = adapter.invoke(task)
        cleanup_complete = False
        iteration_failed = False
        delivery_failed = False
        try:
            async for event in stream:
                terminal_event = event
                try:
                    await _send_ws_agent_event(
                        ws,
                        request_id=request_id,
                        event=event,
                    )
                except RuntimeInvocationError:
                    delivery_failed = True
                    raise
                except Exception as exc:
                    delivery_failed = True
                    logger.error(
                        "WS: failed to send agent_event for request_id=%s: %s",
                        request_id,
                        exc,
                    )
                    raise RuntimeInvocationError(
                        "failed to deliver agent event to the control plane",
                        error_type="AgentEventDeliveryError",
                    ) from exc
        except Exception:
            iteration_failed = True
            raise
        finally:
            await _complete_agent_cleanup(stream.aclose())
            cleanup_complete = not iteration_failed or delivery_failed
        if terminal_event is None or terminal_event.get("type") not in {"done", "error"}:
            # Contract violation (adapter yielded nothing) — still must resolve
            # the center's pending future rather than hang it until timeout.
            terminal_event = {
                "type": "error",
                "task_id": request_id,
                "message": f"runtime {runtime_type!r} adapter yielded no events",
                "error_type": "RuntimeInvocationError",
            }
        await _send_result(terminal_event)
        logger.info(
            "WS agent_task finished request_id=%s runtime=%s terminal=%s",
            request_id,
            runtime_type,
            terminal_event.get("type"),
        )
    except asyncio.CancelledError:
        result = terminal_event
        if result is None or result.get("type") not in {"done", "error"}:
            result = {
                "type": "error",
                "task_id": request_id,
                "message": "Agent execution stopped after runtime cleanup",
                "error_type": "CancelledError",
            }
        await _complete_agent_cleanup(_send_result(result))
    except RuntimeInvocationError as exc:
        logger.exception(
            "WS agent_task request_id=%s: adapter invocation error: %s",
            request_id,
            exc,
        )
        await _send_result(
            {
                "type": "error",
                "task_id": request_id,
                "message": str(exc),
                "error_type": exc.error_type or type(exc).__name__,
            }
        )
    except Exception as exc:
        logger.exception("WS agent_task request_id=%s: unexpected error: %s", request_id, exc)
        await _send_result(
            {
                "type": "error",
                "task_id": request_id,
                "message": str(exc),
                "error_type": type(exc).__name__,
            }
        )


_ACTIVE_AGENT_TASKS: dict[str, asyncio.Task[None]] = {}
_AGENT_TASK_TERMINAL_LIMIT = 1024
_AGENT_TASK_TERMINAL_TTL_SECONDS = 3600
_AGENT_TASK_TERMINALS: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()


def _prune_agent_task_terminals(now: float) -> None:
    while _AGENT_TASK_TERMINALS:
        expires_at, _result = next(iter(_AGENT_TASK_TERMINALS.values()))
        if expires_at > now:
            break
        _AGENT_TASK_TERMINALS.popitem(last=False)


def _cache_agent_task_terminal(task_id: str, result: dict) -> None:
    if (
        not isinstance(task_id, str)
        or not 0 < len(task_id) <= 256
        or result.get("type") not in {"done", "error"}
        or result.get("task_id") != task_id
        or not isinstance(result.get("cleanup_complete"), bool)
    ):
        return
    terminal = {
        "type": result["type"],
        "task_id": task_id,
        "cleanup_complete": result["cleanup_complete"],
    }
    error_type = result.get("error_type")
    if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", error_type):
        terminal["error_type"] = error_type
    now = monotonic()
    _prune_agent_task_terminals(now)
    _AGENT_TASK_TERMINALS.pop(task_id, None)
    _AGENT_TASK_TERMINALS[task_id] = (now + _AGENT_TASK_TERMINAL_TTL_SECONDS, terminal)
    while len(_AGENT_TASK_TERMINALS) > _AGENT_TASK_TERMINAL_LIMIT:
        _AGENT_TASK_TERMINALS.popitem(last=False)


async def _handle_ws_agent_task_status(ws, msg: dict) -> None:
    request_id = msg.get("request_id")
    task_id = msg.get("task_id")
    if any(
        not isinstance(value, str) or not 0 < len(value) <= 256 for value in (request_id, task_id)
    ):
        return
    _prune_agent_task_terminals(monotonic())
    cached = _AGENT_TASK_TERMINALS.get(task_id)
    active = _ACTIVE_AGENT_TASKS.get(task_id)
    if cached is not None and cached[1]["cleanup_complete"] is True:
        result = cached[1]
    elif active is not None and not active.done():
        result = {"status": "running"}
    elif cached is not None:
        result = cached[1]
    else:
        result = {"status": "unknown"}
    await ws.send(
        json.dumps(
            {
                "type": "agent_task_status_result",
                "request_id": request_id,
                "task_id": task_id,
                "result": result,
            }
        )
    )


def _forget_ws_agent_task(request_id: str, task: asyncio.Task[None]) -> None:
    if _ACTIVE_AGENT_TASKS.get(request_id) is task:
        _ACTIVE_AGENT_TASKS.pop(request_id, None)


def _start_ws_agent_task(ws, msg: dict) -> None:
    request_id = msg.get("request_id", "")
    _AGENT_TASK_TERMINALS.pop(request_id, None)
    task = asyncio.create_task(_handle_ws_agent_task(ws, msg))
    _ACTIVE_AGENT_TASKS[request_id] = task
    task.add_done_callback(lambda completed: _forget_ws_agent_task(request_id, completed))


_TERMINAL_FRAME_HEADER = struct.Struct("!B36s36sQ")
_TERMINAL_INPUT = 1
_TERMINAL_OUTPUT = 2
_TERMINAL_BROADCAST_ATTACHMENT = "00000000-0000-0000-0000-000000000000"
_TERMINAL_MAX_PAYLOAD = 64 * 1024
_TERMINAL_REPLAY_LIMIT = 4 * 1024 * 1024
_TERMINAL_RETAINED_LIMIT = 32
_TERMINAL_RUNTIME_ENV = {
    "codex": ("AGENT_CODEX_ISOLATED_RUNNER", "AGENT_CODEX_ALLOWED_ROOTS"),
    "omp": ("AGENT_OMP_ISOLATED_RUNNER", "AGENT_OMP_ALLOWED_ROOTS"),
}


@dataclass
class _NativeTerminalSession:
    session_id: str
    runtime: str
    process: asyncio.subprocess.Process
    master_fd: int
    status: str = "active"
    exit_code: int | None = None
    cleanup_complete: bool = False
    controller_id: str | None = None
    controller_attachment_id: str | None = None
    attachments: dict[str, str] = field(default_factory=dict)
    replay: bytearray = field(default_factory=bytearray)
    sequence: int = 0
    replay_truncated: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    reader_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[None] | None = None


_NATIVE_TERMINALS: dict[str, _NativeTerminalSession] = {}
_NATIVE_TERMINALS_LOCK = asyncio.Lock()
_STARTING_NATIVE_TERMINALS: set[str] = set()
_ACTIVE_TERMINAL_WS: Any | None = None


def _canonical_terminal_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("terminal identifier must be a UUID")
    parsed = str(uuid.UUID(value))
    if parsed != value:
        raise ValueError("terminal identifier must be canonical")
    return parsed


def _terminal_dimensions(msg: dict) -> tuple[int, int]:
    cols = msg.get("cols")
    rows = msg.get("rows")
    if (
        not isinstance(cols, int)
        or isinstance(cols, bool)
        or not isinstance(rows, int)
        or isinstance(rows, bool)
        or not 1 <= cols <= 1000
        or not 1 <= rows <= 1000
    ):
        raise ValueError("terminal dimensions are invalid")
    return cols, rows


def _terminal_runner_command(runtime: str, cwd: str, initial_input: str) -> list[str]:
    if os.name == "nt":
        raise RuntimeError("native PTY terminals require Linux or WSL")
    env_names = _TERMINAL_RUNTIME_ENV.get(runtime)
    if env_names is None:
        raise ValueError("unsupported terminal runtime")
    runner_value = os.environ.get(env_names[0], "").strip()
    runner = Path(runner_value)
    if not runner_value or not runner.is_absolute() or not runner.is_file():
        raise RuntimeError("isolated terminal runner is unavailable")
    try:
        allowed_values = json.loads(os.environ.get(env_names[1], ""))
    except json.JSONDecodeError as exc:
        raise RuntimeError("isolated terminal roots are invalid") from exc
    if not isinstance(allowed_values, list) or not allowed_values:
        raise RuntimeError("isolated terminal roots are unavailable")
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in allowed_values):
        raise RuntimeError("isolated terminal roots are invalid")
    working_directory = Path(cwd).resolve(strict=True)
    allowed_roots = [Path(value).resolve(strict=True) for value in allowed_values]
    if not any(
        working_directory == root or working_directory.is_relative_to(root)
        for root in allowed_roots
    ):
        raise PermissionError("terminal working directory is outside the reserved root")
    if runtime == "codex":
        arguments = [
            "--terminal",
            "--sandbox",
            "read-only",
            "--ask-for-approval",
            "never",
            "--cd",
            str(working_directory),
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
    else:
        arguments = [
            "--terminal",
            "--no-session",
            "--no-tools",
            "--no-lsp",
            "--no-extensions",
            "--no-skills",
            "--no-rules",
        ]
    return [str(runner), *arguments, initial_input]


def _set_terminal_size(master_fd: int, cols: int, rows: int) -> None:
    if os.name == "nt":
        raise RuntimeError("native PTY terminals require Linux or WSL")
    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def _write_terminal_fd(master_fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = await asyncio.to_thread(os.write, master_fd, payload[offset:])
        if written <= 0:
            raise OSError("terminal input write failed")
        offset += written


def _encode_terminal_output(
    session_id: str, attachment_id: str, sequence: int, payload: bytes
) -> bytes:
    return _TERMINAL_FRAME_HEADER.pack(
        _TERMINAL_OUTPUT,
        session_id.encode("ascii"),
        attachment_id.encode("ascii"),
        sequence,
    ) + payload


async def _send_terminal_event(
    session_id: str, event: dict[str, Any], *, attachment_id: str | None = None
) -> None:
    ws = _ACTIVE_TERMINAL_WS
    if ws is None:
        return
    frame: dict[str, Any] = {"type": "terminal_event", "session_id": session_id, "event": event}
    if attachment_id is not None:
        frame["attachment_id"] = attachment_id
    try:
        await ws.send(json.dumps(frame))
    except Exception:
        return


async def _send_terminal_output(
    session_id: str, attachment_id: str, sequence: int, payload: bytes
) -> None:
    ws = _ACTIVE_TERMINAL_WS
    if ws is None:
        return
    try:
        await ws.send(_encode_terminal_output(session_id, attachment_id, sequence, payload))
    except Exception:
        return


async def _read_native_terminal(session: _NativeTerminalSession) -> None:
    try:
        while True:
            try:
                payload = await asyncio.to_thread(os.read, session.master_fd, _TERMINAL_MAX_PAYLOAD)
            except OSError:
                break
            if not payload:
                break
            async with session.lock:
                session.sequence += len(payload)
                session.replay.extend(payload)
                if len(session.replay) > _TERMINAL_REPLAY_LIMIT:
                    overflow = len(session.replay) - _TERMINAL_REPLAY_LIMIT
                    del session.replay[:overflow]
                    session.replay_truncated = True
                should_send = bool(session.attachments)
                sequence = session.sequence
            if should_send:
                await _send_terminal_output(
                    session.session_id,
                    _TERMINAL_BROADCAST_ATTACHMENT,
                    sequence,
                    payload,
                )
    finally:
        try:
            os.close(session.master_fd)
        except OSError:
            pass


_TERMINAL_CLEANUP_POLL_ATTEMPTS = 40
_TERMINAL_CLEANUP_POLL_INTERVAL_SECONDS = 0.05


async def _cleanup_terminal_process_group(session: _NativeTerminalSession) -> bool:
    if os.name == "nt":
        return True
    try:
        os.killpg(session.process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    for _attempt in range(_TERMINAL_CLEANUP_POLL_ATTEMPTS):
        try:
            os.killpg(session.process.pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        await asyncio.sleep(_TERMINAL_CLEANUP_POLL_INTERVAL_SECONDS)
    return False


async def _wait_native_terminal(session: _NativeTerminalSession) -> None:
    exit_code = await session.process.wait()
    cleanup_complete = await _cleanup_terminal_process_group(session)
    if session.reader_task is not None:
        await asyncio.gather(session.reader_task, return_exceptions=True)
    async with session.lock:
        session.exit_code = exit_code
        session.cleanup_complete = cleanup_complete
        session.status = "exited"
    await _send_terminal_event(
        session.session_id,
        {
            "type": "exit",
            "status": "exited",
            "exit_code": exit_code,
            "cleanup_complete": cleanup_complete,
        },
    )
    async with _NATIVE_TERMINALS_LOCK:
        completed = [
            session_id
            for session_id, candidate in _NATIVE_TERMINALS.items()
            if candidate.status == "exited"
        ]
        for session_id in completed[:-_TERMINAL_RETAINED_LIMIT]:
            _NATIVE_TERMINALS.pop(session_id, None)


def _terminal_status(session: _NativeTerminalSession) -> dict[str, Any]:
    return {
        "status": session.status,
        "exit_code": session.exit_code,
        "cleanup_complete": session.cleanup_complete,
    }


async def _send_terminal_response(ws: Any, msg: dict, response: dict[str, Any]) -> None:
    request_id = msg.get("request_id")
    session_id = msg.get("session_id")
    if not isinstance(request_id, str) or not isinstance(session_id, str):
        return
    await ws.send(
        json.dumps(
            {
                "type": "terminal_response",
                "request_id": request_id,
                "session_id": session_id,
                "response": response,
            }
        )
    )


async def _handle_terminal_start(ws: Any, msg: dict) -> None:
    master_fd: int | None = None
    slave_fd: int | None = None
    process: asyncio.subprocess.Process | None = None
    session_id: str | None = None
    try:
        session_id = _canonical_terminal_uuid(msg.get("session_id"))
        async with _NATIVE_TERMINALS_LOCK:
            conflict = session_id in _NATIVE_TERMINALS or session_id in _STARTING_NATIVE_TERMINALS
            if not conflict:
                _STARTING_NATIVE_TERMINALS.add(session_id)
        if conflict:
            await _send_terminal_response(ws, msg, {"status": "conflict"})
            return
        runtime = msg.get("runtime")
        cwd = msg.get("cwd")
        initial_input = msg.get("initial_input")
        cols, rows = _terminal_dimensions(msg)
        if (
            runtime not in _TERMINAL_RUNTIME_ENV
            or not isinstance(cwd, str)
            or not isinstance(initial_input, str)
            or not initial_input.strip()
            or len(initial_input) > 20_000
            or "\x00" in initial_input
        ):
            raise ValueError("invalid terminal start request")
        command = _terminal_runner_command(runtime, cwd, initial_input)
        master_fd, slave_fd = pty.openpty()
        _set_terminal_size(master_fd, cols, rows)
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
        os.close(slave_fd)
        slave_fd = None
        session = _NativeTerminalSession(
            session_id=session_id,
            runtime=runtime,
            process=process,
            master_fd=master_fd,
        )
        async with _NATIVE_TERMINALS_LOCK:
            _NATIVE_TERMINALS[session_id] = session
        session.reader_task = asyncio.create_task(_read_native_terminal(session))
        session.wait_task = asyncio.create_task(_wait_native_terminal(session))
        await _send_terminal_response(ws, msg, _terminal_status(session))
    except BaseException as exc:
        logger.warning("Native terminal start failed: %s", type(exc).__name__)
        if process is not None:
            await _kill_process_tree(process)
        if session_id is not None:
            async with _NATIVE_TERMINALS_LOCK:
                candidate = _NATIVE_TERMINALS.get(session_id)
                if candidate is not None and candidate.process is process:
                    _NATIVE_TERMINALS.pop(session_id, None)
        for file_descriptor in (master_fd, slave_fd):
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        await _send_terminal_response(ws, msg, {"status": "failed"})
    finally:
        if session_id is not None:
            async with _NATIVE_TERMINALS_LOCK:
                _STARTING_NATIVE_TERMINALS.discard(session_id)


async def _handle_terminal_attach(ws: Any, msg: dict) -> None:
    try:
        session_id = _canonical_terminal_uuid(msg.get("session_id"))
        attachment_id = _canonical_terminal_uuid(msg.get("attachment_id"))
        controller_id = _canonical_terminal_uuid(msg.get("controller_id"))
        session = _NATIVE_TERMINALS.get(session_id)
        if session is None:
            await _send_terminal_response(ws, msg, {"status": "unknown"})
            return
        async with session.lock:
            old_attachment = session.controller_attachment_id
            takeover = msg.get("takeover") is True
            controls = session.controller_id in {None, controller_id} or takeover
            session.attachments[attachment_id] = controller_id
            if controls:
                session.controller_id = controller_id
                session.controller_attachment_id = attachment_id
            status = _terminal_status(session)
            replay_truncated = session.replay_truncated
            sequence = session.sequence
            snapshot = bytes(session.replay)
        await _send_terminal_response(ws, msg, {**status, "controls": controls})
        if takeover and old_attachment and old_attachment != attachment_id:
            await _send_terminal_event(
                session_id,
                {"type": "kicked"},
                attachment_id=old_attachment,
            )
        await _send_terminal_event(
            session_id,
            {
                "type": "attached" if controls else "locked",
                "controls": controls,
                **status,
                "replay_truncated": replay_truncated,
            },
            attachment_id=attachment_id,
        )
        await _send_terminal_event(
            session_id,
            {"type": "snapshot_begin", "sequence": sequence},
            attachment_id=attachment_id,
        )
        for offset in range(0, len(snapshot), _TERMINAL_MAX_PAYLOAD):
            await _send_terminal_output(
                session_id,
                attachment_id,
                sequence,
                snapshot[offset : offset + _TERMINAL_MAX_PAYLOAD],
            )
        await _send_terminal_event(
            session_id,
            {"type": "snapshot_end", "sequence": sequence},
            attachment_id=attachment_id,
        )
    except (ValueError, TypeError):
        await _send_terminal_response(ws, msg, {"status": "failed"})


async def _handle_terminal_takeover(ws: Any, msg: dict) -> None:
    try:
        session_id = _canonical_terminal_uuid(msg.get("session_id"))
        attachment_id = _canonical_terminal_uuid(msg.get("attachment_id"))
        controller_id = _canonical_terminal_uuid(msg.get("controller_id"))
        session = _NATIVE_TERMINALS.get(session_id)
        if session is None or session.attachments.get(attachment_id) != controller_id:
            await _send_terminal_response(ws, msg, {"status": "unknown"})
            return
        async with session.lock:
            old_attachment = session.controller_attachment_id
            session.controller_id = controller_id
            session.controller_attachment_id = attachment_id
            if old_attachment and old_attachment != attachment_id:
                await _send_terminal_event(
                    session_id, {"type": "kicked"}, attachment_id=old_attachment
                )
            await _send_terminal_event(
                session_id,
                {"type": "control_granted", "controls": True},
                attachment_id=attachment_id,
            )
            await _send_terminal_response(
                ws, msg, {**_terminal_status(session), "controls": True}
            )
    except (ValueError, TypeError):
        await _send_terminal_response(ws, msg, {"status": "failed"})


async def _handle_terminal_resize(msg: dict) -> None:
    try:
        session = _NATIVE_TERMINALS[_canonical_terminal_uuid(msg.get("session_id"))]
        attachment_id = _canonical_terminal_uuid(msg.get("attachment_id"))
        cols, rows = _terminal_dimensions(msg)
        async with session.lock:
            if session.controller_attachment_id != attachment_id or session.status != "active":
                return
            _set_terminal_size(session.master_fd, cols, rows)
    except (KeyError, ValueError, OSError, TypeError):
        return


async def _handle_terminal_detach(msg: dict) -> None:
    try:
        session = _NATIVE_TERMINALS[_canonical_terminal_uuid(msg.get("session_id"))]
        attachment_id = _canonical_terminal_uuid(msg.get("attachment_id"))
    except (KeyError, ValueError, TypeError):
        return
    async with session.lock:
        session.attachments.pop(attachment_id, None)
        if session.controller_attachment_id == attachment_id:
            session.controller_attachment_id = None


async def _handle_terminal_status(ws: Any, msg: dict) -> None:
    try:
        session = _NATIVE_TERMINALS.get(_canonical_terminal_uuid(msg.get("session_id")))
    except (ValueError, TypeError):
        session = None
    await _send_terminal_response(
        ws, msg, _terminal_status(session) if session is not None else {"status": "unknown"}
    )


async def _handle_terminal_stop(ws: Any, msg: dict) -> None:
    try:
        session = _NATIVE_TERMINALS.get(_canonical_terminal_uuid(msg.get("session_id")))
    except (ValueError, TypeError):
        session = None
    if session is None:
        await _send_terminal_response(ws, msg, {"status": "unknown"})
        return
    async with session.lock:
        if session.status == "active":
            session.status = "stopping"
    if session.process.returncode is None:
        await _kill_process_tree(session.process)
    if session.wait_task is not None:
        await asyncio.gather(session.wait_task, return_exceptions=True)
    await _send_terminal_response(ws, msg, _terminal_status(session))


async def _handle_terminal_input(frame: bytes) -> None:
    if len(frame) <= _TERMINAL_FRAME_HEADER.size:
        return
    try:
        kind, session_raw, attachment_raw, _sequence = _TERMINAL_FRAME_HEADER.unpack_from(frame)
        session_id = _canonical_terminal_uuid(session_raw.decode("ascii"))
        attachment_id = _canonical_terminal_uuid(attachment_raw.decode("ascii"))
    except (ValueError, UnicodeDecodeError, struct.error):
        return
    payload = frame[_TERMINAL_FRAME_HEADER.size :]
    if kind != _TERMINAL_INPUT or not 0 < len(payload) <= _TERMINAL_MAX_PAYLOAD:
        return
    session = _NATIVE_TERMINALS.get(session_id)
    if session is None:
        return
    async with session.lock:
        if session.controller_attachment_id != attachment_id or session.status != "active":
            return
        try:
            await _write_terminal_fd(session.master_fd, payload)
        except OSError:
            return


async def _register_via_ws(advertise_url: str) -> None:
    """Initiate persistent reverse WebSocket to center and handle collect tasks.

    Keeps reconnecting with exponential back-off so transient outages are
    recovered automatically.  The loop exits only when the process shuts down.
    """
    global _ACTIVE_TERMINAL_WS

    import websockets  # requires: pip install websockets

    ws_url = (
        _CENTRAL_API_URL.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        + "/api/v1/nodes/ws"
    )
    _proxy = _HTTPS_PROXY or _HTTP_PROXY or None
    # Computed once (not per reconnect attempt): runtime availability includes
    # fixed-binary compatibility probes and does not change without a restart.
    runtimes = _available_agent_runtimes()
    runtime_capabilities = available_runtime_capabilities()
    for runtime in runtimes:
        runtime_capabilities.setdefault(runtime, [])
    register_payload = json.dumps(
        {
            "type": "register",
            "agent_url": advertise_url,
            "mode": _AGENT_MODE,
            "node_type": _AGENT_DEPLOY_TYPE,
            "label": _AGENT_LABEL,
            "runtimes": runtimes,
            "runtime_capabilities": runtime_capabilities,
            "profile_kind": _BROWSER_PROFILE_KIND,
        }
    )

    attempt = 0
    while True:
        attempt += 1
        try:
            logger.info("WS connecting to center %s (attempt %d)", ws_url, attempt)
            connect_kwargs: dict = {"ping_interval": 30, "ping_timeout": 10}
            if _proxy:
                connect_kwargs["proxy"] = _proxy
            headers = _auth_headers()
            if headers:
                try:
                    # websockets >= 14 renamed extra_headers -> additional_headers.
                    connector = websockets.connect(
                        ws_url, additional_headers=headers, **connect_kwargs
                    )
                except TypeError:
                    connector = websockets.connect(ws_url, extra_headers=headers, **connect_kwargs)
            else:
                connector = websockets.connect(ws_url, **connect_kwargs)
            async with connector as ws:
                attempt = 0  # reset on successful connect
                await ws.send(register_payload)

                ack_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                ack = json.loads(ack_raw)
                if ack.get("type") != "registered":
                    raise RuntimeError(f"Unexpected handshake response: {ack}")
                logger.info("WS registered with center as %s", advertise_url)
                _ACTIVE_TERMINAL_WS = ws

                # Main receive loop
                async for raw_msg in ws:
                    if isinstance(raw_msg, bytes):
                        await _handle_terminal_input(raw_msg)
                        continue
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        logger.warning("WS: invalid JSON from center: %r", raw_msg[:200])
                        continue
                    msg_type = msg.get("type")
                    if msg_type == "collect":
                        asyncio.create_task(_handle_ws_collect(ws, msg))
                    elif msg_type == "agent_task":
                        _start_ws_agent_task(ws, msg)
                    elif msg_type == "agent_task_status":
                        await _handle_ws_agent_task_status(ws, msg)
                    elif msg_type == "agent_event_ack":
                        _resolve_ws_agent_event_ack(msg)
                    elif msg_type == "terminal_start":
                        asyncio.create_task(_handle_terminal_start(ws, msg))
                    elif msg_type == "terminal_attach":
                        asyncio.create_task(_handle_terminal_attach(ws, msg))
                    elif msg_type == "terminal_takeover":
                        asyncio.create_task(_handle_terminal_takeover(ws, msg))
                    elif msg_type == "terminal_resize":
                        asyncio.create_task(_handle_terminal_resize(msg))
                    elif msg_type == "terminal_detach":
                        asyncio.create_task(_handle_terminal_detach(msg))
                    elif msg_type == "terminal_status":
                        asyncio.create_task(_handle_terminal_status(ws, msg))
                    elif msg_type == "terminal_stop":
                        asyncio.create_task(_handle_terminal_stop(ws, msg))
                    elif msg_type == "cancel":
                        request_id = msg.get("request_id", "")
                        proc = _ACTIVE_COLLECTS.get(request_id)
                        if proc is not None:
                            asyncio.create_task(_kill_process_tree(proc))
                        agent_task = _ACTIVE_AGENT_TASKS.get(request_id)
                        if agent_task is not None:
                            agent_task.cancel()
                    elif msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))
                    elif msg_type == "pong":
                        pass
                    else:
                        logger.debug("WS: unknown message type %r", msg_type)

        except asyncio.CancelledError:
            logger.info("WS registration task cancelled — shutting down")
            return
        except Exception as exc:
            wait = min(attempt * 3, 60)
            logger.warning(
                "WS connection lost (attempt %d): %s — reconnecting in %ds", attempt, exc, wait
            )
            await asyncio.sleep(wait)
        finally:
            if _ACTIVE_TERMINAL_WS is locals().get("ws"):
                _ACTIVE_TERMINAL_WS = None
                for session in _NATIVE_TERMINALS.values():
                    session.attachments.clear()
                    session.controller_attachment_id = None
            for task in tuple(_ACTIVE_AGENT_TASKS.values()):
                task.cancel()


_ws_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ws_task
    if not _CENTRAL_API_URL or _AGENT_REGISTER == "off":
        logger.info(
            "Auto-registration disabled (CENTRAL_API_URL=%r AGENT_REGISTER=%s)",
            _CENTRAL_API_URL or "",
            _AGENT_REGISTER,
        )
    elif _AGENT_REGISTER == "http":
        advertise_url = _detect_advertise_url()
        logger.info(
            "LAN registration: advertise_url=%s → center=%s",
            advertise_url,
            _CENTRAL_API_URL,
        )
        asyncio.get_event_loop().create_task(_register_with_center(advertise_url))
    elif _AGENT_REGISTER == "ws":
        advertise_url = _detect_advertise_url()
        logger.info(
            "WS registration: advertise_url=%s → center=%s",
            advertise_url,
            _CENTRAL_API_URL,
        )
        _ws_task = asyncio.get_event_loop().create_task(_register_via_ws(advertise_url))
    yield
    if _ws_task and not _ws_task.done():
        _ws_task.cancel()
        try:
            await _ws_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="OpenCLI Agent Server", version="0.4.1", lifespan=lifespan)


class CollectRequest(BaseModel):
    site: str
    command: str
    args: dict[str, Any] = {}
    # Values passed as positional CLI arguments (no --key prefix), inserted
    # right after [site] [command] and before any named --options.
    positional_args: list[str] = []
    format: str = "json"
    mode: str = "bridge"
    # CDP endpoint override; falls back to OPENCLI_CDP_ENDPOINT env var
    cdp_endpoint: str = ""
    execution_id: str = ""


@app.get("/health")
def health() -> dict:
    bin_path = _resolve_bin(_AGENT_MODE)
    return {
        "status": "ok",
        "opencli_bin": bin_path,
        "opencli_bin_exists": shutil.which(bin_path) is not None or os.path.isfile(bin_path),
        "default_cdp_endpoint": _DEFAULT_CDP,
    }


@app.post("/runtime/invoke")
async def invoke_runtime_http(
    req: RuntimeInvokeRequest, authorization: str | None = Header(default=None)
) -> dict:
    _require_collect_auth(authorization)
    if req.runtime in {"codex", "omp"}:
        raise HTTPException(
            status_code=403,
            detail=(
                f"{req.runtime.title()} runtime is only available through controller WS dispatch"
            ),
        )
    if req.runtime not in _bundle_declared_runtimes():
        raise HTTPException(
            status_code=403,
            detail=f"runtime {req.runtime!r} is not declared by the installed bundle",
        )
    unsafe_keys = sorted(set(req.config) - _EDGE_RUNTIME_TASK_CONFIG_KEYS)
    if unsafe_keys:
        raise HTTPException(
            status_code=400,
            detail="edge runtime config cannot override: " + ", ".join(unsafe_keys),
        )
    return await invoke_runtime(str(uuid.uuid4()), req, cdp_endpoint=_DEFAULT_CDP)


async def collect(req: CollectRequest) -> dict:
    cdp_ep = req.cdp_endpoint.strip() or _DEFAULT_CDP
    mode = req.mode

    bin_path = _resolve_bin(mode)

    cmd = [bin_path, req.site, req.command]
    cmd.extend([str(v) for v in req.positional_args])
    for k, v in req.args.items():
        cmd.extend([f"--{k}", str(v)])
    cmd.extend(["-f", req.format])

    env = os.environ.copy()
    if mode == "bridge":
        hostname = urlparse(cdp_ep).hostname or "localhost"
        # When running in a Docker agent WITHOUT bundled Chrome, localhost refers
        # to the container itself — remap to host.docker.internal so opencli
        # reaches the Chrome bridge daemon on the host machine.
        # Agents built with INSTALL_CHROME=true have their own Chrome and should
        # keep localhost as-is.
        if (
            _AGENT_DEPLOY_TYPE == "docker"
            and not _AGENT_HAS_CHROME
            and hostname in ("localhost", "127.0.0.1", "::1")
        ):
            hostname = "host.docker.internal"
        env.pop("OPENCLI_CDP_ENDPOINT", None)
        env["OPENCLI_DAEMON_HOST"] = hostname
        env["OPENCLI_DAEMON_PORT"] = str(_DAEMON_PORT)
        logger.info("bridge | cmd=%s daemon=%s:%s", " ".join(cmd), hostname, _DAEMON_PORT)
    else:
        # Same logic for CDP: remap localhost to host.docker.internal only when
        # running in Docker without bundled Chrome.
        if _AGENT_DEPLOY_TYPE == "docker" and not _AGENT_HAS_CHROME:
            cdp_ep = re.sub(r"(localhost|127\.0\.0\.1)", "host.docker.internal", cdp_ep)
        env["OPENCLI_CDP_ENDPOINT"] = cdp_ep
        logger.info("cdp | cmd=%s cdp=%s", " ".join(cmd), cdp_ep)

    pre_tab_ids: set[str] = set()
    if mode == "cdp":
        pre_tab_ids = await snapshot_tab_ids(cdp_ep)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            **_process_group_kwargs(),
        )
        if req.execution_id:
            _ACTIVE_COLLECTS[req.execution_id] = proc
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_OPENCLI_TIMEOUT)
        rc = proc.returncode
    except TimeoutError:
        logger.error("timeout | cmd=%s", " ".join(cmd))
        if proc:
            await _kill_process_tree(proc)
        if mode == "cdp":
            await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)
        return {"success": False, "items": [], "error": "opencli timed out after 120s"}
    except Exception as exc:
        logger.exception("subprocess error | %s", exc)
        if mode == "cdp":
            await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)
        return {"success": False, "items": [], "error": str(exc)}
    finally:
        if req.execution_id:
            _ACTIVE_COLLECTS.pop(req.execution_id, None)

    if mode == "cdp":
        await cleanup_cdp_tabs(cdp_ep, pre_tab_ids)

    stderr_str = stderr.decode().strip()
    stdout_str = stdout.decode()

    if stderr_str:
        logger.warning("stderr | %s", stderr_str[:500])
    if rc != 0:
        logger.error("exit=%d | %s", rc, stderr_str[:500])
        return {"success": False, "items": [], "error": f"opencli exit {rc}: {stderr_str}"}

    try:
        items = parse_output(stdout_str, req.format)
    except Exception as exc:
        logger.error("parse error | %s", exc)
        return {"success": False, "items": [], "error": f"parse error: {exc}"}

    logger.info("done | site=%s cmd=%s items=%d", req.site, req.command, len(items))
    trace_match = re.search(r"OpenCLI trace artifact:\s*([^\r\n]+)", stderr_str)
    metadata: dict[str, Any] = {"runtime": await _runtime_lineage(bin_path)}
    if trace_match:
        metadata["trace_artifact"] = trace_match.group(1)
    return {
        "success": True,
        "items": items,
        "error": None,
        "runtime": metadata["runtime"],
        "trace_artifact": metadata.get("trace_artifact"),
        "metadata": metadata,
    }


@app.post("/collect")
async def collect_http(
    req: CollectRequest, authorization: str | None = Header(default=None)
) -> dict:
    _require_collect_auth(authorization)
    return await collect(req)


@app.post("/collect/{execution_id}/cancel")
async def cancel_collect(
    execution_id: str, authorization: str | None = Header(default=None)
) -> dict:
    _require_collect_auth(authorization)
    proc = _ACTIVE_COLLECTS.get(execution_id)
    if proc is not None:
        await _kill_process_tree(proc)
    return {"cancelled": True, "execution_id": execution_id}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=_AGENT_PORT, log_level="info")
