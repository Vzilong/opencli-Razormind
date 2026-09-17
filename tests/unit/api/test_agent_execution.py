import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import openai
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.api.v1 import chat
from backend.main import app
from backend.models.agent_conversation import AgentConversation, AgentConversationTurn
from backend.models.identity import User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.model_default import ModelDefault
from backend.models.provider import ModelProvider
from backend.models.provider_model import ProviderModel
from backend.schemas.agent_conversation import EXECUTION_CONTEXT_KEY
from backend.security.identity import RequestIdentity, get_request_identity
from backend.services import agent_conversation_service


@pytest.fixture
async def execution_setup(db_session, client, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "runtime-bin"))
    monkeypatch.setattr("backend.services.agent_chat_options.shutil.which", lambda binary: None)
    user = User(subject="execution-member")
    workspace = Workspace(name="Execution", slug="execution")
    provider = ModelProvider(
        name="Chosen provider",
        default_model="default-model",
        base_url="https://private-endpoint.example/v1",
        api_key="test-private-key",
    )
    db_session.add_all([user, workspace, provider])
    await db_session.flush()
    membership = WorkspaceMembership(
        user_id=user.id, workspace_id=workspace.id, role=WorkspaceRole.MAINTAINER
    )
    models = [
        ProviderModel(provider_id=provider.id, model_id=model_id, source="manual")
        for model_id in ("default-model", "chosen-model")
    ]
    db_session.add_all([membership, *models])
    await db_session.commit()
    identity = RequestIdentity(subject=user.subject)

    async def identity_override():
        return identity

    app.dependency_overrides[get_request_identity] = identity_override
    return SimpleNamespace(
        user=user,
        workspace=workspace,
        provider=provider,
        membership=membership,
        models=models,
        identity=identity,
    )


async def _create(client, setup, execution=None, **extra):
    payload = {"workspace_id": setup.workspace.id, **extra}
    if execution is not None:
        payload["execution"] = execution
    return await client.post("/api/v1/chat/sessions", json=payload)


def _selection(setup, **extra):
    return {"runtime_id": "opencli", "provider_id": setup.provider.id, **extra}


async def test_options_are_safe_and_native_installation_is_unknown(
    client, db_session, execution_setup
):
    setup = execution_setup
    disabled_provider = ModelProvider(name="Disabled provider", enabled=False)
    db_session.add(disabled_provider)
    db_session.add_all(
        [
            ProviderModel(
                provider_id=setup.provider.id,
                model_id="disabled-model",
                enabled=False,
                source="manual",
            ),
            ProviderModel(
                provider_id=setup.provider.id,
                model_id="embedding-model",
                model_type="embedding",
                source="manual",
            ),
        ]
    )
    await db_session.commit()

    response = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["reasoning_efforts"] == []
    assert data["provider_selection_allowed"] is True
    assert data["providers"] == [
        {
            "id": setup.provider.id,
            "name": setup.provider.name,
            "models": [
                {"id": "chosen-model", "name": "chosen-model"},
                {"id": "default-model", "name": "default-model"},
            ],
            "default_model": "default-model",
        }
    ]
    assert data["runtimes"][0] == {
        "id": "opencli",
        "name": "OpenCLI",
        "available": True,
        "reason": None,
        "installed": True,
        "modes": ["gui"],
        "access_modes": ["provider"],
    }
    native = data["runtimes"][1:]
    assert {item["id"] for item in native} == {
        "codex",
        "claude",
        "omp",
        "opencode",
        "pi",
        "cursor",
        "agy",
        "grok",
    }
    for runtime in native:
        assert runtime["available"] is False
        assert runtime["installed"] is None
        assert "Not connected to the conversation engine" in runtime["reason"]
        assert runtime["modes"] == runtime["access_modes"] == []
    for private in ("api_key", "base_url", "test-private-key", "private-endpoint", "notes"):
        assert private not in response.text


