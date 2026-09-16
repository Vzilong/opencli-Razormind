"""Persist and project plugin metadata without loading plugin-owned code."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.browser import (
    BrowserInstance,
    BrowserRuntimeBundle,
    BrowserRuntimeDeployment,
)
from backend.models.identity import Workspace
from backend.models.plugin_installation import PluginInstallation
from backend.models.studio import (
    StudioProject,
    StudioWorkflow,
    StudioWorkflowDraft,
    StudioWorkspace,
)
from backend.plugins.dify_manifest import parse_dify_manifest
from backend.plugins.dify_package import DifyPackageError, read_dify_plugin_payload
from backend.schemas.plugin import PluginInstallationRead
from backend.workflow.dify_graphon_client import DIFY_GRAPHON_BINDING_ID


class PluginRegistryError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


async def list_plugin_installations(
    session: AsyncSession,
    *,
    workspace_id: str | None = None,
    dify_runtime_ready: bool = False,
) -> list[PluginInstallationRead]:
    query = select(PluginInstallation).order_by(
        PluginInstallation.provider_key, PluginInstallation.version
    )
    if workspace_id is None:
        query = query.where(PluginInstallation.workspace_id.is_(None))
    else:
        query = query.where(PluginInstallation.workspace_id == workspace_id)
    rows = (await session.scalars(query)).all()
    runtime_bundles = await _browser_runtime_bundle_installations(session)
    return [
        *_bundled_installations(dify_runtime_ready=dify_runtime_ready),
        *runtime_bundles,
        *[_to_read(row) for row in rows],
    ]


async def get_plugin_installation(
    session: AsyncSession,
    installation_id: str,
    *,
    workspace_id: str | None = None,
    dify_runtime_ready: bool = False,
) -> PluginInstallationRead | None:
    runtime_bundle = next(
        (
            item
            for item in await _browser_runtime_bundle_installations(session)
            if item.id == installation_id
        ),
        None,
    )
    if runtime_bundle is not None:
        return runtime_bundle
    bundled = next(
        (
            item
            for item in _bundled_installations(dify_runtime_ready=dify_runtime_ready)
            if item.id == installation_id
        ),
        None,
    )
    if bundled is not None:
        return bundled
    query = select(PluginInstallation).where(PluginInstallation.id == installation_id)
    if workspace_id is None:
        query = query.where(PluginInstallation.workspace_id.is_(None))
    else:
        query = query.where(PluginInstallation.workspace_id == workspace_id)
    row = await session.scalar(query)
    return _to_read(row) if row is not None else None


async def import_dify_plugin(
    session: AsyncSession,
    *,
    filename: str,
    content: bytes,
    workspace_id: str | None = None,
) -> PluginInstallationRead:
    payload = read_dify_plugin_payload(content, filename=filename)
    parsed = parse_dify_manifest(payload)

    existing = await _find_exact_installation(
        session,
        provider_key=parsed.provider_key,
        version=parsed.version,
        workspace_id=workspace_id,
        source_digest=payload.source_digest,
    )
    if existing is not None:
        return _to_read(existing)
    conflict_query = select(PluginInstallation).where(
        PluginInstallation.provider_key == parsed.provider_key,
        PluginInstallation.version == parsed.version,
    )
    if workspace_id is None:
        conflict_query = conflict_query.where(PluginInstallation.workspace_id.is_(None))
    else:
        conflict_query = conflict_query.where(
            PluginInstallation.workspace_id == workspace_id
        )
    conflicting = await session.scalar(conflict_query)
    if conflicting is not None:
        raise PluginRegistryError(
            "dify_plugin_version_conflict",
            (
                f'Plugin "{parsed.provider_key}" version "{parsed.version}" is already '
                "installed with different content."
            ),
            status_code=409,
        )
    row = PluginInstallation(
        workspace_id=workspace_id,
        enabled=workspace_id is None,
        provider_key=parsed.provider_key,
        name=parsed.name,
        author=parsed.author,
        version=parsed.version,
        source_kind=payload.source_kind,
        source_digest=payload.source_digest,
        manifest_spec_version=parsed.manifest_spec_version,
        signature_state=payload.signature_state,
        manifest_json={
            "raw": parsed.manifest,
            "labels": parsed.labels,
            "descriptions": parsed.descriptions,
            "icon": parsed.icon,
            "pluginTypes": parsed.plugin_types,
        },
        capabilities_json=parsed.capabilities,
        permissions_json=parsed.permissions,
        runtime_status="BLOCKED",
        blockers_json=parsed.blockers,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        duplicate = await _find_exact_installation(
            session,
            provider_key=parsed.provider_key,
            version=parsed.version,
            workspace_id=workspace_id,
            source_digest=payload.source_digest,
        )
        if duplicate is not None:
            return _to_read(duplicate)
        raise PluginRegistryError(
            "dify_plugin_version_conflict",
            f'Plugin "{parsed.provider_key}" version "{parsed.version}" is already installed.',
            status_code=409,
        ) from exc
    return _to_read(row)


async def delete_plugin_installation(
    session: AsyncSession,
    installation_id: str,
    *,
    workspace_id: str | None = None,
) -> None:
    if installation_id.startswith("bundled:"):
        raise PluginRegistryError(
            "bundled_plugin_cannot_uninstall",
            "Bundled OpenCLI capabilities cannot be uninstalled from the plugin catalog.",
            status_code=409,
        )
    if installation_id.startswith("browser-runtime:"):
        raise PluginRegistryError(
            "browser_runtime_bundle_cannot_uninstall",
            "Browser runtime bundles are managed through the browser runtime API.",
            status_code=409,
        )
    query = select(PluginInstallation).where(PluginInstallation.id == installation_id)
    if workspace_id is None:
        query = query.where(PluginInstallation.workspace_id.is_(None))
    else:
        query = query.where(PluginInstallation.workspace_id == workspace_id)
    row = await session.scalar(query)
    if row is None:
        raise PluginRegistryError(
            "plugin_installation_not_found",
            "Plugin installation not found.",
            status_code=404,
        )
    if await _referencing_draft_ids(session, row):
        raise PluginRegistryError(
            "plugin_installation_in_use",
            "A stored workflow draft still references this plugin installation.",
            status_code=409,
        )
    await session.delete(row)
    await session.flush()


async def update_plugin_installation(
    session: AsyncSession,
    installation_id: str,
    *,
    workspace_id: str,
    enabled: bool | None = None,
    granted_permissions: list[str] | None = None,
) -> PluginInstallationRead:
    row = await session.scalar(
        select(PluginInstallation).where(
            PluginInstallation.id == installation_id,
            PluginInstallation.workspace_id == workspace_id,
        )
    )
    if row is None:
        raise PluginRegistryError(
            "plugin_installation_not_found",
            "Plugin installation not found.",
            status_code=404,
        )
    declared_permissions = (row.permissions_json or {}).get("declared", {})
    declared = (
        set(declared_permissions)
        if isinstance(declared_permissions, dict)
        else set()
    )
    if granted_permissions is not None:
        invalid = sorted(set(granted_permissions) - declared)
        if invalid:
            raise PluginRegistryError(
                "plugin_permission_not_declared",
                f"Plugin permissions are not declared: {', '.join(invalid)}.",
                status_code=422,
            )
        row.granted_permissions_json = sorted(set(granted_permissions))
    if enabled is not None:
        row.enabled = enabled
    await session.flush()
    return _to_read(row)


async def _find_exact_installation(
    session: AsyncSession,
    *,
    provider_key: str,
    version: str,
    workspace_id: str | None,
    source_digest: str,
) -> PluginInstallation | None:
    query = select(PluginInstallation).where(
        PluginInstallation.provider_key == provider_key,
        PluginInstallation.version == version,
        PluginInstallation.source_digest == source_digest,
    )
    if workspace_id is None:
        query = query.where(PluginInstallation.workspace_id.is_(None))
    else:
        query = query.where(PluginInstallation.workspace_id == workspace_id)
    return await session.scalar(query)


async def _referencing_draft_ids(
    session: AsyncSession, installation: PluginInstallation
) -> list[str]:
    query = select(StudioWorkflowDraft)
    if installation.workspace_id is not None:
        query = (
            query.join(StudioWorkflow, StudioWorkflow.id == StudioWorkflowDraft.workflow_id)
            .join(StudioProject, StudioProject.id == StudioWorkflow.project_id)
            .join(StudioWorkspace, StudioWorkspace.id == StudioProject.workspace_id)
            .join(Workspace, Workspace.id == installation.workspace_id)
            .where(
                or_(
                    StudioProject.workspace_id == installation.workspace_id,
                    StudioWorkspace.slug == Workspace.slug,
                )
            )
        )
    drafts = (await session.scalars(query)).all()
    return [
        draft.id
        for draft in drafts
        if _graph_references_installation(
            draft.graph,
            installation_id=installation.id,
            provider_key=installation.provider_key,
            version=installation.version,
        )
    ]


def _graph_references_installation(
    value: Any,
    *,
    installation_id: str,
    provider_key: str,
    version: str,
) -> bool:
    if isinstance(value, dict):
        direct_id = value.get("pluginInstallationId") or value.get("installationId")
        if direct_id == installation_id:
            return True
        direct_provider = value.get("pluginProviderKey") or value.get("providerKey")
        direct_version = value.get("pluginVersion")
        if direct_provider == provider_key and direct_version == version:
            return True
        return any(
            _graph_references_installation(
                nested,
                installation_id=installation_id,
                provider_key=provider_key,
                version=version,
            )
            for nested in value.values()
        )
    if isinstance(value, list):
        return any(
            _graph_references_installation(
                nested,
                installation_id=installation_id,
                provider_key=provider_key,
                version=version,
            )
            for nested in value
        )
    return False


def _to_read(row: PluginInstallation) -> PluginInstallationRead:
    manifest = row.manifest_json or {}
    capabilities = _effective_capabilities(row)
    blockers = _effective_installation_blockers(row)
    return PluginInstallationRead(
        id=row.id,
        workspaceId=row.workspace_id,
        enabled=row.enabled,
        providerKey=row.provider_key,
        name=row.name,
        author=row.author,
        version=row.version,
        sourceKind=row.source_kind,
        sourceDigest=row.source_digest,
        manifestSpecVersion=row.manifest_spec_version,
        signatureState=row.signature_state,
        labels=_dict_of_strings(manifest.get("labels")),
        descriptions=_dict_of_strings(manifest.get("descriptions")),
        icon=manifest.get("icon") if isinstance(manifest.get("icon"), str) else None,
        pluginTypes=_list_of_strings(manifest.get("pluginTypes")),
        manifest=manifest.get("raw") if isinstance(manifest.get("raw"), dict) else {},
        capabilities=capabilities,
        permissions=row.permissions_json or {},
        grantedPermissions=list(row.granted_permissions_json or []),
        runtimeStatus="BLOCKED" if blockers else row.runtime_status,
        blockers=blockers,
        nodeDefinitions=_node_definitions(
            capabilities,
            installation_id=row.id,
            provider_key=row.provider_key,
            version=row.version,
            enabled=row.enabled,
        ),
        bundled=False,
        installedAt=row.created_at,
        updatedAt=row.updated_at,
    )


def _effective_capabilities(row: PluginInstallation) -> list[dict[str, Any]]:
    capabilities = [dict(item) for item in row.capabilities_json or []]
    installation_blockers = _effective_installation_blockers(row)
    if not installation_blockers:
        return capabilities
    for capability in capabilities:
        capability["status"] = "BLOCKED"
        blockers = list(capability.get("blockers") or [])
        existing_codes = {
            blocker.get("code")
            for blocker in blockers
            if isinstance(blocker, dict)
        }
        blockers.extend(
            blocker
            for blocker in installation_blockers
            if blocker["code"] not in existing_codes
        )
        capability["blockers"] = blockers
    return capabilities


def _effective_installation_blockers(
    row: PluginInstallation,
) -> list[dict[str, str]]:
    blockers = [
        blocker
        for blocker in (row.blockers_json or [])
        if isinstance(blocker, dict)
        and isinstance(blocker.get("code"), str)
        and isinstance(blocker.get("message"), str)
    ]
    if not row.enabled:
        blockers.append(
            {
                "code": "plugin_disabled",
                "message": "Plugin installation is disabled for this workspace.",
            }
        )
    for permission in _missing_permissions(row):
        blockers.append(
            {
                "code": "plugin_permission_not_granted",
                "message": (
                    f'Permission "{permission}" must be granted before this plugin can run.'
                ),
            }
        )
    if row.runtime_status != "READY" and not any(
        blocker["code"] == "dify_plugin_execution_disabled" for blocker in blockers
    ):
        blockers.append(
            {
                "code": "plugin_runtime_blocked",
                "message": "The plugin runtime is not ready for execution.",
            }
        )
    return _dedupe_blockers(blockers)


def _missing_permissions(row: PluginInstallation) -> list[str]:
    # Global installations predate workspace grants and are only mutable by a
    # platform administrator. Preserve their legacy implicit grant semantics;
    # workspace installations must opt into every declared permission.
    if row.workspace_id is None:
        return []
    permissions = row.permissions_json if isinstance(row.permissions_json, dict) else {}
    declared = permissions.get("declared", {})
    if not isinstance(declared, dict):
        return []
    required = {
        str(key)
        for key, value in declared.items()
        if value is not False and value is not None
    }
    granted = {
        str(permission)
        for permission in (row.granted_permissions_json or [])
        if isinstance(permission, str)
    }
    return sorted(required - granted)


def _dedupe_blockers(blockers: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for blocker in blockers:
        identity = (blocker["code"], blocker["message"])
        if identity not in seen:
            seen.add(identity)
            result.append(blocker)
    return result


def _node_definitions(
    capabilities: list[dict[str, Any]],
    *,
    installation_id: str,
    provider_key: str,
    version: str,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for capability in capabilities:
        if capability.get("flowCapability") is not True:
            continue
        status = (
            capability.get("status")
            if enabled and capability.get("status") == "READY"
            else "BLOCKED"
        )
        rows.append(
            {
                "id": (
                    f"plugin.{installation_id}.{capability.get('family')}.{capability.get('key')}"
                ),
                "label": str(capability.get("label") or capability.get("key") or "Plugin"),
                "family": str(capability.get("family") or "tool"),
                "status": status,
                "locked": status != "READY",
                "lockReason": (
                    None
                    if status == "READY"
                    else (
                        next(
                            (
                                blocker["message"]
                                for blocker in capability.get("blockers", [])
                                if isinstance(blocker, dict)
                                and isinstance(blocker.get("message"), str)
                            ),
                            None,
                        )
                        or (
                            "Enable this plugin installation first."
                            if not enabled
                            else "Install or configure a compatible OpenCLI runtime adapter."
                        )
                    )
                ),
                "installationId": installation_id,
                "providerKey": provider_key,
                "pluginVersion": version,
                "capabilityId": str(capability.get("id") or ""),
            }
        )
    return rows


async def _browser_runtime_bundle_installations(
    session: AsyncSession,
) -> list[PluginInstallationRead]:
    """Project persisted browser bundles into the read-only plugin registry."""

    bundles = (
        await session.scalars(
            select(BrowserRuntimeBundle).order_by(
                BrowserRuntimeBundle.name, BrowserRuntimeBundle.version
            )
        )
    ).all()
    if not bundles:
        return []
    instances = (await session.scalars(select(BrowserInstance))).all()
    deployments = {
        item.browser_instance_id: item
        for item in (await session.scalars(select(BrowserRuntimeDeployment))).all()
    }
    projected: list[PluginInstallationRead] = []
    for bundle in bundles:
        manifest = bundle.manifest
        assigned_instances = [
            instance for instance in instances if instance.runtime_bundle_id == bundle.id
        ]
        unhealthy = [
            (instance, deployments.get(instance.id))
            for instance in assigned_instances
            if deployments.get(instance.id) is None or deployments[instance.id].state != "READY"
        ]
        blockers: list[dict[str, str]] = []
        if not assigned_instances:
            blockers.append(
                {
                    "code": "no_runtime_slot",
                    "message": "没有 Slot 已加载并通过此 Bundle 的自检。",
                }
            )
        elif unhealthy:
            for instance, deployment in unhealthy:
                detail = (
                    deployment.diagnostics[0]
                    if deployment and deployment.diagnostics
                    else "尚未收到运行时上报"
                )
                blockers.append(
                    {
                        "code": "runtime_not_ready",
                        "message": f"{instance.endpoint}: {detail}",
                    }
                )
        runtime_status = "READY" if not blockers else "BLOCKED"
        component_paths = {
            item["id"]: item["path"]
            for item in manifest.get("components", [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str)
            and isinstance(item.get("path"), str)
        }
        capabilities = [
            {
                "id": f"browser-runtime:{bundle.id}:{item.get('name')}",
                "family": "tool",
                "key": item.get("name"),
                "label": item.get("name"),
                "sourcePath": component_paths.get(item.get("component_id")),
                "status": runtime_status,
                "runtimeAdapterId": None,
                "blockers": blockers,
                "flowCapability": False,
            }
            for item in manifest.get("capabilities", [])
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
        components = [
            {
                "kind": item.get("kind"),
                "id": item.get("id"),
                "version": item.get("version"),
            }
            for item in manifest.get("components", [])
            if isinstance(item, dict)
        ]
        digest = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        projected.append(
            PluginInstallationRead(
                id=f"browser-runtime:{bundle.id}",
                providerKey=f"browser-runtime/{bundle.name}",
                name=f"{bundle.name} Browser Runtime",
                author="OpenCLI Browser Runtime",
                version=bundle.version,
                sourceKind="bundled",
                sourceDigest=digest,
                manifestSpecVersion="browser-runtime-manifest/v1",
                signatureState=(
                    "bundled"
                    if bundle.trust_level in {"system", "trusted"}
                    else "present_unverified"
                ),
                labels={"zh_Hans": f"{bundle.name} 浏览器运行时"},
                descriptions={
                    "zh_Hans": "受版本化 Manifest 约束的浏览器扩展、脚本与 OpenCLI 插件。",
                },
                pluginTypes=["browser_runtime", "endpoint"],
                manifest=manifest,
                capabilities=capabilities,
                permissions={
                    "components": components,
                    "allowed_hosts": sorted(
                        {
                            host
                            for item in manifest.get("capabilities", [])
                            if isinstance(item, dict)
                            for host in item.get("allowed_hosts", [])
                            if isinstance(host, str)
                        }
                    ),
                    "risks": sorted(
                        {
                            item["risk"]
                            for item in manifest.get("capabilities", [])
                            if isinstance(item, dict) and isinstance(item.get("risk"), str)
                        }
                    ),
                    "required_gates": sorted(
                        {
                            item["required_gate"]
                            for item in manifest.get("capabilities", [])
                            if isinstance(item, dict) and isinstance(item.get("required_gate"), str)
                        }
                    ),
                },
                runtimeStatus=runtime_status,
                blockers=blockers,
                nodeDefinitions=[],
                bundled=True,
                installedAt=bundle.created_at,
                updatedAt=bundle.updated_at,
            )
        )
    return projected


def _bundled_installations(*, dify_runtime_ready: bool) -> list[PluginInstallationRead]:
    timestamp = datetime(2026, 7, 21, tzinfo=UTC)
    specs = [
        (
            "bundled:opencli-adapters",
            "opencli-admin/opencli-adapters",
            "opencli-adapters",
            "OpenCLI 网站适配器",
            [
                _bundled_capability(
                    "opencli-admin/opencli-adapters:datasource:site-read",
                    "datasource",
                    "site-read",
                    "网站读取",
                    "iii.collector-opencli.snapshot",
                ),
                _bundled_capability(
                    "opencli-admin/opencli-adapters:tool:site-action",
                    "tool",
                    "site-action",
                    "网站操作",
                    "external.tool.capability",
                ),
            ],
        ),
        (
            "bundled:native-data-sources",
            "opencli-admin/native-data-sources",
            "native-data-sources",
            "RSS 与 API 数据源",
            [
                _bundled_capability(
                    "opencli-admin/native-data-sources:datasource:rss",
                    "datasource",
                    "rss",
                    "RSS / Atom",
                    "workflow.source.fetch",
                ),
                _bundled_capability(
                    "opencli-admin/native-data-sources:datasource:http",
                    "datasource",
                    "http",
                    "HTTP / API",
                    "workflow.source.fetch",
                ),
            ],
        ),
        (
            "bundled:http-api",
            "opencli-admin/http-api",
            "http-api",
            "HTTP / API",
            [
                _bundled_capability(
                    "opencli-admin/http-api:datasource:http",
                    "datasource",
                    "http",
                    "HTTP / API",
                    "workflow.source.fetch",
                )
            ],
        ),
        (
            "bundled:model-runtime",
            "opencli-admin/model-runtime",
            "model-runtime",
            "模型运行时",
            [
                _bundled_capability(
                    "opencli-admin/model-runtime:model:analysis",
                    "model",
                    "analysis",
                    "模型分析",
                    "intelligence.agent.summary",
                )
            ],
        ),
        (
            "bundled:agent-runtime",
            "opencli-admin/agent-runtime",
            "agent-runtime",
            "Agent Runtime",
            [
                _bundled_capability(
                    "opencli-admin/agent-runtime:agent_strategy:execution",
                    "agent_strategy",
                    "execution",
                    "Agent 执行",
                    "package.ai.prompt-experiment",
                )
            ],
        ),
        (
            "bundled:schedule-trigger",
            "opencli-admin/schedule-trigger",
            "schedule-trigger",
            "Schedule Trigger",
            [
                _bundled_capability(
                    "opencli-admin/schedule-trigger:trigger:schedule",
                    "trigger",
                    "schedule",
                    "定时计划",
                    "intelligence.schedule.cron",
                )
            ],
        ),
        (
            "bundled:delivery",
            "opencli-admin/delivery",
            "delivery",
            "Delivery",
            [
                _bundled_capability(
                    "opencli-admin/delivery:tool:delivery",
                    "tool",
                    "delivery",
                    "结果交付",
                    "intelligence.output.webhook",
                )
            ],
        ),
        (
            "bundled:workflow-bundles",
            "opencli-admin/workflow-bundles",
            "workflow-bundles",
            "预制工作流工具包",
            [
                _bundled_capability(
                    "opencli-admin/workflow-bundles:tool:workflow-bundles",
                    "tool",
                    "workflow-bundles",
                    "预制工作流工具包",
                    "package.collection.pipeline",
                )
            ],
        ),
        (
            "bundled:dify-graphon-runtime",
            "opencli-admin/dify-graphon-runtime",
            "dify-graphon-runtime",
            "Dify / Graphon 兼容运行时",
            [
                _bundled_capability(
                    "opencli-admin/dify-graphon-runtime:tool:workflow-package",
                    "tool",
                    "workflow-package",
                    "Dify 工作流包",
                    DIFY_GRAPHON_BINDING_ID,
                )
            ],
        ),
    ]
    installations: list[PluginInstallationRead] = []
    for installation_id, provider_key, name, label, capabilities in specs:
        runtime_ready = installation_id != "bundled:dify-graphon-runtime" or dify_runtime_ready
        blockers = (
            []
            if runtime_ready
            else [
                {
                    "code": "dify_graphon_unavailable",
                    "message": (
                        "The pinned Graphon compatibility runtime is unavailable or "
                        "does not match the required identity."
                    ),
                }
            ]
        )
        projected_capabilities = [
            {
                **capability,
                "status": "READY" if runtime_ready else "BLOCKED",
                "blockers": blockers,
            }
            for capability in capabilities
        ]
        installations.append(
            PluginInstallationRead(
                id=installation_id,
                workspaceId=None,
                enabled=True,
                grantedPermissions=[],
                providerKey=provider_key,
                name=name,
                author="opencli-admin",
                version="builtin",
                sourceKind="bundled",
                sourceDigest="bundled",
                manifestSpecVersion="opencli.plugin.v1",
                signatureState="bundled",
                labels={"zh_Hans": label, "en_US": name.replace("-", " ").title()},
                descriptions={},
                pluginTypes=sorted({item["family"] for item in projected_capabilities}),
                manifest={"source": "opencli-admin", "bundled": True},
                capabilities=projected_capabilities,
                permissions={},
                runtimeStatus="READY" if runtime_ready else "BLOCKED",
                blockers=blockers,
                nodeDefinitions=_node_definitions(
                    projected_capabilities,
                    installation_id=installation_id,
                    provider_key=provider_key,
                    version="builtin",
                    enabled=True,
                ),
                bundled=True,
                installedAt=timestamp,
                updatedAt=timestamp,
            )
        )
    return installations


def _bundled_capability(
    capability_id: str,
    family: str,
    key: str,
    label: str,
    runtime_adapter_id: str,
) -> dict[str, Any]:
    return {
        "id": capability_id,
        "family": family,
        "key": key,
        "label": label,
        "sourcePath": None,
        "status": "READY",
        "runtimeAdapterId": runtime_adapter_id,
        "blockers": [],
        "flowCapability": True,
    }


def _dict_of_strings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str)}


def _list_of_strings(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


__all__ = [
    "DifyPackageError",
    "PluginRegistryError",
    "delete_plugin_installation",
    "get_plugin_installation",
    "import_dify_plugin",
    "list_plugin_installations",
]
