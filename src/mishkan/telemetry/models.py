"""Vendor-neutral, bounded derived-observability contracts."""

from __future__ import annotations

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
