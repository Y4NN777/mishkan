"""Disclosure enforcement and best-effort telemetry projection."""

from __future__ import annotations

import json
from collections.abc import Callable
from threading import Lock

from mishkan.config.models import TelemetryConfig
from mishkan.domain.errors import MishkanError
from mishkan.telemetry.exporters import TelemetryExporter
from mishkan.telemetry.models import (
    TelemetryDisclosure,
    TelemetryExporterKind,
    TelemetryExportEvidence,
    TelemetryExportState,
    TelemetryRecord,
    TelemetryStatus,
)
from mishkan.tools.inspection import ContentInspector


class TelemetryService:
    def __init__(
        self,
        config: TelemetryConfig,
        inspector: ContentInspector,
        exporter_factory: Callable[[], TelemetryExporter] | None = None,
    ) -> None:
        self._config = config
        self._inspector = inspector
        self._exporter_factory = exporter_factory
        self._lock = Lock()
        self._attempted = 0
        self._exported = 0
        self._degraded = 0
        self._blocked = 0
        self._last: TelemetryExportEvidence | None = None

    def observe(
        self,
        record: TelemetryRecord,
        *,
        resolved_secrets: tuple[str, ...] = (),
    ) -> TelemetryExportEvidence:
        exporter_kind = self._exporter_kind
        if self._config.disclosure is TelemetryDisclosure.OFF:
            return self._remember(
                TelemetryExportEvidence(
                    record_id=record.record_id,
                    disclosure=self._config.disclosure,
                    state=TelemetryExportState.DISABLED,
                    exported_attribute_names=(),
                    omitted_attribute_names=tuple(sorted(record.attributes)),
                )
            )
        try:
            projected = self._project(record, resolved_secrets)
        except (MishkanError, ValueError, TypeError):
            return self._remember(
                TelemetryExportEvidence(
                    record_id=record.record_id,
                    disclosure=self._config.disclosure,
                    state=TelemetryExportState.BLOCKED,
                    exported_attribute_names=(),
                    omitted_attribute_names=tuple(sorted(record.attributes)),
                    exporter_kind=exporter_kind,
                    reason="content inspection or disclosure policy refused the projection",
                )
            )
        omitted = tuple(sorted(set(record.attributes) - set(projected.attributes)))
        try:
            if self._exporter_factory is None or not self._exporter_factory().export(projected):
                raise RuntimeError("exporter returned an unsuccessful settlement")
        except Exception:
            return self._remember(
                TelemetryExportEvidence(
                    record_id=record.record_id,
                    disclosure=self._config.disclosure,
                    state=TelemetryExportState.DEGRADED,
                    exported_attribute_names=tuple(sorted(projected.attributes)),
                    omitted_attribute_names=omitted,
                    exporter_kind=exporter_kind,
                    reason="telemetry exporter is unavailable or refused the batch",
                )
            )
        return self._remember(
            TelemetryExportEvidence(
                record_id=record.record_id,
                disclosure=self._config.disclosure,
                state=TelemetryExportState.EXPORTED,
                exported_attribute_names=tuple(sorted(projected.attributes)),
                omitted_attribute_names=omitted,
                exporter_kind=exporter_kind,
            )
        )

    def status(self) -> TelemetryStatus:
        with self._lock:
            return TelemetryStatus(
                disclosure=self._config.disclosure,
                exporter_kind=self._exporter_kind,
                attempted=self._attempted,
                exported=self._exported,
                degraded=self._degraded,
                blocked=self._blocked,
                last_evidence=self._last,
            )

    @property
    def _exporter_kind(self) -> TelemetryExporterKind | None:
        exporter = self._config.exporter
        return exporter.kind if exporter is not None else None

    def _project(
        self,
        record: TelemetryRecord,
        resolved_secrets: tuple[str, ...],
    ) -> TelemetryRecord:
        if self._config.disclosure is TelemetryDisclosure.METADATA_ONLY:
            allowed = set(self._config.metadata_attributes) - set(record.content_attribute_names)
            attributes = {key: value for key, value in record.attributes.items() if key in allowed}
        else:
            attributes = dict(record.attributes)
        if len(attributes) > self._config.max_attributes:
            raise ValueError("telemetry attribute count exceeds its public bound")
        inspected: dict[str, str | bool | int | float] = {}
        for key, value in attributes.items():
            if not key or len(key.encode()) > self._config.max_attribute_bytes:
                raise ValueError("telemetry attribute name exceeds its public bound")
            if isinstance(value, str):
                serialized = value
            else:
                serialized = json.dumps(value, separators=(",", ":"))
            if len(serialized.encode()) > self._config.max_attribute_bytes:
                raise ValueError("telemetry attribute value exceeds its public bound")
            cleaned = self._inspector.inspect(serialized, resolved_secrets)
            inspected[key] = cleaned if isinstance(value, str) else value
        return record.model_copy(update={"attributes": inspected})

    def _remember(self, evidence: TelemetryExportEvidence) -> TelemetryExportEvidence:
        with self._lock:
            if evidence.state is not TelemetryExportState.DISABLED:
                self._attempted += 1
            if evidence.state is TelemetryExportState.EXPORTED:
                self._exported += 1
            elif evidence.state is TelemetryExportState.DEGRADED:
                self._degraded += 1
            elif evidence.state is TelemetryExportState.BLOCKED:
                self._blocked += 1
            self._last = evidence
        return evidence
