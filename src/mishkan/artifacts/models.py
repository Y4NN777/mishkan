"""Immutable artifact contracts for governed execution output."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mishkan.domain.identity import DomainRecord
from mishkan.domain.time import utc_now


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactLifecycle(StrEnum):
    STAGING = "staging"
    VALIDATING = "validating"
    AVAILABLE = "available"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    EXPIRED = "expired"
    MISSING = "missing"
    CORRUPT = "corrupt"
    TOMBSTONED = "tombstoned"
    DELETED = "deleted"


class ArtifactValidation(StrEnum):
    INTEGRITY_VERIFIED = "integrity_verified"
    PARTIAL = "partial"


class ArtifactFactState(StrEnum):
    NOT_EVALUATED = "not_evaluated"
    PASSED = "passed"
    FAILED = "failed"


class ArtifactTrust(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"


class ArtifactAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ArtifactFacts(ArtifactModel):
    """Independent evidence dimensions; none grants use authority."""

    integrity: ArtifactFactState = ArtifactFactState.NOT_EVALUATED
    detected_media_type: str | None = None
    security_scan: ArtifactFactState = ArtifactFactState.NOT_EVALUATED
    schema_validity: ArtifactFactState = ArtifactFactState.NOT_EVALUATED
    rendering: ArtifactFactState = ArtifactFactState.NOT_EVALUATED
    trust: ArtifactTrust = ArtifactTrust.UNTRUSTED
    sensitivity: str = Field(min_length=1)
    availability: ArtifactAvailability = ArtifactAvailability.UNAVAILABLE
    authorization: Literal["contextual_policy_required"] = "contextual_policy_required"


class ArtifactProvenance(ArtifactModel):
    producer_identity: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    channel: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")
    source_artifacts: tuple[str, ...] = Field(default_factory=tuple)
    engine: str | None = Field(default=None, min_length=1, max_length=256)
    engine_version: str | None = Field(default=None, min_length=1, max_length=128)
    configuration_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    declared_loss: str | None = Field(default=None, min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def derivation_is_explicit(self) -> ArtifactProvenance:
        for reference in self.source_artifacts:
            if not reference.startswith("artifact:"):
                raise ValueError("artifact provenance source must be an artifact reference")
            try:
                UUID(reference.removeprefix("artifact:"))
            except ValueError as exc:
                raise ValueError("artifact provenance source must contain a UUID") from exc
        if self.source_artifacts and self.engine is None:
            raise ValueError("derived artifact provenance requires an engine")
        if self.engine_version is not None and self.engine is None:
            raise ValueError("artifact provenance engine version requires an engine")
        if self.configuration_fingerprint is not None and self.engine is None:
            raise ValueError("artifact provenance configuration requires an engine")
        return self


class ArtifactManifest(DomainRecord):
    digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    declared_media_type: str = Field(min_length=1)
    detected_media_type: str | None = None
    provenance: ArtifactProvenance
    sensitivity: str = Field(min_length=1)
    retention: str = Field(min_length=1)
    validation: ArtifactValidation
    lifecycle: ArtifactLifecycle
    acceptance: Literal["unaccepted"] = "unaccepted"
    storage_ref: str = Field(pattern=r"^sha256/[a-f0-9]{2}/[a-f0-9]{62}$")
    facts: ArtifactFacts = Field(default_factory=lambda: ArtifactFacts(sensitivity="internal"))

    @property
    def reference(self) -> str:
        return f"artifact:{self.id}"


class UploadSession(ArtifactModel):
    schema_version: str = "1.0"
    upload_id: UUID
    expected_size: int = Field(ge=0)
    expected_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    media_type: str = Field(min_length=1)
    offset: int = Field(ge=0)
    lifecycle: Literal["staging", "committed", "aborted"]
    created_at: datetime = Field(default_factory=utc_now)


class ArtifactCollection(ArtifactModel):
    schema_version: str = "1.0"
    collection_id: UUID
    entries: dict[str, str]
    ordered_paths: tuple[str, ...] = Field(default_factory=tuple)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def ordering_references_exact_members(self) -> ArtifactCollection:
        if self.ordered_paths and (
            len(self.ordered_paths) != len(self.entries)
            or set(self.ordered_paths) != set(self.entries)
        ):
            raise ValueError("collection ordering must identify every member exactly once")
        return self


class WorkingReference(ArtifactModel):
    schema_version: str = "1.0"
    scope: str = Field(min_length=1, max_length=256)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    revision: int = Field(ge=1)
    prior_artifact_reference: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f-]{36}$")
    prior_revision: int | None = Field(default=None, ge=1)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def prior_identity_is_complete(self) -> WorkingReference:
        if (self.prior_artifact_reference is None) != (self.prior_revision is None):
            raise ValueError("working reference prior artifact and revision must be paired")
        if self.prior_revision is not None and self.prior_revision >= self.revision:
            raise ValueError("working reference prior revision must precede current revision")
        return self


class ArtifactHold(ArtifactModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    reason: str = Field(min_length=1, max_length=2_048)
    created_at: datetime


class ArtifactPin(ArtifactModel):
    schema_version: Literal["1.0"] = "1.0"
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    created_at: datetime


class GarbageCollectionPlan(ArtifactModel):
    schema_version: str = "1.0"
    plan_id: UUID
    candidates: tuple[str, ...]
    watermark: datetime
    applied: bool = False


class ArtifactReconciliationAction(StrEnum):
    MARK_MISSING = "mark_missing"
    MARK_CORRUPT = "mark_corrupt"
    DELETE_ORPHAN_BLOB = "delete_orphan_blob"
    DELETE_INVALID_REFERENCE = "delete_invalid_reference"
    DELETE_INCOMPLETE_COLLECTION = "delete_incomplete_collection"
    ABORT_EXPIRED_UPLOAD = "abort_expired_upload"


class ArtifactReconciliationIssue(ArtifactModel):
    action: ArtifactReconciliationAction
    artifact_reference: str | None = None
    storage_ref: str | None = None
    scope: str | None = None
    name: str | None = None
    collection_id: UUID | None = None
    upload_id: UUID | None = None


class ArtifactReconciliationPlan(ArtifactModel):
    schema_version: str = "1.0"
    plan_id: UUID
    issues: tuple[ArtifactReconciliationIssue, ...]
    created_at: datetime = Field(default_factory=utc_now)
    applied: bool = False
