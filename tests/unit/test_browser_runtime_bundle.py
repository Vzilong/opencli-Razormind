import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import select

from backend.models.browser import BrowserCapabilityInvocation, BrowserInstance
from backend.schemas.browser import (
    BrowserInstanceConfigUpdate,
    BrowserInstanceCreate,
    BrowserRuntimeBundleCreate,
    RuntimeBundleManifest,
    SlotRuntimeReport,
)
from backend.services import (
    browser_capability_service,
    browser_service,
    plugin_registry_service,
)


def manifest() -> RuntimeBundleManifest:
    return RuntimeBundleManifest.model_validate(
        {
            "name": "opencli-bridge",
            "version": "1.8.7",
            "components": [
                {
                    "kind": "extension",
                    "id": "bridge",
                    "version": "1.8.7",
                    "path": "extensions/bridge",
                    "required": True,
                    "capabilities": ["page.read"],
                },
                {
                    "kind": "script",
                    "id": "page-actions",
                    "version": "1.8.7",
                    "path": "scripts/page-actions",
                    "required": True,
                    "capabilities": [],
                },
                {
                    "kind": "opencli_plugin",
                    "id": "browser-tools",
                    "version": "1.8.7",
                    "path": "plugins/browser-tools",
                    "required": True,
                    "capabilities": [],
                },
            ],
            "capabilities": [
                {
                    "name": "page.read",
                    "component_id": "bridge",
                    "action": "page.read",
                    "runtime": "opentabs",
                    "args_schema": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}, "cookie": {"type": "string"}},
                        "required": ["url"],
                        "additionalProperties": False,
                    },
                    "allowed_hosts": ["example.com"],
                    "risk": "low",
                    "required_gate": None,
                }
            ],
            "act_pack_ids": ["search-research/google-search-serp"],
        }
    )


async def create_bundle(db_session, bundle_manifest: RuntimeBundleManifest | None = None):
    bundle = await browser_service.create_runtime_bundle(
        db_session,
        BrowserRuntimeBundleCreate(manifest=bundle_manifest or manifest()),
    )
    await db_session.commit()
    return bundle


def healthy_report(**overrides) -> SlotRuntimeReport:
    data = {
        "loaded_bundle_name": "opencli-bridge",
        "loaded_bundle_version": "1.8.7",
        "loaded_components": [
            {"kind": "extension", "id": "bridge", "version": "1.8.7", "healthy": True},
            {"kind": "script", "id": "page-actions", "version": "1.8.7", "healthy": True},
            {"kind": "opencli_plugin", "id": "browser-tools", "version": "1.8.7", "healthy": True},
        ],
        "capabilities": ["page.read"],
        "self_check": {"ok": True},
    }
    data.update(overrides)
    return SlotRuntimeReport.model_validate(data)


@pytest.mark.asyncio
async def test_matching_loaded_report_is_ready_and_persists_slot_config(db_session):
    bundle = await create_bundle(db_session)
    instance = await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(
            endpoint="http://agent:19823",
            profile_name="operator-a",
            runtime_bundle_id=bundle.id,
            startup_pages=["https://example.com"],
            network_policy={"mode": "direct"},
            resource_class="medium",
        ),
    )

    deployment = await browser_service.report_runtime_deployment(
        db_session, instance, healthy_report()
    )

    assert deployment.state == "READY"
    assert instance.runtime_bundle_id == bundle.id
    assert instance.startup_pages == ["https://example.com"]
    assert instance.network_policy == {"mode": "direct"}



@pytest.mark.asyncio
async def test_violentmonkey_bundle_rejects_missing_nested_access_check(db_session):
    manifest_payload = manifest().model_dump()
    manifest_payload["components"].append(
        {
            "kind": "extension",
            "id": "violentmonkey",
            "version": "2.48.0",
            "path": "extensions/violentmonkey",
            "required": True,
            "capabilities": [],
        }
    )
    bundle = await create_bundle(
        db_session, RuntimeBundleManifest.model_validate(manifest_payload)
    )
    instance = await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(
            endpoint="http://agent-violentmonkey:19823",
            profile_name="operator-violentmonkey",
            runtime_bundle_id=bundle.id,
        ),
    )
    report = healthy_report(
        loaded_components=[
            *healthy_report().model_dump()["loaded_components"],
            {
                "kind": "extension",
                "id": "violentmonkey",
                "version": "2.48.0",
                "healthy": True,
            },
        ]
    )

    deployment = await browser_service.report_runtime_deployment(db_session, instance, report)

    assert deployment.state == "EXTENSION_FAILED"
    assert deployment.diagnostics == [
        "Violentmonkey userScriptsAccess self-check did not report ok=true"
    ]