@pytest.mark.parametrize("admin", [False, True])
async def test_viewer_catalog_and_selection_permissions(client, db_session, execution_setup, admin):
    setup = execution_setup
    setup.membership.role = WorkspaceRole.VIEWER
    await db_session.commit()

    async def identity_override():
        return RequestIdentity(subject=setup.user.subject, is_platform_admin=admin)

    app.dependency_overrides[get_request_identity] = identity_override
    options = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    assert options.status_code == 200
    assert options.json()["data"]["provider_selection_allowed"] is admin
    assert bool(options.json()["data"]["providers"]) is admin
    explicit = await _create(client, setup, _selection(setup))
    assert explicit.status_code == (201 if admin else 403)
    default = await _create(client, setup)
    assert default.status_code == 201
    assert default.json()["data"]["execution"]["provider_id"] is None


@pytest.mark.parametrize("state", ["outsider", "inactive_workspace", "disabled_user"])
async def test_workspace_read_required_for_options_and_create(
    client, db_session, execution_setup, state
):
    setup = execution_setup
    if state == "outsider":

        async def outsider():
            return RequestIdentity(subject="outsider", is_platform_admin=True)

        app.dependency_overrides[get_request_identity] = outsider
    elif state == "inactive_workspace":
        setup.workspace.active = False
    else:
        setup.user.disabled = True
    await db_session.commit()
    options = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    created = await _create(client, setup, _selection(setup))
    assert options.status_code == created.status_code == 403


async def test_missing_identity_is_unauthorized(client, execution_setup):
    app.dependency_overrides.pop(get_request_identity)
    response = await client.get(
        "/api/v1/chat/options", params={"workspace_id": execution_setup.workspace.id}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "execution",
    [
        {"runtime_id": runtime_id}
        for runtime_id in ("codex", "claude", "omp", "opencode", "pi", "cursor", "agy", "grok")
    ]
    + [{"mode": "shell"}, {"access_mode": "native"}, {"model_id": "orphan-model"}],
)
async def test_unsupported_choices_are_rejected(client, db_session, execution_setup, execution):
    response = await _create(client, execution_setup, execution)
    assert response.status_code == 422
    assert await db_session.scalar(select(func.count()).select_from(AgentConversation)) == 0


@pytest.mark.parametrize(
    "invalid",
    [
        "missing_provider",
        "disabled_provider",
        "unknown_model",
        "disabled_model",
        "embedding_model",
        "missing_default",
    ],
)
async def test_invalid_configuration_never_creates_session(
    client, db_session, execution_setup, invalid
):
    setup = execution_setup
    selection = _selection(setup)
    if invalid == "missing_provider":
        selection["provider_id"] = "missing"
    elif invalid == "disabled_provider":
        setup.provider.enabled = False
    elif invalid == "unknown_model":
        selection["model_id"] = "unregistered"
    elif invalid == "disabled_model":
        setup.models[0].enabled = False
    elif invalid == "embedding_model":
        setup.models[0].model_type = "embedding"
    else:
        setup.provider.default_model = None
    await db_session.commit()

    response = await _create(client, setup, selection)

    assert response.status_code == 409
    assert await db_session.scalar(select(func.count()).select_from(AgentConversation)) == 0


