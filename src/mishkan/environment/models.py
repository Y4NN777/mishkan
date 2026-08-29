"""Versioned contracts separating environment evidence, decisions, and readiness."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.config.models import CredentialReference
from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now
from mishkan.edits import ChangeSet
from mishkan.tools.execution import EffectSettlement, ExecutionRequest, ExecutionStatus


class EnvironmentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


AvailabilityDimension = Literal[
    "inventoried",
    "detected",
    "installed",
    "executable",
    "authenticated",
    "healthy",
    "project_used",
    "eligible",
    "authorized",
]


class AvailabilityState(StrEnum):
    TRUE = "true"
    FALSE = "false"
    UNKNOWN = "unknown"


class EnvironmentOutcome(StrEnum):
    REUSE_EXISTING = "reuse_existing"
    HOST_NATIVE = "host_native"
    GENERATE = "generate"
    PROPOSE_PROJECT_CHANGE = "propose_project_change"
    UNRESOLVED = "unresolved"


class EnvironmentBindingState(StrEnum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNRESOLVED = "unresolved"
    STALE = "stale"


class EnvironmentSettlement(StrEnum):
    VERIFIED = "verified"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"


class EnvironmentOperation(StrEnum):
    VALIDATE = "validate"
    BUILD = "build"
    MATERIALIZE = "materialize"
    START = "start"
    READINESS = "readiness"
    STOP = "stop"
    CLEANUP = "cleanup"


class AvailabilityFact(EnvironmentModel):
    dimension: AvailabilityDimension
    state: AvailabilityState
    evidence: str = Field(min_length=1, max_length=2_048)
    source: str = Field(min_length=1, max_length=512)
    observed_at: datetime = Field(default_factory=utc_now)
    freshness_seconds: int = Field(ge=1, le=31_536_000)
    sensitivity: Literal["portable", "machine_local"]

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class EngineObservation(EnvironmentModel):
    engine_id: str = Field(min_length=1, max_length=128)
    adapter_id: str | None = Field(default=None, min_length=1, max_length=256)
    executable_name: str | None = Field(default=None, min_length=1, max_length=256)
    executable_path: Path | None = None
    version: str | None = Field(default=None, min_length=1, max_length=512)
    semantics: tuple[str, ...] = Field(min_length=1)
    platforms: tuple[str, ...] = Field(min_length=1)
    facts: tuple[AvailabilityFact, ...] = Field(min_length=9, max_length=9)
    safe_probe: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def dimensions_are_independent_and_complete(self) -> EngineObservation:
        dimensions = [fact.dimension for fact in self.facts]
        if len(dimensions) != len(set(dimensions)) or len(dimensions) != 9:
            raise ValueError("engine observation must contain every independent state once")
        if self.executable_path is not None and self.executable_name is None:
            raise ValueError("observed executable path requires its executable name")
        return self

    def fact(self, dimension: str) -> AvailabilityState:
        return next(item.state for item in self.facts if item.dimension == dimension)


class DescriptorObservation(EnvironmentModel):
    format: str = Field(min_length=1, max_length=128)
    logical_path: str = Field(min_length=1, max_length=1_024)
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    repository_revision: str = Field(min_length=1, max_length=512)
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("observed_at")
    @classmethod
    def descriptor_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class EnvironmentObservationRequest(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    observation_id: UUID = Field(default_factory=new_id)
    actor_identity: str = Field(min_length=1, max_length=256)
    context_id: str = Field(min_length=1, max_length=256)
    repository_id: str | None = Field(default=None, min_length=1, max_length=256)
    repository_revision: str | None = Field(default=None, min_length=1, max_length=512)
    execution_location: str = Field(min_length=1, max_length=512)


class EnvironmentObservation(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    observation_id: UUID = Field(default_factory=new_id)
    revision: int = Field(default=1, ge=1)
    context_id: str = Field(min_length=1, max_length=256)
    repository_id: str | None = Field(default=None, min_length=1, max_length=256)
    repository_revision: str | None = Field(default=None, min_length=1, max_length=512)
    workspace: Path
    path_sensitivity: Literal["machine_local"] = "machine_local"
    execution_location: str = Field(min_length=1, max_length=512)
    platform: str = Field(min_length=1, max_length=128)
    architecture: str = Field(min_length=1, max_length=128)
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: str = Field(min_length=1, max_length=512)
    descriptors: tuple[DescriptorObservation, ...]
    manifests: dict[str, tuple[str, ...]]
    engines: tuple[EngineObservation, ...]
    unknowns: tuple[str, ...]
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("observed_at")
    @classmethod
    def observation_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"observation_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class EnvironmentBindingRequest(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=new_id)
    mission_id: str = Field(min_length=1, max_length=256)
    plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    owner_identity: str = Field(min_length=1, max_length=256)
    context_id: str = Field(min_length=1, max_length=256)
    observation_id: UUID
    observation_revision: int = Field(ge=1)
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    requested_outcome: EnvironmentOutcome
    target_platform: str = Field(min_length=1, max_length=128)
    target_architecture: str = Field(min_length=1, max_length=128)
    execution_location: str = Field(min_length=1, max_length=512)
    required_semantics: tuple[str, ...] = ()
    allowed_descriptor_formats: tuple[str, ...] = ()
    required_engine_ids: tuple[str, ...] = ()
    authorized_engine_ids: tuple[str, ...] = ()
    affected_task_ids: tuple[str, ...] = Field(min_length=1)
    verification_checks: tuple[str, ...] = Field(min_length=1)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    rationale: str = Field(min_length=1, max_length=4_096)


class EnvironmentBinding(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    binding_id: UUID = Field(default_factory=new_id)
    revision: int = Field(default=1, ge=1)
    request: EnvironmentBindingRequest
    state: EnvironmentBindingState
    selected_engine_ids: tuple[str, ...]
    selected_adapter_ids: tuple[str, ...]
    selected_descriptors: tuple[DescriptorObservation, ...]
    missing_conditions: tuple[str, ...]
    lost_fidelity: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=4_096)
    resolved_at: datetime = Field(default_factory=utc_now)

    @field_validator("resolved_at")
    @classmethod
    def resolved_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def compatible_binding_has_no_missing_conditions(self) -> EnvironmentBinding:
        if self.state is EnvironmentBindingState.COMPATIBLE and self.missing_conditions:
            raise ValueError("compatible environment binding cannot have missing conditions")
        if self.state is not EnvironmentBindingState.COMPATIBLE and (
            self.selected_engine_ids or self.selected_adapter_ids
        ):
            raise ValueError("non-compatible binding cannot select an executable adapter")
        return self


class EnvironmentDescriptorMember(EnvironmentModel):
    format: str = Field(min_length=1, max_length=128)
    specification_version: str | None = Field(default=None, min_length=1, max_length=256)
    logical_path: str = Field(min_length=1, max_length=1_024)
    artifact_reference: str = Field(pattern=r"^artifact:[a-f0-9-]{36}$")
    base_revision: str | None = Field(default=None, min_length=1, max_length=512)
    compatibility_limits: tuple[str, ...] = ()


class EnvironmentDescriptorSet(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    descriptor_set_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    target_platform: str = Field(min_length=1, max_length=128)
    target_architecture: str = Field(min_length=1, max_length=128)
    members: tuple[EnvironmentDescriptorMember, ...] = Field(min_length=1)
    credential_references: tuple[str, ...] = ()
    lifecycle_commands: tuple[str, ...] = ()
    expected_artifacts: tuple[str, ...] = ()


class EnvironmentDescriptorChangeRequest(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=new_id)
    descriptor_set_id: UUID
    owner_identity: str = Field(min_length=1, max_length=256)
    result_mode: int = Field(default=0o644, ge=0, le=0o7777)


class EnvironmentDescriptorChangePlan(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    request: EnvironmentDescriptorChangeRequest
    binding_id: UUID
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    unchanged_paths: tuple[str, ...]
    change_set: ChangeSet | None
    planned_at: datetime = Field(default_factory=utc_now)

    @field_validator("planned_at")
    @classmethod
    def change_plan_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class EnvironmentAttempt(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    attempt_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    operation_plan_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    operation: EnvironmentOperation
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,255}$")
    engine_id: str = Field(min_length=1, max_length=128)
    engine_version: str | None = Field(default=None, min_length=1, max_length=512)
    execution_location: str = Field(min_length=1, max_length=512)
    effect_call_ids: tuple[str, ...] = Field(min_length=1)
    artifact_references: tuple[str, ...]
    execution_status: ExecutionStatus
    effect_settlement: EffectSettlement
    declared_effects: tuple[str, ...]
    observed_effects: tuple[str, ...]
    exit_code: int | None = None
    settlement: EnvironmentSettlement
    started_at: datetime
    completed_at: datetime
    limitations: tuple[str, ...] = ()

    @field_validator("started_at", "completed_at")
    @classmethod
    def attempt_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def attempt_evidence_is_consistent(self) -> EnvironmentAttempt:
        if self.completed_at < self.started_at:
            raise ValueError("environment attempt cannot complete before it starts")
        if len(self.effect_call_ids) != len(set(self.effect_call_ids)):
            raise ValueError("environment attempt effect call identities must be unique")
        if len(self.artifact_references) != len(set(self.artifact_references)):
            raise ValueError("environment attempt artifact references must be unique")
        if self.settlement is EnvironmentSettlement.VERIFIED and (
            self.execution_status is not ExecutionStatus.COMPLETED
            or self.effect_settlement
            not in {
                EffectSettlement.ABSENT,
                EffectSettlement.COMPLETED,
            }
        ):
            raise ValueError("verified environment attempt requires verified completion")
        return self


class EnvironmentVerification(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    verification_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    location_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_id: str = Field(min_length=1, max_length=128)
    engine_version: str | None = Field(default=None, min_length=1, max_length=512)
    checks: dict[str, AvailabilityState] = Field(min_length=1)
    attempt_ids: tuple[UUID, ...] = Field(min_length=1)
    artifact_references: tuple[str, ...]
    settlement: EnvironmentSettlement
    limitations: tuple[str, ...]
    verified_at: datetime = Field(default_factory=utc_now)

    @field_validator("verified_at")
    @classmethod
    def verification_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def verification_evidence_is_consistent(self) -> EnvironmentVerification:
        if len(self.attempt_ids) != len(set(self.attempt_ids)):
            raise ValueError("environment verification attempt identities must be unique")
        if len(self.artifact_references) != len(set(self.artifact_references)):
            raise ValueError("environment verification artifact references must be unique")
        all_true = all(state is AvailabilityState.TRUE for state in self.checks.values())
        any_false = any(state is AvailabilityState.FALSE for state in self.checks.values())
        expected = (
            EnvironmentSettlement.VERIFIED
            if all_true
            else EnvironmentSettlement.FAILED
            if any_false
            else EnvironmentSettlement.UNCERTAIN
        )
        if self.settlement is not expected:
            raise ValueError("environment verification settlement contradicts its checks")
        return self


class EnvironmentOperationRequest(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    descriptor_set_id: UUID | None = None
    adapter_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,255}$")
    operation: EnvironmentOperation
    descriptor_path: str | None = Field(default=None, min_length=1, max_length=1_024)
    parameters: dict[str, str] = Field(default_factory=dict, max_length=32)
    network_destinations: tuple[str, ...] = ()
    credential_environment: dict[str, CredentialReference] = Field(
        default_factory=dict,
        max_length=32,
    )
    credential_references: tuple[CredentialReference, ...] = ()
    owner_identity: str = Field(min_length=1, max_length=256)
    run_id: str = Field(min_length=1, max_length=256)
    task_id: str = Field(min_length=1, max_length=256)
    session_profile: str = Field(min_length=1, max_length=128)
    deadline: datetime
    timeout_seconds: int = Field(ge=1, le=86_400)
    expected_exit_codes: tuple[int, ...] = (0,)
    preview_bytes: int = Field(default=65_536, ge=1, le=16_777_216)

    @field_validator("deadline")
    @classmethod
    def operation_deadline_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("descriptor_path")
    @classmethod
    def operation_descriptor_path_is_safe(cls, value: str | None) -> str | None:
        if value is not None:
            path = Path(value)
            if path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("environment operation descriptor path is unsafe")
        return value

    @field_validator("parameters")
    @classmethod
    def operation_parameters_are_bounded(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            not key
            or len(key) > 64
            or not parameter
            or len(parameter) > 1_024
            or "\x00" in parameter
            for key, parameter in value.items()
        ):
            raise ValueError("environment operation parameters are invalid")
        return value

    @field_validator("network_destinations")
    @classmethod
    def operation_network_destinations_are_unique(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("environment network destinations must be unique")
        return value


class EnvironmentOperationPlan(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    request: EnvironmentOperationRequest
    binding_revision: int = Field(ge=1)
    observation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: str = Field(min_length=1, max_length=512)
    adapter_revision: str = Field(min_length=1, max_length=512)
    execution: ExecutionRequest
    planned_at: datetime = Field(default_factory=utc_now)

    @field_validator("planned_at")
    @classmethod
    def operation_plan_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @property
    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json", exclude={"planned_at"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


class DescriptorValidationResult(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    descriptor_set_id: UUID
    binding_id: UUID
    valid: bool
    validated_members: tuple[str, ...]
    violations: tuple[str, ...]
    validator_revision: str = Field(min_length=1, max_length=512)
    validated_at: datetime = Field(default_factory=utc_now)

    @field_validator("validated_at")
    @classmethod
    def descriptor_validation_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class EnvironmentVerificationRequest(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    verification_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    owner_identity: str = Field(min_length=1, max_length=256)
    context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    engine_id: str = Field(min_length=1, max_length=128)
    check_attempt_ids: dict[str, tuple[UUID, ...]] = Field(min_length=1, max_length=64)
    limitations: tuple[str, ...] = ()

    @field_validator("check_attempt_ids")
    @classmethod
    def verification_checks_reference_attempts(
        cls,
        value: dict[str, tuple[UUID, ...]],
    ) -> dict[str, tuple[UUID, ...]]:
        if any(
            not name or not attempts or len(attempts) != len(set(attempts))
            for name, attempts in value.items()
        ):
            raise ValueError("verification checks require unique attempt identities")
        return value


class EnvironmentInvalidationCause(StrEnum):
    CONTEXT = "context"
    REPOSITORY = "repository"
    PLATFORM = "platform"
    ENGINE = "engine"
    POLICY = "policy"
    MISSION_SCOPE = "mission_scope"


class EnvironmentInvalidation(EnvironmentModel):
    schema_version: Literal["1.0"] = "1.0"
    invalidation_id: UUID = Field(default_factory=new_id)
    binding_id: UUID
    expected_binding_revision: int = Field(ge=1)
    owner_identity: str = Field(min_length=1, max_length=256)
    cause: EnvironmentInvalidationCause
    observed_context_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    affected_task_ids: tuple[str, ...] = Field(min_length=1)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=4_096)
    invalidated_at: datetime = Field(default_factory=utc_now)

    @field_validator("invalidated_at")
    @classmethod
    def invalidation_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)
