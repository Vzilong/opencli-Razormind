from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend import ws_agent_manager
from backend.models.automation import Automation
from backend.models.edge_node import EdgeNode
from backend.models.identity import Team, User, Workspace
from backend.models.operations_agent import (
    AgentPermissionProfile,
    AgentProfileMode,
    OperationsAgentIdentity,
    OperationsAgentRun,
    PublishedOperationsAgentVersion,
)
from backend.services import automation_schedule_service as service
from backend.services import scheduled_run_recovery as recovery


async def _seed_bound_automation(
    db_session,
    *,
    profile_mode: AgentProfileMode = AgentProfileMode.OBSERVE_ONLY,
    runtime_capabilities: dict[str, list[str]] | None = None,
    input_schema: dict | None = None,
):
    user = User(subject=f"scheduler-{profile_mode.value}")
    workspace = Workspace(name="Scheduled", slug=f"scheduled-{profile_mode.value}")
    db_session.add_all((user, workspace))
    await db_session.flush()
    team = Team(workspace_id=workspace.id, name="Operations", slug="operations")
    db_session.add(team)
    await db_session.flush()
    agent = OperationsAgentIdentity(
        workspace_id=workspace.id,
        owning_team_id=team.id,
        name="Read-only scheduler agent",
        current_profile_version=1,
        current_published_version=1,
    )
    db_session.add(agent)
    await db_session.flush()
    profile = AgentPermissionProfile(
        operations_agent_id=agent.id,
        version=1,
        mode=profile_mode,
        assigned_by_user_id=user.id,
        reason="test profile",
    )
    version = PublishedOperationsAgentVersion(
        operations_agent_id=agent.id,
        version=1,
        draft_revision=1,
        instructions="Read only",
        model_configuration={
            "agent_contract": {
                "schema_version": "agent.contract.v2",
                "role": "operations_reviewer",
                "input_schema": input_schema or {"type": "object"},
                "output_schema": {"type": "object"},
                "state_schema": {"type": "object"},
                "required_capabilities": ["streaming"],
                "tool_policy": {},
                "budget": {},
                "quality_gates": [],
                "evidence_requirements": [],
            },
            "runtime_binding": {
                "schema_version": "agent.runtime-binding.v2",
                "workflow": "builtin.read_only_readiness",
                "preferred_agent_urls": ["http://scheduler-agent:19823"],
                "preferred_runtimes": ["miniflow"],
                "model_binding": None,
                "config": {"timeout_seconds": 60},
                "dispatch_timeout_seconds": 60,
            },
        },
        tool_configuration={},
        published_by_user_id=user.id,
        reason="test version",
    )
    node = EdgeNode(
        url="http://scheduler-agent:19823",
        label="Scheduler runtime",
        protocol="ws",
        mode="cdp",
        node_type="docker",
        status="online",
        runtimes=list((runtime_capabilities or {"miniflow": ["streaming"]}).keys()),
        runtime_capabilities=runtime_capabilities or {"miniflow": ["streaming"]},
    )
    automation = Automation(
        workspace_id=workspace.id,
        revision=1,
        operations_agent_id=agent.id,
        operations_agent_version=1,
        name="Daily readiness",
        prompt="Check readiness without mutations",
        precheck=None,
        executor="operations-agent",
        schedule="daily@12:00",
        session_mode="fresh",
        approval_mode=profile_mode.value,
        project={},
        enabled=True,
        created_by_user_id=user.id,
    )
    db_session.add_all((profile, version, node, automation))
    await db_session.commit()
    return automation, agent, profile