async def test_choice_survives_database_reload_and_reaches_real_tool_loop(
    client, db_session, execution_setup, monkeypatch
):
    setup = execution_setup
    created = await _create(
        client, setup, _selection(setup, model_id="chosen-model"), context={"surface": "home"}
    )
    assert created.status_code == 201, created.text
    session = created.json()["data"]
    expected = {
        "runtime_id": "opencli",
        "provider_id": setup.provider.id,
        "model_id": "chosen-model",
        "mode": "gui",
        "access_mode": "provider",
    }
    assert session["execution"] == expected
    assert session["context_binding"] == {"surface": "home"}
    session_factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    async with session_factory() as restored_db:
        stored = await restored_db.get(AgentConversation, session["id"])
        assert stored.context_binding[EXECUTION_CONTEXT_KEY] == expected
    setup.provider.default_model = "default-model"
    defaults = ModelDefault(
        role="chat", candidates=[{"provider_id": setup.provider.id, "model_id": "default-model"}]
    )
    db_session.add(defaults)
    await db_session.commit()

    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="selected model reply", tool_calls=[]))
        ]
    )
    completions = AsyncMock(return_value=reply)
    build_client = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=completions))
        )
    )
    monkeypatch.setattr(chat, "_build_client", build_client)
    run_chat = AsyncMock(wraps=chat.run_chat_request)
    monkeypatch.setattr(chat, "run_chat_request", run_chat)
    payload = {"request_id": "chosen-1", "content": "hello", "context": {"surface": "inbox"}}
    sent = await client.post(f"/api/v1/chat/sessions/{session['id']}/messages", json=payload)
    assert sent.status_code == 200, sent.text
    assert completions.await_args.kwargs["model"] == "chosen-model"
    assert completions.await_args.kwargs["tools"] == chat.TOOLS
    assert build_client.await_args.args[0].id == setup.provider.id
    request = run_chat.await_args.args[1]
    assert (request.provider_id, request.model_id) == (setup.provider.id, "chosen-model")
    assert request.context == {"surface": "inbox"}
    assert sent.json()["data"]["turn"]["context_binding"] == {"surface": "inbox"}
    duplicate = await client.post(f"/api/v1/chat/sessions/{session['id']}/messages", json=payload)
    assert duplicate.status_code == 200
    assert completions.await_count == 1

    for suffix in ("", "/replay"):
        restored = await client.get(f"/api/v1/chat/sessions/{session['id']}{suffix}")
        assert restored.json()["data"]["execution"] == expected
    listed = await client.get("/api/v1/chat/sessions", params={"workspace_id": setup.workspace.id})
    assert listed.json()["data"][0]["execution"] == expected
    assert setup.provider.default_model == "default-model"
    assert defaults.candidates == [{"provider_id": setup.provider.id, "model_id": "default-model"}]


async def test_provider_only_selection_pins_default_at_creation(
    client, db_session, execution_setup, monkeypatch
):
    setup = execution_setup
    created = await _create(client, setup, _selection(setup))
    session = created.json()["data"]
    assert session["execution"]["model_id"] == "default-model"
    setup.provider.default_model = "chosen-model"
    await db_session.commit()
    runner = AsyncMock(return_value=chat.ChatReply(type="message", content="reply"))
    monkeypatch.setattr(chat, "run_chat_request", runner)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "pinned", "content": "hello"},
    )
    assert response.status_code == 200
    assert runner.await_args.args[1].model_id == "default-model"


@pytest.mark.parametrize("field", [EXECUTION_CONTEXT_KEY, "execution"])
async def test_context_cannot_forge_or_replace_execution(
    client, db_session, execution_setup, monkeypatch, field
):
    setup = execution_setup
    forged_context = {field: _selection(setup, model_id="chosen-model")}
    forged_create = await _create(client, setup, context=forged_context)
    assert forged_create.status_code == 409
    created = await _create(client, setup, _selection(setup))
    session = created.json()["data"]
    runner = AsyncMock()
    monkeypatch.setattr(chat, "run_chat_request", runner)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "forged", "content": "hello", "context": forged_context},
    )
    assert response.status_code == 409
    runner.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(AgentConversationTurn)) == 0
    restored = await client.get(f"/api/v1/chat/sessions/{session['id']}")
    assert restored.json()["data"]["execution"] == session["execution"]


