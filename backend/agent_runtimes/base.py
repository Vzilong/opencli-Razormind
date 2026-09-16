"""Contract for pluggable agent runtimes (LangGraph, VoltAgent, pi, ...).

Mirrors ``backend/channels/base.py``'s split: a frozen ``Capabilities`` dataclass
the caller branches on (never ``isinstance``), a small task/result value-object
pair, and an ``ABC`` adapters implement. The three target frameworks
(LangGraph, VoltAgent, pi) span two languages and three transport shapes, so
the abstraction has to be a *process/protocol* contract, not a Python import.
Each adapter owns exactly one framework's native stream and translates it into
the closed event set below; nothing outside the adapter ever sees a
framework-native shape.

Event design note: normalize the *protocol*, not the output. The event set is
intentionally tiny and closed —
``EVENT_TYPES`` — so adapters cannot invent new shapes ad hoc. Use the
``event_*`` helper constructors below rather than hand-building event dicts;
that is what prevents typos in ``type`` strings and missing ``task_id`` fields
from ever reaching a caller.
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

RUNTIME_CAPABILITY_STREAMING = "streaming"
RUNTIME_CAPABILITY_RESUMABLE = "resumable"
RUNTIME_CAPABILITY_PERSISTENT_SESSION = "persistent_session"

@dataclass(frozen=True)
class RuntimeReadiness:
    """Safe, typed evidence used before dispatching a runtime task.

    Readiness is intentionally separate from ``RuntimeCapabilities``: a
    registered adapter may be known to the node while its executable or
    working directory is unavailable.  Implementations MUST keep secrets out
    of this structure; it is suitable for Fleet diagnostics and wire output.
    """

    runtime: str
    capability_id: str
    status: Literal["ready", "blocked"]
    binary_present: bool
    version: str | None = None
    permitted_project_root: str | None = None
    working_directory: str | None = None
    reason_code: str | None = None
    reason: str | None = None

#: Closed tagged-union of runtime event types. Adapters MUST NOT emit any
#: ``type`` outside this set. Artifact, evidence, and audit events use the
#: same envelope across every runtime so callers never parse native shapes.
EVENT_TYPES: frozenset[str] = frozenset(
    {
        "started",
        "text",
        "tool_call",
        "tool_result",
        "state",
        "artifact",
        "evidence",
        "audit",
        "done",
        "error",
    }
)


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Stable capability advertisement used by Fleet selection."""

    transport: str  # "stdio" | "http" | "inprocess"
    streaming: bool = True
    resume_by_id: bool = False
    checkpoint: str = "none"  # none | memory | sqlite | postgres
    concurrent_sessions: bool = True
    features: frozenset[str] = frozenset()

    def names(self) -> frozenset[str]:
        names = set(self.features)
        if self.streaming:
            names.add(RUNTIME_CAPABILITY_STREAMING)
        if self.resume_by_id:
            names.add(RUNTIME_CAPABILITY_RESUMABLE)
        if self.checkpoint != "none":
            names.add(RUNTIME_CAPABILITY_PERSISTENT_SESSION)
        return frozenset(names)

@dataclass
class AgentTask:
    """Runtime-neutral task handed to one selected ``RuntimeAdapter``."""

    task_id: str
    workflow: str
    instructions: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    # Only task-scoped policy belongs here. Binary paths, provider credentials,
    # launch environment, and other Fleet-owned settings stay on the edge.
    config: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    required_capabilities: tuple[str, ...] = ()
    permissions: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)
    evidence_requirements: tuple[str, ...] = ()


# ── Event constructors ───────────────────────────────────────────────────────
# Plain dicts (not a dataclass) so events serialize to JSON for the ws wire
# protocol with zero translation step — but callers should always go through
# these constructors, never build the dict literal by hand, so a typo'd key
# or a `type` outside EVENT_TYPES is impossible.


def event_started(task_id: str) -> dict[str, Any]:
    return {"type": "started", "task_id": task_id}


def event_text(task_id: str, text: str) -> dict[str, Any]:
    return {"type": "text", "task_id": task_id, "text": text}


def event_tool_call(
    task_id: str, name: str, args: dict[str, Any] | None = None, call_id: str | None = None
) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "task_id": task_id,
        "name": name,
        "args": args or {},
        "call_id": call_id,
    }


def event_tool_result(
    task_id: str,
    name: str,
    result: Any = None,
    call_id: str | None = None,
    is_error: bool = False,
) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "task_id": task_id,
        "name": name,
        "result": result,
        "call_id": call_id,
        "is_error": is_error,
    }


