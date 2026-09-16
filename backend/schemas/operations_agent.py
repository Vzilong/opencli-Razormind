import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from referencing.exceptions import Unresolvable

from backend.models.operations_agent import AgentProfileMode
from backend.schemas.common import UTCModel

AGENT_CONTRACT_CONFIGURATION_KEY = "agent_contract"
AGENT_RUNTIME_BINDING_CONFIGURATION_KEY = "runtime_binding"
DEFAULT_DEEP_RUN_TIMEOUT_SECONDS = 1800
MAX_DEEP_RUN_TIMEOUT_SECONDS = 3600
MAX_AGENT_SCHEMA_BYTES = 65_536
MAX_AGENT_SCHEMA_DEPTH = 32
MAX_AGENT_MODEL_CONFIGURATION_BYTES = 262_144


class AgentQualityGateV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=100)
    required: bool = True
    config: dict[str, JsonValue] = Field(default_factory=dict)


class AgentContractV2(BaseModel):
    """Runtime-neutral business role, I/O, policy, and evidence boundary."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent.contract.v2"]
    role: str = Field(min_length=1, max_length=100)
    input_schema: dict[str, JsonValue]
    output_schema: dict[str, JsonValue]
    state_schema: dict[str, JsonValue]
    required_capabilities: list[str] = Field(default_factory=list, max_length=64)
    tool_policy: dict[str, JsonValue] = Field(default_factory=dict)
    budget: dict[str, JsonValue] = Field(default_factory=dict)
    quality_gates: list[AgentQualityGateV1] = Field(default_factory=list, max_length=64)
    evidence_requirements: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("input_schema", "output_schema", "state_schema")
    @classmethod
    def schemas_are_valid_json_schema(cls, schema: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if len(json.dumps(schema, allow_nan=False).encode("utf-8")) > MAX_AGENT_SCHEMA_BYTES:
            raise ValueError("JSON Schema exceeds 65536 bytes")
        stack: list[tuple[JsonValue, int]] = [(schema, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > MAX_AGENT_SCHEMA_DEPTH:
                raise ValueError("JSON Schema exceeds maximum nesting depth")
            if isinstance(value, dict):
                for keyword in ("$ref", "$dynamicRef"):
                    reference = value.get(keyword)
                    if isinstance(reference, str) and not reference.startswith("#"):
                        raise ValueError("remote JSON Schema references are not supported")
                stack.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                stack.extend((child, depth + 1) for child in value)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise ValueError(f"invalid JSON Schema: {exc.message}") from exc
        return schema

    @field_validator("required_capabilities", "evidence_requirements")
    @classmethod
    def identifiers_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(
            not value
            or len(value) > 64
            or not value.replace(".", "_").replace("-", "_").replace("_", "").isalnum()
            for value in normalized
        ):
            raise ValueError("capability and evidence identifiers must be simple names")
        if len(set(normalized)) != len(normalized):
            raise ValueError("capability and evidence identifiers must be unique")
        return normalized

    @model_validator(mode="after")
    def quality_gate_ids_are_unique(self):
        ids = [gate.id for gate in self.quality_gates]
        if len(set(ids)) != len(ids):
            raise ValueError("quality gate ids must be unique")
        return self


class AgentRuntimeBindingV1(BaseModel):
    """Published binding from one Operations Agent version to an existing edge runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent.runtime-binding.v1"]
    agent_url: str = Field(min_length=1, max_length=512)
    runtime: Literal["miniflow", "pi", "codex"]
    workflow: str = Field(min_length=1, max_length=255)
    config: dict[str, JsonValue] = Field(default_factory=dict)
    dispatch_timeout_seconds: int = Field(
        default=DEFAULT_DEEP_RUN_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_DEEP_RUN_TIMEOUT_SECONDS,
    )
    execution_node_url: str | None = Field(default=None, min_length=1, max_length=512)
    shared_filesystem_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @field_validator("config")
    @classmethod
    def config_is_task_scoped(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        unsupported = sorted(set(value) - {"timeout_seconds"})
        if unsupported:
            raise ValueError(
                "runtime config is Fleet-owned; unsupported task keys: "
                + ", ".join(unsupported)
            )
        timeout = value.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 1 <= timeout <= MAX_DEEP_RUN_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "config.timeout_seconds must be between 1 and "
                f"{MAX_DEEP_RUN_TIMEOUT_SECONDS}"
            )
        return value

    @field_validator("agent_url")
    @classmethod
    def agent_url_is_http(cls, value: str) -> str:
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("agent_url must be an http/https URL")
        return normalized

    @field_validator("execution_node_url")
    @classmethod
    def execution_node_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("execution_node_url must be an http/https URL")
        return normalized