@pytest.mark.parametrize("invalid", ["disabled_provider", "disabled_model", "revoked_permission"])
async def test_send_revalidates_saved_configuration(
    client, db_session, execution_setup, monkeypatch, invalid
):
    setup = execution_setup
    created = await _create(client, setup, _selection(setup))
    session = created.json()["data"]
    if invalid == "disabled_provider":
        setup.provider.enabled = False
    elif invalid == "disabled_model":
        setup.models[0].enabled = False
    else:
        setup.membership.role = WorkspaceRole.VIEWER
    await db_session.commit()
    runner = AsyncMock()
    monkeypatch.setattr(chat, "run_chat_request", runner)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "revalidate", "content": "hello"},
    )
    assert response.status_code == (403 if invalid == "revoked_permission" else 409)
    runner.assert_not_awaited()
    assert await db_session.scalar(select(func.count()).select_from(AgentConversationTurn)) == 0


async def test_explicit_provider_failure_never_falls_back(
    client, db_session, execution_setup, monkeypatch
):
    setup = execution_setup
    fallback = ModelProvider(name="Fallback", default_model="fallback-model")
    db_session.add(fallback)
    await db_session.flush()
    db_session.add(
        ModelDefault(
            role="chat", candidates=[{"provider_id": fallback.id, "model_id": "fallback-model"}]
        )
    )
    await db_session.commit()
    created = await _create(client, setup, _selection(setup))
    session = created.json()["data"]
    completions = AsyncMock(
        side_effect=openai.APIConnectionError(
            request=httpx.Request("POST", "https://provider.example")
        )
    )
    build_client = AsyncMock(
        return_value=SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=completions))
        )
    )
    monkeypatch.setattr(chat, "_build_client", build_client)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "no-fallback", "content": "hello"},
    )
    assert response.status_code == 502
    assert build_client.await_count == 1
    assert build_client.await_args.args[0].id == setup.provider.id
    restored = await client.get(f"/api/v1/chat/sessions/{session['id']}")
    assert restored.json()["data"]["turns"][0]["status"] == "failed"


async def test_legacy_conversation_keeps_default_routing(client, execution_setup, monkeypatch):
    setup = execution_setup
    created = await _create(client, setup)
    session = created.json()["data"]
    runner = AsyncMock(return_value=chat.ChatReply(type="message", content="reply"))
    monkeypatch.setattr(chat, "run_chat_request", runner)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "legacy", "content": "hello"},
    )
    assert response.status_code == 200
    request = runner.await_args.args[1]
    assert request.provider_id is None
    assert request.model_id is None


async def test_stored_native_metadata_fails_closed(
    client, db_session, execution_setup, monkeypatch
):
    setup = execution_setup
    created = await _create(client, setup)
    session = created.json()["data"]
    conversation = await db_session.get(AgentConversation, session["id"])
    conversation.context_binding = {EXECUTION_CONTEXT_KEY: {"runtime_id": "codex"}}
    await db_session.commit()
    runner = AsyncMock()
    monkeypatch.setattr(agent_conversation_service.chat, "run_chat_request", runner)
    response = await client.post(
        f"/api/v1/chat/sessions/{session['id']}/messages",
        json={"request_id": "corrupt", "content": "hello"},
    )
    assert response.status_code == 409
    runner.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/api/v1/chat", "/api/v1/chat/stream"])
@pytest.mark.parametrize("actor", ["anonymous", "viewer", "operator", "outsider", "inactive"])
async def test_legacy_model_override_cannot_bypass_selection_authorization(
    client, db_session, execution_setup, monkeypatch, endpoint, actor
):
    setup = execution_setup
    identity = setup.identity
    if actor == "anonymous":
        identity = None
    elif actor in {"viewer", "operator"}:
        setup.membership.role = WorkspaceRole(actor)
    elif actor == "outsider":
        identity = RequestIdentity(subject="outsider", is_platform_admin=True)
    else:
        setup.workspace.active = False
    await db_session.commit()

    async def optional_identity():
        return identity

    app.dependency_overrides[chat._optional_request_identity] = optional_identity
    build = AsyncMock()
    create_run = AsyncMock()
    monkeypatch.setattr(chat, "_build_client", build)
    monkeypatch.setattr(chat, "_create_durable_run", create_run)
    response = await client.post(
        endpoint,
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "provider_id": setup.provider.id,
            "model_id": "chosen-model",
            "workspace_id": setup.workspace.id,
        },
    )
    assert response.status_code == (401 if actor == "anonymous" else 403)
    build.assert_not_awaited()
    create_run.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/api/v1/chat", "/api/v1/chat/stream"])