@pytest.mark.asyncio
async def test_two_slots_with_the_same_bundle_report_the_same_ready_runtime(db_session):
    bundle = await create_bundle(db_session)
    slots = [
        await browser_service.create_browser_instance(
            db_session,
            BrowserInstanceCreate(
                endpoint=f"http://agent-ready-{index}:19823",
                profile_name=f"operator-ready-{index}",
                runtime_bundle_id=bundle.id,
            ),
        )
        for index in (1, 2)
    ]

    deployments = [
        await browser_service.report_runtime_deployment(db_session, slot, healthy_report())
        for slot in slots
    ]

    assert [deployment.state for deployment in deployments] == ["READY", "READY"]
    assert deployments[0].loaded_components == deployments[1].loaded_components
    assert deployments[0].self_check["capabilities"] == ["page.read"]


@pytest.mark.asyncio
async def test_optional_component_capabilities_are_omitted_when_component_is_absent(db_session):
    manifest_payload = manifest().model_dump()
    manifest_payload["components"][1]["required"] = False
    manifest_payload["capabilities"].append(
        {
            "name": "page.optional",
            "component_id": "page-actions",
            "action": "page.optional",
            "runtime": "opentabs",
            "args_schema": {"type": "object"},
            "allowed_hosts": [],
            "risk": "low",
            "required_gate": None,
        }
    )
    bundle = await create_bundle(db_session, RuntimeBundleManifest.model_validate(manifest_payload))
    instance = BrowserInstance(
        endpoint="http://agent-optional:19823",
        profile_name="operator-optional",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()
    report = healthy_report(
        loaded_components=[
            component
            for component in healthy_report().model_dump()["loaded_components"]
            if component["id"] != "page-actions"
        ]
    )

    deployment = await browser_service.report_runtime_deployment(db_session, instance, report)

    assert deployment.state == "READY"


@pytest.mark.asyncio
async def test_unknown_component_and_required_extension_failure_are_not_ready(db_session):
    bundle = await create_bundle(db_session)
    instance = BrowserInstance(
        endpoint="http://agent:19824",
        profile_name="operator-b",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()

    drift = await browser_service.report_runtime_deployment(
        db_session,
        instance,
        healthy_report(
            loaded_components=[
                {"kind": "extension", "id": "bridge", "version": "1.8.7", "healthy": True},
                {"kind": "extension", "id": "foreign", "version": "1", "healthy": True},
            ]
        ),
    )
    assert drift.state == "CONFIG_DRIFT"

    failed = await browser_service.report_runtime_deployment(
        db_session,
        instance,
        healthy_report(
            loaded_components=[
                {"kind": "extension", "id": "bridge", "version": "1.8.7", "healthy": False},
                {"kind": "script", "id": "page-actions", "version": "1.8.7", "healthy": True},
                {
                    "kind": "opencli_plugin",
                    "id": "browser-tools",
                    "version": "1.8.7",
                    "healthy": True,
                },
            ]
        ),
    )
    assert failed.state == "EXTENSION_FAILED"


@pytest.mark.asyncio
async def test_invoke_unready_slot_fails_closed_and_is_audited(db_session):
    bundle = await create_bundle(db_session)
    instance = BrowserInstance(
        endpoint="http://agent:19825",
        profile_name="operator-c",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()

    with pytest.raises(browser_service.BrowserRuntimeError) as excinfo:
        await browser_capability_service.invoke_capability(
            db_session,
            instance,
            "page.read",
            {"url": "https://example.com"},
            None,
        )

    assert excinfo.value.code == "slot_not_ready"
    invocation = (await db_session.execute(select(BrowserCapabilityInvocation))).scalar_one()
    assert invocation.error["code"] == "slot_not_ready"


@pytest.mark.asyncio
async def test_profile_cannot_be_assigned_to_two_slots(db_session):
    await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(endpoint="http://agent:19826", profile_name="exclusive"),
    )

    with pytest.raises(browser_service.BrowserRuntimeError) as excinfo:
        await browser_service.create_browser_instance(
            db_session,
            BrowserInstanceCreate(endpoint="http://agent:19827", profile_name="exclusive"),
        )

    assert excinfo.value.code == "profile_in_use"


@pytest.mark.asyncio
async def test_capability_gate_fail_closed_then_records_full_lineage(db_session, monkeypatch):
    manifest_payload = manifest().model_dump()
    manifest_payload["capabilities"][0]["risk"] = "high"
    manifest_payload["capabilities"][0]["required_gate"] = "operator-approval"
    bundle = await create_bundle(db_session, RuntimeBundleManifest.model_validate(manifest_payload))
    instance = BrowserInstance(
        endpoint="http://agent:19828",
        agent_url="http://agent:19828",
        agent_protocol="http",
        profile_name="operator-gated",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()
    await browser_service.report_runtime_deployment(db_session, instance, healthy_report())

    async def dispatch(_, __, args):
        return {
            "result": {"title": "Example"},
            "page_before": {"url": args["url"]},
            "page_after": {"url": args["url"], "title": "Example"},
        }

    monkeypatch.setattr(browser_capability_service, "_dispatch_capability", dispatch)

    with pytest.raises(browser_service.BrowserRuntimeError) as excinfo:
        await browser_capability_service.invoke_capability(
            db_session,
            instance,
            "page.read",
            {"url": "https://example.com"},
            None,
        )
    assert excinfo.value.code == "gate_not_satisfied"

    with pytest.raises(browser_service.BrowserRuntimeError) as excinfo:
        await browser_capability_service.invoke_capability(
            db_session,
            instance,
            "page.read",
            {"url": "https://example.com"},
            "operator-approval",
        )
    assert excinfo.value.code == "gate_not_satisfied"

    invocation = await browser_capability_service.invoke_capability(
        db_session,
        instance,
        "page.read",
        {"url": "https://example.com"},
        "operator-approval",
        gate_authorized=True,
    )

    assert invocation.output_payload == {
        "result": {"title": "Example"},
        "page_before": {"url": "https://example.com"},
        "page_after": {"url": "https://example.com", "title": "Example"},
    }
    assert invocation.desired_bundle_version == "1.8.7"
    assert invocation.loaded_bundle_version == "1.8.7"
    assert {item["id"] for item in invocation.component_versions} == {
        "bridge",
        "page-actions",
        "browser-tools",
    }
    assert invocation.risk == "high"
    assert invocation.gate == "operator-approval"
    assert invocation.page_before == {"url": "https://example.com"}
    assert invocation.page_after == {
        "url": "https://example.com",
        "title": "Example",
    }


@pytest.mark.asyncio
async def test_capability_audit_can_store_redacted_space_input(db_session, monkeypatch):
    bundle = await create_bundle(db_session)
    instance = BrowserInstance(
        endpoint="http://agent:19830",
        agent_url="http://agent:19830",
        agent_protocol="http",
        profile_name="operator-space-audit",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()
    await browser_service.report_runtime_deployment(db_session, instance, healthy_report())

    async def dispatch(_, __, args):
        return {"result": {"title": "Example"}, "page_after": {"title": "Example"}}

    monkeypatch.setattr(browser_capability_service, "_dispatch_capability", dispatch)
    invocation = await browser_capability_service.invoke_capability(
        db_session,
        instance,
        "page.read",
        {"url": "https://example.com", "cookie": "session=secret"},
        None,
        audit_input_payload={"url": "[redacted-url]", "cookie": "[REDACTED]"},
    )

    assert invocation.input_payload == {
        "url": "[redacted-url]",
        "cookie": "[REDACTED]",
    }


@pytest.mark.asyncio
async def test_bundle_is_projected_as_a_read_only_runtime_plugin(db_session):
    bundle = await create_bundle(db_session)
    instance = await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(
            endpoint="http://agent:19829",
            profile_name="operator-plugin",
            runtime_bundle_id=bundle.id,
        ),
    )

    before_report = await plugin_registry_service.list_plugin_installations(db_session)
    plugin_before = next(
        item for item in before_report if item.id == f"browser-runtime:{bundle.id}"
    )
    assert plugin_before.runtime_status == "BLOCKED"
    assert plugin_before.plugin_types == ["browser_runtime", "endpoint"]
    assert plugin_before.permissions["components"][0]["id"] == "bridge"

    await browser_service.report_runtime_deployment(db_session, instance, healthy_report())
    after_report = await plugin_registry_service.list_plugin_installations(db_session)
    plugin_after = next(item for item in after_report if item.id == f"browser-runtime:{bundle.id}")
    assert plugin_after.runtime_status == "READY"
    assert plugin_after.capabilities[0].key == "page.read"


@pytest.mark.asyncio
async def test_reassigning_a_bundle_requires_slot_restart(db_session):
    first = await create_bundle(db_session)
    next_manifest = manifest().model_dump()
    next_manifest["version"] = "1.8.6"
    second = await create_bundle(db_session, RuntimeBundleManifest.model_validate(next_manifest))
    instance = await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(
            endpoint="http://agent:19830",
            profile_name="operator-reassign",
            runtime_bundle_id=first.id,
        ),
    )
    await browser_service.report_runtime_deployment(db_session, instance, healthy_report())

    await browser_service.update_browser_instance(
        db_session,
        instance,
        BrowserInstanceConfigUpdate(runtime_bundle_id=second.id),
    )

    deployment = await browser_service.get_runtime_deployment(db_session, instance.id)
    assert deployment is not None
    assert deployment.state == "RESTART_REQUIRED"


@pytest.mark.asyncio
async def test_assigned_bundle_version_cannot_be_edited_in_place(db_session):
    bundle = await create_bundle(db_session)
    await browser_service.create_browser_instance(
        db_session,
        BrowserInstanceCreate(
            endpoint="http://agent-immutable:19823",
            profile_name="operator-immutable",
            runtime_bundle_id=bundle.id,
        ),
    )

    with pytest.raises(browser_service.BrowserRuntimeError) as excinfo:
        await browser_service.update_runtime_bundle(
            db_session,
            bundle,
            BrowserRuntimeBundleCreate(
                manifest=manifest(),
                trust_level="reviewed",
                source="replacement",
            ),
        )

    assert excinfo.value.code == "bundle_in_use"


def _resolver() -> Path:
    return Path(__file__).parents[2] / "scripts" / "resolve-browser-runtime-bundle.mjs"


def test_bundle_resolver_loads_only_manifest_allowlisted_extensions(tmp_path):
    root = tmp_path / "bundles"
    bundle = root / "research-default" / "4"
    bridge = bundle / "extensions" / "bridge"
    script_host = bundle / "extensions" / "script-host"
    bridge.mkdir(parents=True)
    script_host.mkdir(parents=True)
    (bridge / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Bridge", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (script_host / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Script Host", "version": "1.2.0"}),
        encoding="utf-8",
    )
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "name": "research-default",
                "version": "4",
                "components": [
                    {
                        "kind": "extension",
                        "id": "bridge",
                        "version": "0.1.0",
                        "path": "extensions/bridge",
                        "required": True,
                    },
                    {
                        "kind": "extension",
                        "id": "script-host",
                        "version": "1.2.0",
                        "path": "extensions/script-host",
                        "required": True,
                    },
                ],
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(_resolver()), str(bundle / "manifest.json"), str(root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(bridge.resolve()), str(script_host.resolve())]

    report_result = subprocess.run(
        [
            "node",
            str(_resolver()),
            str(bundle / "manifest.json"),
            str(root),
            "--report",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert report_result.returncode == 0, report_result.stderr
    report = SlotRuntimeReport.model_validate_json(report_result.stdout)
    assert report.loaded_bundle_name == "research-default"
    assert {component.id for component in report.loaded_components} == {
        "bridge",
        "script-host",
    }
    assert report.self_check == {"ok": True, "phase": "launcher-resolver"}


def test_bundle_resolver_rejects_component_version_drift(tmp_path):
    root = tmp_path / "bundles"
    bundle = root / "research-default" / "4"
    bridge = bundle / "extensions" / "bridge"
    bridge.mkdir(parents=True)
    (bridge / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "Bridge", "version": "0.1.0"}),
        encoding="utf-8",
    )
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "name": "research-default",
                "version": "4",
                "components": [
                    {
                        "kind": "extension",
                        "id": "bridge",
                        "version": "9.9.9",
                        "path": "extensions/bridge",
                        "required": True,
                    }
                ],
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(_resolver()), str(bundle / "manifest.json"), str(root)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "expected version 9.9.9, found 0.1.0" in result.stderr


