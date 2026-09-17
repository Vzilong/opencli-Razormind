"""Chat capabilities, administrator-only PATH detection, and execution validation."""

import os
import shutil

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.model_default import ModelDefault
from backend.models.provider import ModelProvider
from backend.models.provider_model import ProviderModel
from backend.schemas.agent_conversation import (
    EXECUTION_CONTEXT_KEY,
    AgentChatModelOption,
    AgentChatOptions,
    AgentChatProviderOption,
    AgentChatRuntimeOption,
    AgentExecutionSelection,
)
from backend.security.identity import RequestIdentity, is_platform_admin
from backend.security.workspace_rbac import (
    WorkspaceAccess,
    WorkspacePermission,
    get_workspace_access,
    require_permission,
)

_NATIVE_RUNTIMES = (
    ("claude", "Claude Code", "claude"),
    ("codex", "Codex", "codex"),
    ("omp", "Oh My Pi", "omp"),
    ("opencode", "opencode", "opencode"),
    ("cursor", "Cursor Agent", "cursor-agent"),
    ("agy", "Antigravity", "agy"),
    ("grok", "Grok Build", "grok"),
    ("pi", "Pi", "pi"),
)


def native_runtime_options(identity: RequestIdentity) -> list[AgentChatRuntimeOption]:
    detect_installation = is_platform_admin(identity)
    options: list[AgentChatRuntimeOption] = []
    for runtime_id, name, binary in _NATIVE_RUNTIMES:
        installed = None
        installation_reason = "Installation detection requires platform administrator access."
        if detect_installation:
            try:
                installed = any(
                    shutil.which(os.path.join(directory, binary)) is not None
                    for directory in os.environ.get("PATH", "").split(os.pathsep)
                    if os.path.isabs(directory)
                )
                installation_reason = (
                    "Installed on the API host PATH."
                    if installed
                    else "Not installed on the API host PATH."
                )
            except (OSError, ValueError):
                installation_reason = "Installation detection is unavailable on the API host."
        options.append(
            AgentChatRuntimeOption(
                id=runtime_id,
                name=name,
                available=False,
                reason=f"Not connected to the conversation engine. {installation_reason}",
                installed=installed,
                modes=[],
                access_modes=[],
            )
        )
    return options


def can_select_provider(identity: RequestIdentity, access: WorkspaceAccess) -> bool:
    return is_platform_admin(identity) or access.allows(WorkspacePermission.MANAGE_CONFIGURATION)


def stored_execution(binding: dict) -> AgentExecutionSelection:
    if EXECUTION_CONTEXT_KEY not in binding:
        return AgentExecutionSelection()
    try:
        selection = AgentExecutionSelection.model_validate(binding[EXECUTION_CONTEXT_KEY])
    except ValidationError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Invalid stored execution selection") from exc
    if selection.provider_id is not None and selection.model_id is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Stored execution model is missing")
    return selection


async def validate_provider_model(db: AsyncSession, provider_id: str, model_id: str | None) -> str:
    provider = (
        await db.execute(
            select(ModelProvider.enabled, ModelProvider.default_model).where(
                ModelProvider.id == provider_id
            )
        )
    ).one_or_none()
    if provider is None or not provider.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Selected provider is unavailable")
    selected_model = model_id if model_id is not None else provider.default_model
    if not selected_model or not selected_model.strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "Select a configured provider model")
    catalog_model = (
        await db.execute(
            select(ProviderModel.enabled, ProviderModel.model_type).where(
                ProviderModel.provider_id == provider_id,
                ProviderModel.model_id == selected_model,
            )
        )
    ).one_or_none()
    if catalog_model is not None:
        if not catalog_model.enabled or catalog_model.model_type != "llm":
            raise HTTPException(status.HTTP_409_CONFLICT, "Selected model is unavailable")
    elif selected_model != provider.default_model:
        raise HTTPException(status.HTTP_409_CONFLICT, "Selected model is not configured")
    return selected_model


