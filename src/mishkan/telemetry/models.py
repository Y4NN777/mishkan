"""Vendor-neutral, bounded derived-observability contracts."""

from __future__ import annotations

import math
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.time import require_aware, utc_now


class TelemetryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TelemetryDisclosure(StrEnum):
    OFF = "off"
    METADATA_ONLY = "metadata_only"
    SANITIZED = "sanitized"


class TelemetryExporterKind(StrEnum):
    OTLP_HTTP = "otlp_http"
    LANGSMITH_OTLP = "langsmith_otlp"


class TelemetryRecordStatus(StrEnum):
    OK = "ok"
    ERROR = "error"


class TelemetryExportState(StrEnum):
    DISABLED = "disabled"
    EXPORTED = "exported"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


TelemetryScalar = str | bool | int | float


class LangSmithFeedbackImportRequest(TelemetryModel):
    """Bounded, attributable feedback copied from an authorized LangSmith client."""

    schema_version: Literal["1.0"] = "1.0"
    import_id: UUID = Field(default_factory=uuid4)
    owner_identity: str = Field(min_length=1, max_length=256)
    project_name: str = Field(min_length=1, max_length=256)
    external_feedback_id: UUID
    traced_run_id: UUID
    session_id: UUID | None = None
    key: str = Field(min_length=1, max_length=256)
    score: float | None = None
    value: TelemetryScalar | None = None
    comment: str | None = Field(default=None, max_length=8_192)
    feedback_source_type: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    )
    source_created_at: datetime

    @field_validator("source_created_at")
    @classmethod
    def source_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("value")
    @classmethod
    def string_value_is_bounded(cls, value: TelemetryScalar | None) -> TelemetryScalar | None:
        if isinstance(value, str) and len(value.encode()) > 8_192:
            raise ValueError("LangSmith feedback value exceeds its public bound")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("LangSmith feedback value must be finite")
        return value

    @field_validator("score")
    @classmethod
    def score_is_finite(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("LangSmith feedback score must be finite")
        return value


class TelemetryEvaluationCandidate(TelemetryModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: UUID = Field(default_factory=uuid4)
    source: Literal["langsmith"] = "langsmith"
    request: LangSmithFeedbackImportRequest
    source_payload_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority: Literal["candidate_only"] = "candidate_only"
    accepted: Literal[False] = False
    imported_at: datetime = Field(default_factory=utc_now)

    @field_validator("imported_at")
    @classmethod
    def import_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class TelemetryEvaluationImportResult(TelemetryModel):
    schema_version: Literal["1.0"] = "1.0"
    candidate: TelemetryEvaluationCandidate
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")


class TelemetryRecord(TelemetryModel):
    schema_version: Literal["1.0"] = "1.0"
    record_id: UUID = Field(default_factory=uuid4)
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    started_at: datetime
    completed_at: datetime
    status: TelemetryRecordStatus
    attributes: dict[str, TelemetryScalar] = Field(default_factory=dict, max_length=1_000)
    content_attribute_names: tuple[str, ...] = ()

    @field_validator("started_at", "completed_at")
    @classmethod
    def times_are_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def record_is_consistent(self) -> TelemetryRecord:
        if self.completed_at < self.started_at:
            raise ValueError("telemetry record cannot complete before it starts")
        if len(self.content_attribute_names) != len(set(self.content_attribute_names)):
            raise ValueError("telemetry content attribute names must be unique")
        if set(self.content_attribute_names) - set(self.attributes):
            raise ValueError("telemetry content attributes must exist in the record")
        return self


class TelemetryExportEvidence(TelemetryModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: UUID = Field(default_factory=uuid4)
    record_id: UUID
    disclosure: TelemetryDisclosure
    state: TelemetryExportState
    exported_attribute_names: tuple[str, ...]
    omitted_attribute_names: tuple[str, ...]
    exporter_kind: TelemetryExporterKind | None = None
    reason: str | None = Field(default=None, max_length=1_024)
    observed_at: datetime = Field(default_factory=utc_now)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class TelemetryStatus(TelemetryModel):
    schema_version: Literal["1.0"] = "1.0"
    disclosure: TelemetryDisclosure
    exporter_kind: TelemetryExporterKind | None
    attempted: int = Field(ge=0)
    exported: int = Field(ge=0)
    degraded: int = Field(ge=0)
    blocked: int = Field(ge=0)
    last_evidence: TelemetryExportEvidence | None = None