def event_state(task_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return {"type": "state", "task_id": task_id, "state": state}


def event_artifact(task_id: str, artifact: dict[str, Any]) -> dict[str, Any]:
    return {"type": "artifact", "task_id": task_id, "artifact": artifact}


def event_evidence(task_id: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"type": "evidence", "task_id": task_id, "evidence": evidence}


def event_audit(task_id: str, audit: dict[str, Any]) -> dict[str, Any]:
    return {"type": "audit", "task_id": task_id, "audit": audit}


def event_done(task_id: str, result: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"type": "done", "task_id": task_id, "result": result or {}}


def event_error(task_id: str, message: str, error_type: str | None = None) -> dict[str, Any]:
    """``error_type`` mirrors ``channels.base.ChannelResult.error_type``: the
    failing exception's class name (e.g. "TimeoutError"), letting callers
    build a retryable-vs-permanent taxonomy without re-parsing free text."""
    return {"type": "error", "task_id": task_id, "message": message, "error_type": error_type}


class RuntimeInvocationError(Exception):
    """Raised by an adapter when a run cannot be started or fails outside the
    normal event stream (mirrors ``channels.base.ChannelFetchError``).

    ``error_type`` carries an explicit retry-classification hint for callers
    that already know the fault category but don't want to lose it behind a
    generic wrapper exception."""

    def __init__(self, message: str, error_type: str | None = None) -> None:
        super().__init__(message)
        self.error_type = error_type


class RuntimeAdapter(ABC):
    """Base class for all agent-runtime adapters."""

    runtime_type: str
    #: What this adapter can do. A subclass overrides this with its own
    #: RuntimeCapabilities(...); there is no safe default transport, so
    #: unlike Capabilities this has no class-level fallback value.
    capabilities: RuntimeCapabilities

    @abstractmethod
    async def invoke(self, task: AgentTask) -> AsyncIterator[dict[str, Any]]:
        """Run one agent task, yielding RuntimeEvents (see EVENT_TYPES) as
        they occur. MUST yield exactly one terminal event — either a single
        ``done`` or a single ``error`` — as the last item, and MUST NOT yield
        anything after it."""
        raise NotImplementedError
        yield  # pragma: no cover - makes this an async generator for type checkers

    @abstractmethod
    async def health(self) -> bool:
        """Cheap liveness check for this runtime (binary present, sidecar
        reachable, ...). Does not run a task."""

    async def readiness(self, config: dict[str, Any] | None = None) -> RuntimeReadiness:
        """Return safe pre-dispatch evidence for this runtime.

        Adapters with richer checks override this method.  The default keeps
        existing adapters source-compatible while giving callers a typed
        readiness shape.
        """
        ready = await self.health()
        return RuntimeReadiness(
            runtime=self.runtime_type,
            capability_id=f"runtime.{self.runtime_type}",
            status="ready" if ready else "blocked",
            binary_present=ready,
            reason_code=None if ready else "unavailable",
            reason=None if ready else f"runtime {self.runtime_type!r} is unavailable",
        )


    @abstractmethod
    def validate_config(self, config: dict[str, Any]) -> list[str]:
        """Validate an AgentTask.config dict; return list of error strings
        (empty = valid)."""

    async def bootstrap(self) -> None:
        """One-time env/config setup (e.g. writing skill files, provisioning
        a session directory). Default is a no-op; adapters override as
        needed. Mirrors OpenAlice's ``bootstrap()`` pattern."""
        return None

#: Keys shared by local stdio subprocess adapters.  The binding schema keeps
#: task configuration to ``timeout_seconds``; these additional keys remain
#: useful for direct edge invocations and are validated here before spawning.
_COMMON_CONFIG_KEYS: tuple[str, ...] = ("binary", "cwd", "env", "args", "timeout_seconds")


def validate_common_config(
    config: dict[str, Any],
    *,
    default_binary: str = "pi",
) -> list[str]:
    """Validate the common subprocess configuration surface.

    Runtime-specific adapters call this helper before validating their own
    options.  Environment variables are deliberately restricted to strings:
    ``asyncio.create_subprocess_exec`` ultimately hands them to the OS and
    accepting arbitrary JSON values would produce a late, opaque ``TypeError``
    during process creation.
    """
    errors: list[str] = []
    binary = config.get("binary", default_binary)
    if not isinstance(binary, str) or not binary.strip():
        errors.append("'binary' must be a non-empty string")
    elif "\x00" in binary:
        errors.append("'binary' must not contain NUL bytes")

    if "cwd" in config and config["cwd"] is not None:
        cwd = config["cwd"]
        if not isinstance(cwd, str) or not cwd.strip():
            errors.append("'cwd' must be a non-empty string when provided")
        elif "\x00" in cwd:
            errors.append("'cwd' must not contain NUL bytes")

    if "env" in config and config["env"] is not None:
        env = config["env"]
        if not isinstance(env, dict):
            errors.append("'env' must be a dict when provided")
        elif not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
            errors.append("'env' must contain only string keys and values")

    if "args" in config and config["args"] is not None:
        args = config["args"]
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            errors.append("'args' must be a list of strings when provided")

    if "timeout_seconds" in config and config["timeout_seconds"] is not None:
        timeout = config["timeout_seconds"]
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("'timeout_seconds' must be a positive number when provided")
    return errors