class AgentModelBindingV1(BaseModel):
    """Non-secret model selection; credentials are resolved by the edge runtime."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent.model-binding.v1"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=255)
    auth_profile: str | None = Field(default=None, min_length=1, max_length=255)


class AgentRuntimeBindingV2(BaseModel):
    """Capability policy for selecting an edge runtime at dispatch time."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent.runtime-binding.v2"]
    workflow: str = Field(min_length=1, max_length=255)
    preferred_agent_urls: list[str] = Field(default_factory=list, max_length=32)
    preferred_runtimes: list[str] = Field(default_factory=list, max_length=32)
    model_binding: AgentModelBindingV1 | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    dispatch_timeout_seconds: int = Field(
        default=DEFAULT_DEEP_RUN_TIMEOUT_SECONDS,
        ge=1,
        le=MAX_DEEP_RUN_TIMEOUT_SECONDS,
    )

    @field_validator("preferred_agent_urls")
    @classmethod
    def agent_urls_are_http(cls, values: list[str]) -> list[str]:
        normalized = [value.rstrip("/") for value in values]
        if any(not value.startswith(("http://", "https://")) for value in normalized):
            raise ValueError("preferred_agent_urls must contain only http/https URLs")
        if len(set(normalized)) != len(normalized):
            raise ValueError("preferred_agent_urls must be unique")
        return normalized

    @field_validator("preferred_runtimes")
    @classmethod
    def runtimes_are_normalized(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 100 for value in normalized):
            raise ValueError("preferred_runtimes must contain non-empty names")
        if len(set(normalized)) != len(normalized):
            raise ValueError("preferred_runtimes must be unique")
        return normalized

    @field_validator("config")
    @classmethod
    def config_is_task_scoped(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        unsupported = sorted(set(value) - {"timeout_seconds"})
        if unsupported:
            raise ValueError(
                "runtime config is Fleet-owned; unsupported task keys: "
                + ", ".join(unsupported)
            )
        timeout = value.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not 1 <= timeout <= MAX_DEEP_RUN_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "config.timeout_seconds must be between 1 and "
                f"{MAX_DEEP_RUN_TIMEOUT_SECONDS}"
            )
        return value


def validated_agent_model_configuration(model_configuration: dict[str, Any]) -> dict[str, Any]:
    """Return a detached configuration with canonical versioned agent contracts."""

    configuration = deepcopy(model_configuration)
    if AGENT_CONTRACT_CONFIGURATION_KEY in configuration:
        configuration[AGENT_CONTRACT_CONFIGURATION_KEY] = AgentContractV2.model_validate(
            configuration[AGENT_CONTRACT_CONFIGURATION_KEY]
        ).model_dump(mode="json")
    if AGENT_RUNTIME_BINDING_CONFIGURATION_KEY in configuration:
        configuration[AGENT_RUNTIME_BINDING_CONFIGURATION_KEY] = (
            AgentRuntimeBindingV2.model_validate(
                configuration[AGENT_RUNTIME_BINDING_CONFIGURATION_KEY]
            ).model_dump(mode="json")
        )
    if (
        len(json.dumps(configuration, allow_nan=False).encode("utf-8"))
        > MAX_AGENT_MODEL_CONFIGURATION_BYTES
    ):
        raise ValueError("Operations Agent model_configuration exceeds 262144 bytes")
    return configuration


def agent_contract_from_model_configuration(
    model_configuration: dict[str, Any],
) -> AgentContractV2 | None:
    contract = model_configuration.get(AGENT_CONTRACT_CONFIGURATION_KEY)
    return None if contract is None else AgentContractV2.model_validate(contract)


def agent_runtime_binding_from_model_configuration(
    model_configuration: dict[str, Any],
) -> AgentRuntimeBindingV2 | None:
    binding = model_configuration.get(AGENT_RUNTIME_BINDING_CONFIGURATION_KEY)
    return None if binding is None else AgentRuntimeBindingV2.model_validate(binding)


