"""Minimum immutable artifact contracts required by I02 execution output."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from mishkan.domain.identity import DomainRecord


class ArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtifactLifecycle(StrEnum):
    AVAILABLE = "available"
    QUARANTINED = "quarantined"


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
