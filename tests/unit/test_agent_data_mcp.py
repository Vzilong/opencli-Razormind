"""MCP adapter and caller-identity propagation checks."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp import Client

from backend import mcp_server


def _settings(*, fleet="fleet-secret", caller="caller-secret", url="http://api:8130"):
    return SimpleNamespace(
        api_auth_token=fleet,
        opencli_mcp_caller_token=caller,
        opencli_admin_api_url=url,
    )


def test_stdio_auth_separates_fleet_access_from_explicit_caller(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _settings())

    assert mcp_server._api_base_url() == "http://api:8130"
    assert mcp_server._auth_headers() == {
        "X-API-Token": "fleet-secret",
        "Authorization": "Bearer caller-secret",
    }


def test_http_auth_propagates_request_caller_and_never_promotes_fleet_token(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_settings", lambda: _settings())
    user_context = SimpleNamespace(headers={"authorization": "Bearer user-session"})
    fleet_context = SimpleNamespace(headers={"authorization": "Bearer fleet-secret"})

    assert mcp_server._auth_headers(user_context) == {
        "X-API-Token": "fleet-secret",
        "Authorization": "Bearer user-session",
    }
    assert mcp_server._auth_headers(fleet_context) == {"X-API-Token": "fleet-secret"}
    assert mcp_server._auth_headers(SimpleNamespace(headers={})) == {"X-API-Token": "fleet-secret"}


@pytest.mark.asyncio
async def test_existing_mcp_tools_forward_http_caller_context(monkeypatch):
    request = AsyncMock(return_value={"success": True, "data": []})
    monkeypatch.setattr(mcp_server, "_request", request)
    http_context = SimpleNamespace(headers={"authorization": "Bearer user-session"})

    await mcp_server.list_sources(ctx=http_context)
    request.assert_awaited_once_with(
        "GET",
        "/api/v1/sources",
        context=http_context,
        params={"page": 1, "limit": 20},
    )

    request.reset_mock()
    await mcp_server.run_published_workflow(
        "workspace-1",
        "project-1",
        "workflow-1",
        {"topic": "search"},
        "retry-1",
        ctx=http_context,
    )
    request.assert_awaited_once_with(
        "POST",
        "/api/v1/workspaces/workspace-1/projects/project-1/workflows/workflow-1/runs",
        context=http_context,
        headers={"Idempotency-Key": "retry-1"},
        json={
            "inputs": {"topic": "search"},
            "response_mode": "async",
            "user": "mcp-client",
        },
    )


@pytest.mark.asyncio
async def test_downstream_tools_are_discoverable_and_annotated():
    async with Client(mcp_server.mcp, mode=mcp_server.MCP_PROTOCOL_VERSION) as client:
        result = await client.list_tools()

    tools = {tool.name: tool for tool in result.tools}
    expected = {
        "query_project_records",
        "query_project_context",
        "get_project_data_capabilities",
        "get_project_research_readiness",
        "start_project_research",
        "list_project_research_runs",
        "get_project_research_run",
    }
    assert expected <= tools.keys()
    assert tools["query_project_records"].annotations.read_only_hint is True
    assert tools["query_project_context"].annotations.read_only_hint is True
    assert tools["start_project_research"].annotations.idempotent_hint is True
    assert "ctx" not in tools["query_project_records"].input_schema["properties"]
    assert "ctx" not in tools["list_sources"].input_schema["properties"]


@pytest.mark.asyncio
async def test_project_data_tools_reuse_rest_contract(monkeypatch):
    request = AsyncMock(return_value={"success": True, "data": {"items": [], "total": 0}})
    monkeypatch.setattr(mcp_server, "_request", request)

    await mcp_server.query_project_records("workspace-1", "project-1", "search", 12)
    request.assert_awaited_once_with(
        "GET",
        "/api/v1/workspaces/workspace-1/projects/project-1/agent-data/records",
        params={"q": "search", "limit": 12},
    )

    request.reset_mock()
    context_payload = {
        "success": True,
        "data": {
            "query": "auth",
            "matches": [
                {
                    "id": "research:run-1:version-1:source:source-1",
                    "text": "Captured evidence (not a model-analyzed finding): auth evidence",
                    "source": {
                        "type": "research",
                        "run_id": "run-1",
                        "version": "version-1",
                        "status": "partial",
                        "evidence_kind": "captured-source",
                        "gaps": ["No configured model analyzed the evidence."],
                    },
                }
            ],
            "gaps": ["Research run run-1: No configured model analyzed the evidence."],
        },
    }
    request.return_value = context_payload
    result = await mcp_server.query_project_context("workspace-1", "project-1", "auth", 5)
    request.assert_awaited_once_with(
        "GET",
        "/api/v1/workspaces/workspace-1/projects/project-1/agent-data/context",
        params={"q": "auth", "limit": 5},
    )
    assert result == context_payload


@pytest.mark.asyncio
async def test_research_tools_are_thin_shared_route_adapters(monkeypatch):
    request = AsyncMock(return_value={"success": True, "data": {"id": "run-1"}})
    monkeypatch.setattr(mcp_server, "_request", request)

    await mcp_server.start_project_research(
        "workspace-1",
        "project-1",
        "research-brief",
        "Compare search systems",
        "request-1",
        ["https://docs.example"],
        4,
    )
    request.assert_awaited_once_with(
        "POST",
        "/api/v1/workspaces/workspace-1/projects/project-1/research/runs",
        json={
            "template_id": "research-brief",
            "question": "Compare search systems",
            "seed_urls": ["https://docs.example"],
            "max_sources": 4,
            "request_id": "request-1",
        },
    )

    request.reset_mock()
    await mcp_server.get_project_research_run("workspace-1", "project-1", "run-1")
    request.assert_awaited_once_with(
        "GET",
        "/api/v1/workspaces/workspace-1/projects/project-1/research/runs/run-1",
    )