async def validate_execution(
    db: AsyncSession,
    identity: RequestIdentity,
    access: WorkspaceAccess,
    execution: AgentExecutionSelection,
    workspace_id: str | None = None,
) -> AgentExecutionSelection:
    if execution.runtime_id != "opencli":
        from backend.services.agent_native_chat import require_native_binding

        if not workspace_id:
            raise HTTPException(409, "原生会话需要明确的工作区。")
        binding = await require_native_binding(
            db, identity, access, workspace_id, execution.runtime_id, execution.binding_revision
        )
        return execution.model_copy(update={"binding_revision": binding.revision})
    if execution.provider_id is None:
        return execution
    if not can_select_provider(identity, access):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Provider selection permission required")
    model_id = await validate_provider_model(db, execution.provider_id, execution.model_id)
    return execution.model_copy(update={"model_id": model_id})


async def default_route_ready(db: AsyncSession) -> bool:
    candidates = await db.scalar(select(ModelDefault.candidates).where(ModelDefault.role == "chat"))
    if candidates:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            provider_id = candidate.get("provider_id")
            model_id = candidate.get("model_id")
            if not isinstance(provider_id, str) or not isinstance(model_id, str) or not model_id:
                continue
            try:
                await validate_provider_model(db, provider_id, model_id)
            except HTTPException:
                continue
            return True
        return False
    provider = (
        await db.execute(
            select(ModelProvider.provider_type, ModelProvider.default_model)
            .where(ModelProvider.enabled.is_(True))
            .order_by(ModelProvider.created_at.asc())
            .limit(1)
        )
    ).one_or_none()
    return provider is not None and (
        provider.provider_type != "local" or bool(provider.default_model)
    )


async def get_chat_options(
    db: AsyncSession, identity: RequestIdentity, workspace_id: str
) -> AgentChatOptions:
    access = await get_workspace_access(db, workspace_id, identity)
    require_permission(access, WorkspacePermission.READ)
    allowed = can_select_provider(identity, access)
    providers: list[AgentChatProviderOption] = []
    if allowed:
        provider_rows = (
            await db.execute(
                select(ModelProvider.id, ModelProvider.name, ModelProvider.default_model)
                .where(ModelProvider.enabled.is_(True))
                .order_by(ModelProvider.created_at, ModelProvider.id)
            )
        ).all()
        model_rows = (
            await db.execute(
                select(
                    ProviderModel.provider_id,
                    ProviderModel.model_id,
                    ProviderModel.enabled,
                    ProviderModel.model_type,
                )
                .join(ModelProvider, ModelProvider.id == ProviderModel.provider_id)
                .where(ModelProvider.enabled.is_(True))
                .order_by(ProviderModel.model_id)
            )
        ).all()
        for provider in provider_rows:
            catalog = {row.model_id: row for row in model_rows if row.provider_id == provider.id}
            model_ids = [
                model_id
                for model_id, row in catalog.items()
                if row.enabled and row.model_type == "llm"
            ]
            if provider.default_model and provider.default_model not in catalog:
                model_ids.append(provider.default_model)
            providers.append(
                AgentChatProviderOption(
                    id=provider.id,
                    name=provider.name,
                    models=[
                        AgentChatModelOption(id=model_id, name=model_id) for model_id in model_ids
                    ],
                    default_model=(
                        provider.default_model if provider.default_model in model_ids else None
                    ),
                )
            )
    native_options = native_runtime_options(identity)
    if is_platform_admin(identity):
        from backend.services.agent_native_chat import require_native_binding

        for runtime in native_options:
            if runtime.id not in {"codex", "omp"}:
                continue
            try:
                await require_native_binding(db, identity, access, workspace_id, runtime.id)
            except HTTPException as exc:
                runtime.reason = f"{runtime.reason} {exc.detail}"
            else:
                runtime.available = True
                runtime.installed = True
                runtime.reason = None
                runtime.modes = ["gui", "terminal"]
                runtime.access_modes = ["native"]
    return AgentChatOptions(
        runtimes=[
            AgentChatRuntimeOption(
                id="opencli",
                name="OpenCLI",
                available=True,
                reason=None,
                installed=True,
                modes=["gui"],
                access_modes=["provider"],
            ),
            *native_options,
        ],
        providers=providers,
        provider_selection_allowed=allowed,
        default_route_ready=await default_route_ready(db),
    )
