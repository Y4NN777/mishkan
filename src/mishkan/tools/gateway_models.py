"""Invocation, target, result, and audit envelopes for governed capabilities."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mishkan.domain.identity import DomainRecord
from mishkan.domain.time import require_aware
from mishkan.policy.models import AuthorizationDecision, EffectivePolicy, ResourceRequest
from mishkan.tools.models import RegistrySnapshot, ToolBinding


class GatewayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CallStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
    REFUSED = "refused"


class DeclaredTargets(GatewayModel):
    paths: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    remotes: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    external_resources: tuple[str, ...] = ()


class ResolvedPath(GatewayModel):
    requested: str
    lexical_relative: str = ""
    relative: str
    absolute: Path
    link_chain: tuple[str, ...] = ()


class ResolvedTargets(GatewayModel):
    paths: tuple[ResolvedPath, ...] = ()
    executables: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    repositories: tuple[str, ...] = ()
    remotes: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    external_resources: tuple[str, ...] = ()


class InvocationContext(GatewayModel):
    run_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    identity: str = Field(min_length=1)
    objective_class: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    outcome: str = Field(min_length=1)
    role: str = Field(min_length=1)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    registry: RegistrySnapshot
    binding: ToolBinding
    policy: EffectivePolicy
    resources: ResourceRequest
    isolation_profile: str | None = None


class InvocationEnvelope(DomainRecord):
    run_id: str
    task_attempt_id: str
    acting_identity: str
    tool_id: str
    tool_version: str
    registry_fingerprint: str = Field(min_length=64, max_length=64)
    plan_fingerprint: str = Field(min_length=64, max_length=64)
    policy_fingerprint: str = Field(min_length=64, max_length=64)
    normalized_arguments: dict[str, Any]
    declared_targets: DeclaredTargets
    authorization: AuthorizationDecision
    deadline: datetime

    @field_validator("deadline")
    @classmethod
    def deadline_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class AdapterResult(GatewayModel):
    output: dict[str, Any]
    actual_targets: ResolvedTargets
    external_references: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)


class ToolResultEnvelope(DomainRecord):
    call_id: str
    run_id: str
    task_attempt_id: str
    tool_id: str
    tool_version: str
    status: CallStatus
    started_at: datetime
    completed_at: datetime
    output: dict[str, Any] | None = None
    actual_targets: ResolvedTargets | None = None
    external_references: tuple[str, ...] = ()
    retryable: bool = False
    adapter_evidence: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    reason: str

    @field_validator("started_at", "completed_at")
    @classmethod
    def result_times_are_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class AuditEvent(DomainRecord):
    event_type: str = Field(min_length=1)
    run_id: str
    task_attempt_id: str
    call_id: str | None = None
    identity: str
    capability: str
    decision: str
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
