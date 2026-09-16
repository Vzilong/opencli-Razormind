from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend import ws_agent_manager
from backend.api.v1.operations_agents import router
from backend.database import get_db
from backend.models.edge_node import EdgeNode
from backend.models.identity import Team, User, Workspace, WorkspaceMembership, WorkspaceRole
from backend.models.operations_agent import (
    AgentProfileMode,
    OperationsAgentDraft,
    OperationsAgentIdentity,
    OperationsAgentRun,
    PublishedOperationsAgentVersion,
)
from backend.security.identity import RequestIdentity, get_request_identity


async def _client(db_session, subject: str) -> AsyncClient:
    app = FastAPI()
    app.include_router(router)

    async def override_db():
        yield db_session

    async def override_identity():
        return RequestIdentity(subject=subject)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_request_identity] = override_identity
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _seed_member(db_session, role: WorkspaceRole, suffix: str = "primary"):
    user = User(subject=f"{role.value}-{suffix}")
    workspace = Workspace(name=f"Workspace {suffix}", slug=f"workspace-{suffix}")
    db_session.add_all((user, workspace))
    await db_session.flush()
    team = Team(workspace_id=workspace.id, name="Operations", slug="operations")
    db_session.add_all(
        (
            team,
            WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role),
        )
    )
    await db_session.commit()
    return user, workspace, team


async def _add_member(db_session, workspace: Workspace, role: WorkspaceRole, suffix: str):
    user = User(subject=f"{role.value}-{suffix}")
    db_session.add(user)
    await db_session.flush()
    db_session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role=role))
    await db_session.commit()
    return user


def _agent_contract() -> dict:
    return {
        "schema_version": "agent.contract.v2",
        "role": "operations_reviewer",
        "input_schema": {
            "type": "object",
            "properties": {"target_id": {"type": "string"}},
            "required": ["target_id"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
        "state_schema": {
            "type": "object",
            "properties": {"last_target_id": {"type": "string"}},
        },
        "required_capabilities": ["streaming"],
        "tool_policy": {},
        "budget": {},
        "quality_gates": [],
        "evidence_requirements": [],
    }


async def _runtime_binding(db_session, monkeypatch) -> dict:
    agent_url = "http://agent-runtime.test:19823"
    db_session.add(
        EdgeNode(
            url=agent_url,
            label="Test runtime",
            protocol="ws",
            mode="bridge",
            node_type="shell",
            status="online",
            runtimes=["pi"],
            runtime_capabilities={"pi": ["streaming", "tool_events"]},
        )
    )
    await db_session.commit()
    monkeypatch.setattr(ws_agent_manager, "is_connected", lambda url: url == agent_url)
    return {
        "schema_version": "agent.runtime-binding.v2",
        "workflow": "operations-agent",
        "preferred_agent_urls": [agent_url],
        "preferred_runtimes": ["pi"],
        "model_binding": None,
        "config": {},
    }


async def test_maintainer_creates_agent_with_observe_only_profile(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Collector caretaker", "owning_team_id": team.id},
        )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["current_profile"]["version"] == 1
    assert data["current_profile"]["mode"] == AgentProfileMode.OBSERVE_ONLY
    assert data["disabled"] is False



async def test_maintainer_creates_agent_in_only_workspace_team_when_owner_is_omitted(
    db_session,
):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Runtime readiness advisor"},
        )

    assert response.status_code == 201
    assert response.json()["data"]["owning_team_id"] == team.id


async def test_multi_team_workspace_lists_choices_and_requires_explicit_owner(db_session):
    _, workspace, first_team = await _seed_member(
        db_session,
        WorkspaceRole.MAINTAINER,
    )
    second_team = Team(workspace_id=workspace.id, name="Research", slug="research")
    db_session.add(second_team)
    await db_session.commit()
    client = await _client(db_session, "maintainer-primary")

    async with client:
        teams = await client.get(
            f"/workspaces/{workspace.id}/operations-agents/teams"
        )
        omitted = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Needs explicit Team"},
        )

    assert teams.status_code == 200
    assert {row["id"] for row in teams.json()["data"]} == {
        first_team.id,
        second_team.id,
    }
    assert omitted.status_code == 422

async def test_maintainer_can_assign_suggest_but_not_automatic(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")
    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Planner", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        suggest = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={"mode": "suggest_changes", "reason": "May draft proposals"},
        )
        automatic = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={
                "mode": "low_risk_automatic",
                "tool_scope": ["plans.update_schedule"],
                "resource_scope": ["plan:daily-news"],
                "action_scope": ["schedule.adjust"],
                "reason": "Try automation",
            },
        )

    assert suggest.status_code == 201
    assert suggest.json()["data"]["version"] == 2
    assert automatic.status_code == 403