def validate_agent_contract_payload(
    contract: AgentContractV2,
    schema_field: Literal["input_schema", "output_schema", "state_schema"],
    payload: dict[str, JsonValue],
) -> None:
    try:
        error = next(
            Draft202012Validator(getattr(contract, schema_field)).iter_errors(payload),
            None,
        )
    except Unresolvable as exc:
        raise ValueError(f"{schema_field}: local schema reference cannot be resolved") from exc
    if error is None:
        return
    path = ".".join(str(part) for part in error.absolute_path)
    location = f" at {path}" if path else ""
    raise ValueError(f"{schema_field}{location}: {error.message}")


class OperationsAgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4000)
    owning_team_id: str | None = None


class OperationsAgentPatch(BaseModel):
    disabled: bool


class OperationsAgentDraftUpdate(BaseModel):
    revision: int = Field(ge=1)
    instructions: str = Field(min_length=1, max_length=20000)
    model_configuration: dict[str, JsonValue] = Field(default_factory=dict)
    tool_configuration: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("model_configuration")
    @classmethod
    def validate_agent_contract(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validated_agent_model_configuration(value)


class OperationsAgentDraftRead(UTCModel):
    revision: int
    instructions: str
    model_configuration: dict
    tool_configuration: dict
    updated_by_user_id: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class OperationsAgentPublish(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class PublishedOperationsAgentVersionRead(UTCModel):
    version: int
    draft_revision: int
    instructions: str
    model_configuration: dict
    tool_configuration: dict
    published_by_user_id: str
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OperationsAgentRunCreate(BaseModel):
    target_resource_type: str = Field(min_length=1, max_length=100)
    target_resource_id: str = Field(min_length=1, max_length=255)
    input_payload: dict[str, JsonValue] = Field(default_factory=dict)
    state_payload: dict[str, JsonValue] = Field(default_factory=dict)


class AgentRunEvidenceEnvelopeV1(BaseModel):
    """Runtime-independent event, artifact, evidence, lineage, and audit record."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["agent.run-evidence.v1"] = "agent.run-evidence.v1"
    runtime: dict[str, JsonValue]
    events: list[dict[str, JsonValue]] = Field(default_factory=list)
    artifacts: list[dict[str, JsonValue]] = Field(default_factory=list)
    evidence: list[dict[str, JsonValue]] = Field(default_factory=list)
    lineage: list[dict[str, JsonValue]] = Field(default_factory=list)
    audit: list[dict[str, JsonValue]] = Field(default_factory=list)


class OperationsAgentRunRead(UTCModel):
    id: str
    workspace_id: str
    operations_agent_id: str
    published_version: int
    profile_version: int
    trigger_type: str
    trigger_reference: str | None
    automation_id: str | None
    automation_revision: int | None
    automation_snapshot: dict[str, JsonValue] | None
    scheduled_for: datetime | None
    schedule_timezone: str | None
    target_resource_type: str
    target_resource_id: str
    input_payload: dict[str, JsonValue]
    state_payload: dict[str, JsonValue]
    output_payload: dict[str, JsonValue] | None
    execution_binding: dict[str, JsonValue] | None
    evidence_payload: AgentRunEvidenceEnvelopeV1 | None
    error_message: str | None
    status: str
    started_by_user_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AgentProfileCreate(BaseModel):
    mode: AgentProfileMode
    tool_scope: list[str] = Field(default_factory=list)
    resource_scope: list[str] = Field(default_factory=list)
    action_scope: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def automatic_profile_is_explicitly_scoped(self):
        scopes = (self.tool_scope, self.resource_scope, self.action_scope)
        if self.mode == AgentProfileMode.LOW_RISK_AUTOMATIC and (
            not all(scopes) or any("*" in value for scope in scopes for value in scope)
        ):
            raise ValueError(
                "Low-Risk Automatic requires explicit tool, resource, and action scopes"
            )
        return self


class AgentProfileRead(UTCModel):
    version: int
    mode: str
    tool_scope: list[str]
    resource_scope: list[str]
    action_scope: list[str]
    assigned_by_user_id: str
    reason: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OperationsAgentTeamRead(UTCModel):
    id: str
    workspace_id: str
    name: str
    slug: str
    created_at: datetime

    model_config = {"from_attributes": True}


class OperationsAgentRead(UTCModel):
    id: str
    workspace_id: str
    owning_team_id: str
    name: str
    description: str | None
    disabled: bool
    current_published_version: int | None
    current_profile: AgentProfileRead
    effective_profile: AgentProfileRead | None
    created_at: datetime
    updated_at: datetime
