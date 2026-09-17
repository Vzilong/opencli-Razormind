from unittest.mock import AsyncMock

import pytest

from backend import ws_agent_manager
from backend.config import get_settings


def complete_binding(**values):
    return {
        "workspace_id": "authorized-workspace",
        "cwd": "/isolated/work",
        "agent_url": "http://native-only:19833",
        "runtime_id": "codex",
        **values,
    }


@pytest.mark.parametrize(
    "task",
    [
        {"runtime": "codex", "workflow": "generic"},
        {"runtime": "omp", "workflow": "operator_chat", "native_chat_authorized": True},
        {"runtime": "pi", "workflow": "generic"},
    ],
)
async def test_generic_dispatch_cannot_consume_native_chat_node(monkeypatch, task):
    address = "http://native-only:19833"
    monkeypatch.setattr(
        get_settings(), "native_chat_bindings", [complete_binding(agent_url=address)]
    )
    connection = AsyncMock()
    monkeypatch.setitem(ws_agent_manager._connections, address, connection)
    with pytest.raises(PermissionError, match="reserved"):
        await ws_agent_manager.send_agent_task(address, task, AsyncMock())
    connection.send_json.assert_not_awaited()


@pytest.mark.parametrize(
    "bindings,authorized,ack,runtime",
    [
        ([], True, True, "codex"),
        ([{"agent_url": "http://native-only:19833", "runtime_id": "codex"}], True, True, "omp"),
        ([{"agent_url": "http://native-only:19833", "runtime_id": "codex"}], True, False, "codex"),
        ([], False, True, "codex"),
    ],
)
async def test_native_dispatch_flag_requires_matching_binding_and_cleanup(
    monkeypatch, bindings, authorized, ack, runtime
):
    monkeypatch.setattr(
        get_settings(), "native_chat_bindings", [complete_binding(**entry) for entry in bindings]
    )
    with pytest.raises(PermissionError):
        await ws_agent_manager.send_agent_task(
            "http://native-only:19833",
            {"runtime": runtime, "workflow": "operator_chat"},
            AsyncMock(),
            require_cancel_ack=ack,
            native_chat_authorized=authorized,
        )


@pytest.mark.parametrize(
    "configured_url", ["http://native-only:19833/", " http://native-only:19833/ "]
)
async def test_collect_cannot_bypass_normalized_native_node_reservation(
    monkeypatch, configured_url
):
    monkeypatch.setattr(
        get_settings(), "native_chat_bindings", [complete_binding(agent_url=configured_url)]
    )
    with pytest.raises(PermissionError, match="reserved"):
        await ws_agent_manager.dispatch_collect(
            "http://native-only:19833", "site", "command", {}, [], "json", "cdp"
        )


async def test_invalid_reservation_configuration_fails_closed(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "native_chat_bindings", [{"agent_url": "http://native-only:19833"}]
    )
    with pytest.raises(PermissionError, match="invalid"):
        await ws_agent_manager.send_agent_task("http://other:19833", {"runtime": "pi"}, AsyncMock())


async def test_unreserved_node_keeps_existing_dispatch_behavior(monkeypatch):
    monkeypatch.setattr(get_settings(), "native_chat_bindings", [complete_binding()])
    with pytest.raises(RuntimeError, match="No active WS connection"):
        await ws_agent_manager.send_agent_task("http://other:19833", {"runtime": "pi"}, AsyncMock())