async def test_admin_assigns_narrow_automatic_profile_as_new_immutable_version(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    client = await _client(db_session, "admin-primary")
    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Automatic caretaker", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        assigned = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={
                "mode": "low_risk_automatic",
                "tool_scope": ["plans.update_schedule"],
                "resource_scope": ["plan:daily-news"],
                "action_scope": ["schedule.adjust"],
                "reason": "Qualified action enrollment",
            },
        )
        listed = await client.get(f"/workspaces/{workspace.id}/operations-agents")

    assert assigned.status_code == 201
    assert assigned.json()["data"] == {
        "version": 2,
        "mode": "low_risk_automatic",
        "tool_scope": ["plans.update_schedule"],
        "resource_scope": ["plan:daily-news"],
        "action_scope": ["schedule.adjust"],
        "assigned_by_user_id": assigned.json()["data"]["assigned_by_user_id"],
        "reason": "Qualified action enrollment",
        "created_at": assigned.json()["data"]["created_at"],
    }
    agent = listed.json()["data"][0]
    assert agent["current_profile"]["version"] == 2
    assert agent["current_profile"]["mode"] == "low_risk_automatic"


async def test_automatic_profile_requires_explicit_narrow_scopes(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    client = await _client(db_session, "admin-primary")
    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Unsafe caretaker", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={"mode": "low_risk_automatic", "reason": "Too broad"},
        )
        wildcard = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={
                "mode": "low_risk_automatic",
                "tool_scope": ["*"],
                "resource_scope": ["plan:daily-news"],
                "action_scope": ["schedule.adjust"],
                "reason": "Still too broad",
            },
        )

    assert response.status_code == 422
    assert wildcard.status_code == 422


async def test_operator_cannot_change_agent_profile(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    operator = await _add_member(db_session, workspace, WorkspaceRole.OPERATOR, "profile")
    admin_client = await _client(db_session, "admin-primary")
    async with admin_client:
        created = await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Operator boundary", "owning_team_id": team.id},
        )
    agent_id = created.json()["data"]["id"]

    operator_client = await _client(db_session, operator.subject)
    async with operator_client:
        response = await operator_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={"mode": "suggest_changes", "reason": "Not my permission"},
        )

    assert response.status_code == 403


async def test_disabling_agent_revokes_profile_and_prevents_new_assignments(
    db_session, monkeypatch
):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    runtime_binding = await _runtime_binding(db_session, monkeypatch)
    client = await _client(db_session, "admin-primary")
    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Retired caretaker", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Observe one target",
                "model_configuration": {
                    "agent_contract": _agent_contract(),
                    "runtime_binding": runtime_binding,
                },
            },
        )
        await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Ready for a manual run"},
        )
        started = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
                "input_payload": {"target_id": "daily-news"},
            },
        )
        disabled = await client.patch(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}",
            json={"disabled": True},
        )
        assignment = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={"mode": "suggest_changes", "reason": "Should not apply"},
        )

    assert disabled.status_code == 200
    assert disabled.json()["data"]["disabled"] is True
    assert disabled.json()["data"]["effective_profile"] is None
    assert assignment.status_code == 409
    assert await db_session.get(OperationsAgentIdentity, agent_id) is not None
    run = await db_session.get(OperationsAgentRun, started.json()["data"]["id"])
    await db_session.refresh(run)
    assert run.status == "cancelled"


async def test_maintainer_cannot_reenable_automatic_agent(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    maintainer = await _add_member(db_session, workspace, WorkspaceRole.MAINTAINER, "reenable")
    admin_client = await _client(db_session, "admin-primary")
    async with admin_client:
        created = await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Revoked automatic", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={
                "mode": "low_risk_automatic",
                "tool_scope": ["plans.update_schedule"],
                "resource_scope": ["plan:daily-news"],
                "action_scope": ["schedule.adjust"],
                "reason": "Qualified action enrollment",
            },
        )
        await admin_client.patch(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}",
            json={"disabled": True},
        )

    maintainer_client = await _client(db_session, maintainer.subject)
    async with maintainer_client:
        denied = await maintainer_client.patch(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}",
            json={"disabled": False},
        )
    admin_client = await _client(db_session, "admin-primary")
    async with admin_client:
        allowed = await admin_client.patch(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}",
            json={"disabled": False},
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200


