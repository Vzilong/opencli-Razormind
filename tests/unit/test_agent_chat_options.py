import os
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.provider import ModelProvider
from backend.models.provider_model import ProviderModel
from backend.schemas.agent_conversation import EXECUTION_CONTEXT_KEY, AgentExecutionSelection
from backend.security.identity import RequestIdentity
from backend.services.agent_chat_options import (
    get_chat_options,
    native_runtime_options,
    stored_execution,
    validate_provider_model,
)


@pytest.mark.parametrize(
    "path_kind",
    ["missing", "empty", "empty_entry", "dot", "relative", "other_absolute", "explicit_absolute"],
)
def test_native_installation_requires_an_explicit_absolute_path_entry(
    tmp_path, monkeypatch, path_kind
):
    working_directory = tmp_path / "working"
    working_directory.mkdir()
    relative_directory = working_directory / "relative"
    relative_directory.mkdir()
    other_directory = tmp_path / "other"
    other_directory.mkdir()
    filename = "codex.cmd" if os.name == "nt" else "codex"
    for directory in (working_directory, relative_directory):
        candidate = directory / filename
        candidate.write_text("")
        candidate.chmod(0o755)
    monkeypatch.chdir(working_directory)
    monkeypatch.setenv("PATHEXT", ".CMD")
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    paths = {
        "empty": "",
        "empty_entry": os.pathsep + str(other_directory),
        "dot": ".",
        "relative": "relative",
        "other_absolute": str(other_directory),
        "explicit_absolute": str(working_directory),
    }
    if path_kind == "missing":
        monkeypatch.delenv("PATH", raising=False)
    else:
        monkeypatch.setenv("PATH", paths[path_kind])
    options = native_runtime_options(RequestIdentity(subject="admin", is_platform_admin=True))
    codex = next(option for option in options if option.id == "codex")
    assert codex.installed is (path_kind == "explicit_absolute")
    assert codex.available is False


@pytest.mark.skipif(os.name != "nt", reason="Windows PATHEXT behavior")
@pytest.mark.parametrize("pathext, installed", [(".CMD", True), (".EXE", False)])
def test_absolute_path_detection_honors_pathext(tmp_path, monkeypatch, pathext, installed):
    (tmp_path / "codex.cmd").write_text("")
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("PATHEXT", pathext)
    options = native_runtime_options(RequestIdentity(subject="admin", is_platform_admin=True))
    codex = next(option for option in options if option.id == "codex")
    assert codex.installed is installed
    assert codex.available is False


@pytest.mark.parametrize(
    "payload",
    [
        {"runtime_id": "codex"},
        {"runtime_id": "unknown"},
        {"mode": "shell"},
        {"access_mode": "native"},
        {"reasoning_effort": "high"},
        {"model_id": "configured-model"},
        {"provider_id": "   "},
        {"provider_id": "provider", "model_id": "   "},
    ],
)
def test_selection_rejects_unsupported_or_incomplete_configuration(payload):
    with pytest.raises(ValidationError):
        AgentExecutionSelection.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [None, {"runtime_id": "codex"}, {"provider_id": "provider"}, {"mode": "shell"}],
)
def test_invalid_stored_execution_never_falls_back(payload):
    with pytest.raises(HTTPException) as raised:
        stored_execution({EXECUTION_CONTEXT_KEY: payload})
    assert raised.value.status_code == 409


async def test_legacy_default_is_valid_but_disabled_catalog_row_takes_precedence(db_session):
    provider = ModelProvider(name="Legacy", default_model="legacy-model")
    db_session.add(provider)
    await db_session.commit()
    assert await validate_provider_model(db_session, provider.id, None) == "legacy-model"
    db_session.add(
        ProviderModel(
            provider_id=provider.id,
            model_id="legacy-model",
            enabled=False,
            source="manual",
        )
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as raised:
        await validate_provider_model(db_session, provider.id, None)
    assert raised.value.status_code == 409


async def test_readers_only_query_default_readiness_not_provider_catalog(db_session, monkeypatch):
    user = User(subject="reader")
    workspace = Workspace(name="Workspace", slug="reader")
    db_session.add_all([user, workspace])
    await db_session.flush()
    db_session.add(
        WorkspaceMembership(user_id=user.id, workspace_id=workspace.id, role=WorkspaceRole.VIEWER)
    )
    await db_session.commit()
    execute = AsyncMock(wraps=db_session.execute)
    monkeypatch.setattr(db_session, "execute", execute)

    options = await get_chat_options(db_session, RequestIdentity(subject="reader"), workspace.id)

    assert options.providers == []
    assert options.provider_selection_allowed is False
    assert options.default_route_ready is False
    for call in execute.call_args_list:
        query = str(call.args[0])
        assert "model_providers.name" not in query
        assert "model_providers.api_key" not in query
        assert "model_providers.base_url" not in query
        assert "provider_models" not in query