def _opencli_default_v2_manifest() -> dict:
    return json.loads(
        (
            Path(__file__).parents[2]
            / "chrome"
            / "runtime-bundles"
            / "opencli-default"
            / "2"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )


def _materialize_opencli_default_v2_bundle(
    root: Path,
    *,
    absent_component_ids: set[str] | None = None,
    component_versions: dict[str, str] | None = None,
) -> tuple[Path, dict]:
    manifest = _opencli_default_v2_manifest()
    bundle = root / manifest["name"] / manifest["version"]
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    absent_component_ids = absent_component_ids or set()
    component_versions = component_versions or {}
    for component in manifest["components"]:
        if component["id"] in absent_component_ids:
            continue
        extension = bundle / component["path"]
        extension.mkdir(parents=True)
        (extension / "manifest.json").write_text(
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": component["id"],
                    "version": component_versions.get(
                        component["id"], component["version"]
                    ),
                }
            ),
            encoding="utf-8",
        )
    return bundle, manifest


def test_opencli_default_v2_resolver_loads_the_three_required_extensions(tmp_path):
    bundle, manifest = _materialize_opencli_default_v2_bundle(tmp_path / "bundles")

    result = subprocess.run(
        [
            "node",
            str(_resolver()),
            str(bundle / "manifest.json"),
            str(tmp_path / "bundles"),
            "--report",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = SlotRuntimeReport.model_validate_json(result.stdout)
    assert report.loaded_bundle_name == "opencli-default"
    assert report.loaded_bundle_version == "2"
    assert [(component.id, component.version) for component in report.loaded_components] == [
        (component["id"], component["version"]) for component in manifest["components"]
    ]


def test_opencli_default_v2_resolver_fails_when_violentmonkey_is_missing(tmp_path):
    bundle, _ = _materialize_opencli_default_v2_bundle(
        tmp_path / "bundles", absent_component_ids={"violentmonkey"}
    )

    result = subprocess.run(
        ["node", str(_resolver()), str(bundle / "manifest.json"), str(tmp_path / "bundles")],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 1
    assert "required component violentmonkey@2.48.0 is missing" in result.stderr


@pytest.mark.asyncio
async def test_opencli_default_v2_loaded_runtime_requires_violentmonkey_and_exact_versions(
    db_session,
):
    manifest_payload = _opencli_default_v2_manifest()
    bundle = await create_bundle(
        db_session, RuntimeBundleManifest.model_validate(manifest_payload)
    )
    instance = BrowserInstance(
        endpoint="http://agent-v2:19823",
        profile_name="operator-v2",
        runtime_bundle_id=bundle.id,
    )
    db_session.add(instance)
    await db_session.flush()

    base_report = {
        "loaded_bundle_name": "opencli-default",
        "loaded_bundle_version": "2",
        "loaded_components": [
            {
                "kind": component["kind"],
                "id": component["id"],
                "version": component["version"],
                "healthy": True,
            }
            for component in manifest_payload["components"]
        ],
        "capabilities": ["page.metadata"],
        "self_check": {
            "ok": True,
            "violentmonkey_user_scripts_access": {"ok": True},
        },
    }

    ready = await browser_service.report_runtime_deployment(
        db_session, instance, SlotRuntimeReport.model_validate(base_report)
    )
    assert ready.state == "READY"

    missing = await browser_service.report_runtime_deployment(
        db_session,
        instance,
        SlotRuntimeReport.model_validate(
            {
                **base_report,
                "loaded_components": [
                    component
                    for component in base_report["loaded_components"]
                    if component["id"] != "violentmonkey"
                ],
            }
        ),
    )
    assert missing.state == "EXTENSION_FAILED"
    assert missing.diagnostics == ["required extension 'violentmonkey' is not loaded"]

    version_drift = await browser_service.report_runtime_deployment(
        db_session,
        instance,
        SlotRuntimeReport.model_validate(
            {
                **base_report,
                "loaded_components": [
                    {
                        **component,
                        "version": "2.47.0",
                    }
                    if component["id"] == "violentmonkey"
                    else component
                    for component in base_report["loaded_components"]
                ],
            }
        ),
    )
    assert version_drift.state == "CONFIG_DRIFT"
    assert "violentmonkey" in version_drift.diagnostics[0]


def test_upgrade_from_opencli_default_v1_preserves_lineage_and_reassigns_slots(
    tmp_path,
):
    production_manifest = _opencli_default_v2_manifest()
    v1_bundle_id = "f4bce7f9-1df8-4e18-b671-37aa03230e93"
    v2_bundle_id = "b5b4d7d1-a2f7-4e53-92cf-9d85f9fca3bc"
    database = tmp_path / "migration.db"
    environment = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{database.as_posix()}",
    }

    before = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "l9m0n1o2p3q4"],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert before.returncode == 0, before.stderr

    connection = sqlite3.connect(database)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(browser_instances)")
        }
        values = {
            "id": "browser-runtime-v1",
            "created_at": "2026-08-29T00:00:00+00:00",
            "updated_at": "2026-08-29T00:00:00+00:00",
            "endpoint": "http://legacy-v1:19823",
            "mode": "bridge",
            "label": "",
            "agent_url": None,
            "agent_protocol": None,
            "profile_kind": "authenticated",
            "profile_name": "legacy-v1",
            "runtime_bundle_id": v1_bundle_id,
            "resource_class": "standard",
            "startup_pages": "[]",
            "network_policy": "{\"mode\":\"direct\"}",
        }
        insert_columns = [name for name in values if name in columns]
        connection.execute(
            "INSERT INTO browser_instances "
            f"({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in insert_columns)})",
            [values[name] for name in insert_columns],
        )
        connection.execute(
            "INSERT INTO browser_runtime_deployments "
            "(id, created_at, updated_at, browser_instance_id, loaded_bundle_name, "
            "loaded_bundle_version, loaded_components, self_check, state, diagnostics) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-v1-deployment",
                "2026-08-29T00:00:00+00:00",
                "2026-08-29T00:00:00+00:00",
                "browser-runtime-v1",
                "opencli-default",
                "1",
                "[]",
                "{\"ok\": true}",
                "READY",
                "[]",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    upgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert upgraded.returncode == 0, upgraded.stderr

    connection = sqlite3.connect(database)
    try:
        bundle_rows = connection.execute(
            "SELECT id, version, manifest FROM browser_runtime_bundles "
            "WHERE name = 'opencli-default' ORDER BY version"
        ).fetchall()
        loaded_bundle_id = connection.execute(
            "SELECT runtime_bundle_id FROM browser_instances "
            "WHERE endpoint = 'http://legacy-v1:19823'"
        ).fetchone()[0]
        deployment_state = connection.execute(
            "SELECT state FROM browser_runtime_deployments "
            "WHERE browser_instance_id = 'browser-runtime-v1'"
        ).fetchone()[0]
    finally:
        connection.close()

    bundles = {row[0]: (row[1], json.loads(row[2])) for row in bundle_rows}
    assert bundles[v1_bundle_id][0] == "1"
    assert bundles[v2_bundle_id] == ("2", production_manifest)
    assert loaded_bundle_id == v2_bundle_id
    assert deployment_state == "RESTART_REQUIRED"

    downgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "l9m0n1o2p3q4"],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )
    assert downgraded.returncode == 0, downgraded.stderr
    connection = sqlite3.connect(database)
    try:
        restored_bundle_id, restored_state = connection.execute(
            "SELECT browser_instances.runtime_bundle_id, browser_runtime_deployments.state "
            "FROM browser_instances JOIN browser_runtime_deployments "
            "ON browser_runtime_deployments.browser_instance_id = browser_instances.id "
            "WHERE browser_instances.id = 'browser-runtime-v1'"
        ).fetchone()
        assert restored_bundle_id == v1_bundle_id
        assert restored_state == "RESTART_REQUIRED"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM browser_runtime_bundles WHERE id = ?",
                (v2_bundle_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