async def test_legacy_model_override_requires_explicit_workspace(
    client, execution_setup, monkeypatch, endpoint
):
    async def optional_identity():
        return execution_setup.identity

    app.dependency_overrides[chat._optional_request_identity] = optional_identity
    create_run = AsyncMock()
    monkeypatch.setattr(chat, "_create_durable_run", create_run)
    response = await client.post(
        endpoint,
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "provider_id": execution_setup.provider.id,
            "model_id": "chosen-model",
        },
    )
    assert response.status_code == 400
    create_run.assert_not_awaited()


@pytest.mark.parametrize("endpoint", ["/api/v1/chat", "/api/v1/chat/stream"])
async def test_authorized_legacy_model_override_reaches_requested_model(
    client, db_session, execution_setup, monkeypatch, endpoint
):
    setup = execution_setup

    async def optional_identity():
        return setup.identity

    app.dependency_overrides[chat._optional_request_identity] = optional_identity
    monkeypatch.setattr(
        chat, "AsyncSessionLocal", async_sessionmaker(db_session.bind, expire_on_commit=False)
    )
    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content="authorized reply", tool_calls=[]))
        ]
    )
    completions = AsyncMock(return_value=reply)
    monkeypatch.setattr(
        chat,
        "_build_client",
        AsyncMock(
            return_value=SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=completions))
            )
        ),
    )
    response = await client.post(
        endpoint,
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "provider_id": setup.provider.id,
            "model_id": "chosen-model",
            "workspace_id": setup.workspace.id,
        },
    )
    assert response.status_code == 200, response.text
    assert "authorized reply" in response.text
    assert completions.await_args.kwargs["model"] == "chosen-model"


@pytest.mark.parametrize(
    "routing",
    [
        "legacy",
        "disabled",
        "missing_candidate",
        "disabled_candidate",
        "valid_candidate",
        "disabled_candidate_model",
        "local_without_model",
    ],
)
async def test_default_route_readiness_is_separate_from_hidden_catalog(
    client, db_session, execution_setup, routing
):
    setup = execution_setup
    setup.membership.role = WorkspaceRole.OPERATOR
    if routing == "disabled":
        setup.provider.enabled = False
    elif routing == "local_without_model":
        setup.provider.provider_type = "local"
        setup.provider.default_model = None
    elif routing != "legacy":
        provider_id = "missing" if routing == "missing_candidate" else setup.provider.id
        if routing == "disabled_candidate":
            setup.provider.enabled = False
        if routing == "disabled_candidate_model":
            setup.models[1].enabled = False
        db_session.add(
            ModelDefault(
                role="chat", candidates=[{"provider_id": provider_id, "model_id": "chosen-model"}]
            )
        )
    await db_session.commit()
    response = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    assert response.status_code == 200
    options = response.json()["data"]
    assert options["providers"] == []
    assert options["provider_selection_allowed"] is False
    assert options["runtimes"][0]["available"] is True
    assert options["default_route_ready"] is (routing in {"legacy", "valid_candidate"})


