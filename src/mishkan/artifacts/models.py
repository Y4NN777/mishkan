"""Minimum immutable artifact contracts required by I02 execution output."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class ArtifactProvenance(ArtifactModel):
    producer_identity: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    call_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    channel: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,63}$")


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
    created_at: datetime = Field(default_factory=utc_now)


class WorkingReference(ArtifactModel):
    schema_version: str = "1.0"
    scope: str = Field(min_length=1, max_length=256)
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,255}$")
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    revision: int = Field(ge=1)
    updated_at: datetime = Field(default_factory=utc_now)


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


class ArtifactReconciliationIssue(ArtifactModel):
    action: ArtifactReconciliationAction
    artifact_reference: str | None = None
    storage_ref: str | None = None
    scope: str | None = None
    name: str | None = None
    collection_id: UUID | None = None


class ArtifactReconciliationPlan(ArtifactModel):
    schema_version: str = "1.0"
    plan_id: UUID
    issues: tuple[ArtifactReconciliationIssue, ...]
    created_at: datetime = Field(default_factory=utc_now)
    applied: bool = False