async def test_agent_cannot_use_team_from_another_workspace(db_session):
    _, workspace, _ = await _seed_member(db_session, WorkspaceRole.ADMIN, "one")
    _, _, foreign_team = await _seed_member(db_session, WorkspaceRole.ADMIN, "two")
    client = await _client(db_session, "admin-one")
    async with client:
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Wrong owner", "owning_team_id": foreign_team.id},
        )

    assert response.status_code == 422


async def test_published_versions_are_immutable_snapshots_of_draft(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")
    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Versioned caretaker", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Observe failed collection runs",
                "model_configuration": {"model": {"name": "small"}},
                "tool_configuration": {"access": {"allowed": ["runs.read"]}},
            },
        )
        published = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Initial reviewed behavior"},
        )
        draft = await db_session.scalar(
            select(OperationsAgentDraft).where(OperationsAgentDraft.operations_agent_id == agent_id)
        )
        version = await db_session.scalar(
            select(PublishedOperationsAgentVersion).where(
                PublishedOperationsAgentVersion.operations_agent_id == agent_id
            )
        )
        draft.model_configuration["model"]["name"] = "mutated in memory"
        draft.tool_configuration["access"]["allowed"].append("proposals.create")
        assert version.model_configuration == {"model": {"name": "small"}}
        assert version.tool_configuration == {"access": {"allowed": ["runs.read"]}}
        edited = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 2,
                "instructions": "Suggest recovery changes",
                "model_configuration": {"model": "large"},
                "tool_configuration": {"allowed": ["runs.read", "proposals.create"]},
            },
        )

    assert published.status_code == 201
    assert published.json()["data"]["version"] == 1
    assert published.json()["data"]["draft_revision"] == 2
    assert published.json()["data"]["instructions"] == "Observe failed collection runs"
    assert edited.json()["data"]["revision"] == 3
    assert published.json()["data"]["model_configuration"] == {"model": {"name": "small"}}


async def test_agent_draft_reads_and_rejects_stale_revision(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Revision guarded", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        initial = await client.get(f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft")
        saved = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={"revision": 1, "instructions": "First reviewed draft"},
        )
        stale = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={"revision": 1, "instructions": "Stale overwrite"},
        )
        current = await client.get(f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft")

    assert initial.status_code == 200
    assert initial.json()["data"]["revision"] == 1
    assert saved.json()["data"]["revision"] == 2
    assert stale.status_code == 409
    assert stale.json()["detail"] == {
        "code": "operations_agent_draft_revision_conflict",
        "expected_revision": 1,
        "actual_revision": 2,
    }
    assert current.json()["data"]["instructions"] == "First reviewed draft"


async def test_agent_contract_v2_is_validated_and_published_in_versioned_configuration(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")
    contract = _agent_contract()

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Contracted caretaker", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        updated = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Inspect one target and return a summary",
                "model_configuration": {
                    "model": "small",
                    "agent_contract": contract,
                },
            },
        )
        published = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Typed boundary reviewed"},
        )
        versions = await client.get(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions"
        )
        selected_version = await client.get(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions/1"
        )

    assert updated.status_code == 200
    assert updated.json()["data"]["model_configuration"]["agent_contract"] == contract
    assert published.status_code == 201
    assert published.json()["data"]["model_configuration"] == {
        "model": "small",
        "agent_contract": contract,
    }
    assert versions.status_code == 200
    assert [version["version"] for version in versions.json()["data"]] == [1]
    assert selected_version.status_code == 200
    assert selected_version.json()["data"] == published.json()["data"]