async def test_admin_native_installation_detection_is_allowlisted_and_does_not_enable_dispatch(
    client, execution_setup, monkeypatch
):
    setup = execution_setup

    async def admin_identity():
        return RequestIdentity(subject=setup.user.subject, is_platform_admin=True)

    app.dependency_overrides[get_request_identity] = admin_identity
    installed_binaries = {"claude", "codex", "omp", "opencode"}
    lookup = Mock(
        side_effect=lambda binary: (
            f"C:/private-runtime-location/{os.path.basename(binary)}.cmd"
            if os.path.basename(binary) in installed_binaries
            else None
        )
    )
    monkeypatch.setattr("backend.services.agent_chat_options.shutil.which", lookup)
    response = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    assert response.status_code == 200
    native = response.json()["data"]["runtimes"][1:]
    assert [(runtime["id"], runtime["name"]) for runtime in native] == [
        ("claude", "Claude Code"),
        ("codex", "Codex"),
        ("omp", "Oh My Pi"),
        ("opencode", "opencode"),
        ("cursor", "Cursor Agent"),
        ("agy", "Antigravity"),
        ("grok", "Grok Build"),
        ("pi", "Pi"),
    ]
    assert all(os.path.isabs(call.args[0]) for call in lookup.call_args_list)
    assert [os.path.basename(call.args[0]) for call in lookup.call_args_list] == [
        "claude",
        "codex",
        "omp",
        "opencode",
        "cursor-agent",
        "agy",
        "grok",
        "pi",
    ]
    for runtime in native:
        assert runtime["installed"] is (runtime["id"] in installed_binaries)
        assert runtime["available"] is False
        assert runtime["modes"] == runtime["access_modes"] == []
        assert "Not connected to the conversation engine" in runtime["reason"]
    assert "private-runtime-location" not in response.text
    assert "version" not in response.text
    assert "binPath" not in response.text


@pytest.mark.parametrize(
    "role", [WorkspaceRole.ADMIN, WorkspaceRole.MAINTAINER, WorkspaceRole.VIEWER]
)
async def test_non_platform_admin_cannot_probe_host_installations(
    client, db_session, execution_setup, monkeypatch, role
):
    setup = execution_setup
    setup.membership.role = role
    await db_session.commit()
    lookup = Mock(side_effect=AssertionError("host PATH must not be probed"))
    monkeypatch.setattr("backend.services.agent_chat_options.shutil.which", lookup)
    response = await client.get("/api/v1/chat/options", params={"workspace_id": setup.workspace.id})
    assert response.status_code == 200
    for runtime in response.json()["data"]["runtimes"][1:]:
        assert runtime["installed"] is None
        assert runtime["available"] is False
        assert "requires platform administrator access" in runtime["reason"]
    lookup.assert_not_called()


async def test_platform_admin_without_workspace_access_cannot_probe_host(
    client, execution_setup, monkeypatch
):
    async def outsider_identity():
        return RequestIdentity(subject="outside-workspace", is_platform_admin=True)

    app.dependency_overrides[get_request_identity] = outsider_identity
    lookup = Mock(side_effect=AssertionError("host PATH must not be probed"))
    monkeypatch.setattr("backend.services.agent_chat_options.shutil.which", lookup)
    response = await client.get(
        "/api/v1/chat/options", params={"workspace_id": execution_setup.workspace.id}
    )
    assert response.status_code == 403
    lookup.assert_not_called()


async def test_installation_probe_failure_is_unknown_without_exposing_error_details(
    client, execution_setup, monkeypatch
):
    async def admin_identity():
        return RequestIdentity(subject=execution_setup.user.subject, is_platform_admin=True)

    app.dependency_overrides[get_request_identity] = admin_identity
    lookup = Mock(side_effect=OSError("private-host-path api_key=private-value"))
    monkeypatch.setattr("backend.services.agent_chat_options.shutil.which", lookup)
    response = await client.get(
        "/api/v1/chat/options", params={"workspace_id": execution_setup.workspace.id}
    )
    assert response.status_code == 200
    for runtime in response.json()["data"]["runtimes"][1:]:
        assert runtime["installed"] is None
        assert runtime["available"] is False
        assert "detection is unavailable" in runtime["reason"]
    assert "private-host-path" not in response.text
    assert "private-value" not in response.text