@pytest_asyncio.fixture
async def scheduler_factory(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    monkeypatch.setattr(service, "AsyncSessionLocal", factory)
    monkeypatch.setattr(recovery, "AsyncSessionLocal", factory)
    monkeypatch.setattr(service, "schedule_operations_agent_run", lambda _run_id: None)
    return factory


async def test_duplicate_scans_claim_one_durable_occurrence(
    db_session,
    scheduler_factory,
):
    automation, agent, profile = await _seed_bound_automation(db_session)
    scheduled_for = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    first, first_created = await service.claim_scheduled_automation_run(
        automation.id,
        scheduled_for,
    )
    second, second_created = await service.claim_scheduled_automation_run(
        automation.id,
        scheduled_for,
    )

    assert first is not None and second is not None
    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert first.trigger_type == "scheduled"
    assert first.trigger_reference == f"automation:{automation.id}:20260824T1200Z"
    assert first.automation_revision == 1
    assert first.automation_snapshot["name"] == "Daily readiness"
    assert first.scheduled_for == scheduled_for
    assert first.schedule_timezone == "UTC"
    assert first.operations_agent_id == agent.id
    assert await recovery.list_queued_scheduled_run_ids() == [first.id]
    assert first.execution_binding["runtime"] == "miniflow"
    await recovery.recover_operations_agent_runs_on_startup()
    persisted = await db_session.get(OperationsAgentRun, first.id)
    assert persisted is not None and persisted.status == "queued"
    assert first.published_version == 1
    assert first.profile_version == profile.version
    count = await db_session.scalar(
        select(func.count()).select_from(OperationsAgentRun).where(
            OperationsAgentRun.automation_id == automation.id
        )
    )
    assert count == 1


async def test_pause_recheck_prevents_claim(
    db_session,
    scheduler_factory,
):
    automation, _, _ = await _seed_bound_automation(db_session)
    automation.enabled = False
    await db_session.commit()

    run, created = await service.claim_scheduled_automation_run(
        automation.id,
        datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    assert run is None
    assert created is False




async def test_manual_run_rejects_offline_bound_runtime(
    db_session,
    monkeypatch,
):
    automation, _, _ = await _seed_bound_automation(db_session)
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda _url: False)

    with pytest.raises(service.AutomationBindingError, match="no connected"):
        await service.create_bound_automation_run(
            db_session,
            automation,
            trigger_type="manual",
            started_by_user_id=automation.created_by_user_id,
        )

    count = await db_session.scalar(
        select(func.count()).select_from(OperationsAgentRun).where(
            OperationsAgentRun.automation_id == automation.id
        )
    )
    assert count == 0


async def test_scheduled_offline_runtime_remains_queued_for_recovery(
    db_session,
    scheduler_factory,
    monkeypatch,
):
    automation, _, _ = await _seed_bound_automation(db_session)
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda _url: False)

    run, created = await service.claim_scheduled_automation_run(
        automation.id,
        datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    assert run is not None and created is True
    assert run.status == "queued"
    assert await recovery.list_queued_scheduled_run_ids() == [run.id]


async def test_structural_binding_failure_persists_one_failed_occurrence(
    db_session,
    scheduler_factory,
):
    automation, agent, profile = await _seed_bound_automation(
        db_session,
        runtime_capabilities={"pi": []},
    )
    scheduled_for = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    first, first_created = await service.claim_scheduled_automation_run(
        automation.id,
        scheduled_for,
    )
    second, second_created = await service.claim_scheduled_automation_run(
        automation.id,
        scheduled_for,
    )

    assert first is not None and second is not None
    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert first.status == "failed"
    assert "required capabilities" in first.error_message
    assert first.trigger_reference == f"automation:{automation.id}:20260824T1200Z"
    assert first.scheduled_for == scheduled_for
    assert first.automation_revision == automation.revision
    assert first.automation_snapshot["id"] == automation.id
    assert first.operations_agent_id == agent.id
    assert first.published_version == automation.operations_agent_version
    assert first.profile_version == profile.version
    count = await db_session.scalar(
        select(func.count()).select_from(OperationsAgentRun).where(
            OperationsAgentRun.automation_id == automation.id,
            OperationsAgentRun.scheduled_for == scheduled_for,
        )
    )
    assert count == 1
async def test_low_risk_automatic_profile_is_incompatible(db_session):
    automation, _, _ = await _seed_bound_automation(
        db_session,
        profile_mode=AgentProfileMode.LOW_RISK_AUTOMATIC,
    )

    with pytest.raises(service.AutomationBindingError, match="Low-Risk Automatic"):
        await service.validate_automation_binding(db_session, automation)


async def test_runtime_must_satisfy_agent_capabilities(db_session):
    automation, _, _ = await _seed_bound_automation(
        db_session,
        runtime_capabilities={"pi": []},
    )

    with pytest.raises(service.AutomationBindingError, match="required capabilities"):
        await service.validate_automation_binding(db_session, automation)


async def test_automation_payload_must_satisfy_pinned_agent_contract(db_session):
    automation, _, _ = await _seed_bound_automation(
        db_session,
        input_schema={
            "type": "object",
            "required": ["unsupported_required_field"],
        },
    )

    with pytest.raises(service.AutomationBindingError, match="incompatible"):
        await service.validate_automation_binding(db_session, automation)