async def test_agent_contract_v2_rejects_invalid_json_schema_before_draft_write(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")
    contract = _agent_contract()
    contract["input_schema"] = {"type": 42}

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Invalid contract", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        response = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "This must not be stored",
                "model_configuration": {"agent_contract": contract},
            },
        )
        contract["input_schema"] = {"$ref": "https://example.com/schema.json"}
        remote_reference = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Remote schemas must not be fetched",
                "model_configuration": {"agent_contract": contract},
            },
        )
        contract["input_schema"] = {"$dynamicRef": "https://example.com/schema.json#target"}
        remote_dynamic_reference = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Remote dynamic schemas must not be fetched",
                "model_configuration": {"agent_contract": contract},
            },
        )
        legacy_runtime_binding = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Legacy bindings must be migrated before use",
                "model_configuration": {
                    "runtime_binding": {
                        "schema_version": "agent.runtime-binding.v1",
                        "agent_url": "http://agent.test:19823",
                        "runtime": "bbx",
                        "workflow": "status",
                        "config": {},
                    }
                },
            },
        )
        unsafe_launch_config = await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Workspace config must not choose the edge process",
                "model_configuration": {
                    "runtime_binding": {
                        "schema_version": "agent.runtime-binding.v2",
                        "workflow": "status",
                        "preferred_agent_urls": ["http://agent.test:19823"],
                        "preferred_runtimes": ["pi"],
                        "model_binding": None,
                        "config": {
                            "binary": "python",
                            "args": ["-c", "print('unsafe')"],
                        },
                    }
                },
            },
        )

    assert response.status_code == 422
    assert remote_reference.status_code == 422
    assert remote_dynamic_reference.status_code == 422
    assert legacy_runtime_binding.status_code == 422
    assert unsafe_launch_config.status_code == 422
    draft = await db_session.scalar(
        select(OperationsAgentDraft).where(OperationsAgentDraft.operations_agent_id == agent_id)
    )
    assert draft.revision == 1
    assert draft.instructions == ""


async def test_publish_revalidates_persisted_agent_contract_v2(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Persisted invalid contract", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]

    draft = await db_session.scalar(
        select(OperationsAgentDraft).where(OperationsAgentDraft.operations_agent_id == agent_id)
    )
    draft.instructions = "Bypassed draft API"
    draft.model_configuration = {
        "agent_contract": {
            **_agent_contract(),
            "schema_version": "agent.contract.v3",
        }
    }
    await db_session.commit()

    client = await _client(db_session, "maintainer-primary")
    async with client:
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Must fail closed"},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent Draft contains invalid versioned configuration"
    assert (
        await db_session.scalar(
            select(PublishedOperationsAgentVersion).where(
                PublishedOperationsAgentVersion.operations_agent_id == agent_id
            )
        )
        is None
    )


async def test_agent_run_validates_and_persists_contract_input_and_state(db_session, monkeypatch):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    runtime_binding = await _runtime_binding(db_session, monkeypatch)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Contract bound runner", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Inspect the requested target",
                "model_configuration": {
                    "agent_contract": _agent_contract(),
                    "runtime_binding": runtime_binding,
                },
            },
        )
        await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Input boundary reviewed"},
        )
        invalid = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
                "input_payload": {},
            },
        )
        started = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
                "input_payload": {"target_id": "daily-news"},
                "state_payload": {"last_target_id": "yesterday"},
            },
        )

    assert invalid.status_code == 422
    assert "input_schema" in invalid.json()["detail"]
    assert started.status_code == 201
    assert started.json()["data"]["input_payload"] == {"target_id": "daily-news"}
    assert started.json()["data"]["state_payload"] == {"last_target_id": "yesterday"}
    assert started.json()["data"]["execution_binding"]["runtime"] == "pi"
    assert started.json()["data"]["evidence_payload"] is None
    stored = await db_session.get(OperationsAgentRun, started.json()["data"]["id"])
    assert stored.input_payload == {"target_id": "daily-news"}
    assert stored.state_payload == {"last_target_id": "yesterday"}


async def test_agent_run_requires_published_runtime_binding(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.MAINTAINER)
    client = await _client(db_session, "maintainer-primary")

    async with client:
        created = await client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Unbound runner", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Inspect one target",
                "model_configuration": {"agent_contract": _agent_contract()},
            },
        )
        await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Behavior only"},
        )
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
            },
        )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Published Agent Version requires an AgentRuntimeBindingV2"
    )


async def test_automatic_agent_behavior_release_requires_admin_and_governed_gateway(
    db_session, monkeypatch
):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    runtime_binding = await _runtime_binding(db_session, monkeypatch)
    maintainer = await _add_member(db_session, workspace, WorkspaceRole.MAINTAINER, "publisher")
    admin_client = await _client(db_session, "admin-primary")
    async with admin_client:
        created = await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Guarded release", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await admin_client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Adjust qualified schedules",
                "model_configuration": {
                    "agent_contract": _agent_contract(),
                    "runtime_binding": runtime_binding,
                },
            },
        )
        await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={
                "mode": "low_risk_automatic",
                "tool_scope": ["plans.update_schedule"],
                "resource_scope": ["plan:daily-news"],
                "action_scope": ["schedule.adjust"],
                "reason": "Qualified action enrollment",
            },
        )

    maintainer_client = await _client(db_session, maintainer.subject)
    async with maintainer_client:
        denied = await maintainer_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Maintainer review"},
        )
    admin_publish_client = await _client(db_session, "admin-primary")
    async with admin_publish_client:
        allowed = await admin_publish_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Admin reviewed automatic behavior"},
        )
        blocked_run = await admin_publish_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
                "input_payload": {"target_id": "daily-news"},
            },
        )

    assert denied.status_code == 403
    assert allowed.status_code == 201
    assert blocked_run.status_code == 409
    assert blocked_run.json()["detail"] == (
        "Automatic Operations Agent runs require the governed action gateway"
    )


async def test_operator_run_binds_published_and_profile_versions_then_pauses(
    db_session, monkeypatch
):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.ADMIN)
    runtime_binding = await _runtime_binding(db_session, monkeypatch)
    operator = await _add_member(db_session, workspace, WorkspaceRole.OPERATOR, "runner")
    admin_client = await _client(db_session, "admin-primary")
    async with admin_client:
        created = await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Bound runner", "owning_team_id": team.id},
        )
        agent_id = created.json()["data"]["id"]
        await admin_client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 1,
                "instructions": "Inspect one target",
                "model_configuration": {
                    "agent_contract": _agent_contract(),
                    "runtime_binding": runtime_binding,
                },
            },
        )
        await admin_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Ready for manual runs"},
        )

    operator_client = await _client(db_session, operator.subject)
    async with operator_client:
        started = await operator_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={
                "target_resource_type": "plan",
                "target_resource_id": "daily-news",
                "input_payload": {"target_id": "daily-news"},
            },
        )
        run_id = started.json()["data"]["id"]
    admin_profile_client = await _client(db_session, "admin-primary")
    async with admin_profile_client:
        await admin_profile_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/profiles",
            json={"mode": "suggest_changes", "reason": "Expanded after run start"},
        )
        await admin_profile_client.put(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/draft",
            json={
                "revision": 2,
                "instructions": "Inspect two targets",
                "model_configuration": {
                    "agent_contract": _agent_contract(),
                    "runtime_binding": runtime_binding,
                },
            },
        )
        await admin_profile_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/versions",
            json={"reason": "Expanded after run start"},
        )
    operator_pause_client = await _client(db_session, operator.subject)
    async with operator_pause_client:
        paused = await operator_pause_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs/{run_id}/pause"
        )

    assert started.status_code == 201
    assert started.json()["data"]["published_version"] == 1
    assert started.json()["data"]["profile_version"] == 1
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == "paused"
    stored = await db_session.get(OperationsAgentRun, run_id)
    assert stored is not None
    assert stored.profile_version == 1
    assert stored.published_version == 1


async def test_agent_ids_are_scoped_to_workspace(db_session):
    _, workspace, _ = await _seed_member(db_session, WorkspaceRole.ADMIN, "one")
    _, foreign_workspace, foreign_team = await _seed_member(db_session, WorkspaceRole.ADMIN, "two")
    foreign_client = await _client(db_session, "admin-two")
    async with foreign_client:
        foreign_agent = await foreign_client.post(
            f"/workspaces/{foreign_workspace.id}/operations-agents",
            json={"name": "Foreign agent", "owning_team_id": foreign_team.id},
        )

    client = await _client(db_session, "admin-one")
    async with client:
        response = await client.post(
            f"/workspaces/{workspace.id}/operations-agents/"
            f"{foreign_agent.json()['data']['id']}/profiles",
            json={"mode": "suggest_changes", "reason": "Cross-workspace attempt"},
        )

    assert response.status_code == 404


async def test_agent_without_published_version_cannot_run(db_session):
    _, workspace, team = await _seed_member(db_session, WorkspaceRole.OPERATOR)
    maintainer = await _add_member(db_session, workspace, WorkspaceRole.MAINTAINER, "creator")
    maintainer_client = await _client(db_session, maintainer.subject)
    async with maintainer_client:
        created = await maintainer_client.post(
            f"/workspaces/{workspace.id}/operations-agents",
            json={"name": "Unpublished", "owning_team_id": team.id},
        )
    agent_id = created.json()["data"]["id"]

    operator_client = await _client(db_session, "operator-primary")
    async with operator_client:
        response = await operator_client.post(
            f"/workspaces/{workspace.id}/operations-agents/{agent_id}/runs",
            json={"target_resource_type": "plan", "target_resource_id": "daily-news"},
        )

    assert response.status_code == 409


