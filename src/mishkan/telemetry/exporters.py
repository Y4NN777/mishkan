"""Bounded OTLP/HTTP export through MISHKAN's DNS-locked network boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, Protocol, cast

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, InstrumentationScope, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

from mishkan.config.models import NetworkProfileConfig
from mishkan.telemetry.models import TelemetryRecord, TelemetryRecordStatus, TelemetryScalar
from mishkan.web.network import HttpxWebTransport


class TelemetryExporter(Protocol):
    def export(self, record: TelemetryRecord) -> bool: ...


def _value(value: TelemetryScalar) -> AnyValue:
    if isinstance(value, bool):
        return AnyValue(bool_value=value)
    if isinstance(value, int):
        return AnyValue(int_value=value)
    if isinstance(value, float):
        return AnyValue(double_value=value)
    return AnyValue(string_value=value)


class OtlpHttpTelemetryExporter:
    def __init__(
        self,
        *,
        endpoint: str,
        profile: NetworkProfileConfig,
        service_name: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        transport: HttpxWebTransport | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._profile = profile
        self._service_name = service_name
        self._headers = dict(headers)
        self._timeout_seconds = timeout_seconds
        self._transport = transport or HttpxWebTransport()

    def export(self, record: TelemetryRecord) -> bool:
        identity = record.record_id.bytes
        trace_id = hashlib.sha256(identity + b"trace").digest()[:16]
        span_id = hashlib.sha256(identity + b"span").digest()[:8]
        attributes = [
            KeyValue(key=name, value=_value(value)) for name, value in record.attributes.items()
        ]
        span = Span(
            trace_id=trace_id,
            span_id=span_id,
            name=record.name,
            start_time_unix_nano=int(record.started_at.timestamp() * 1_000_000_000),
            end_time_unix_nano=int(record.completed_at.timestamp() * 1_000_000_000),
            attributes=attributes,
            status=Status(
                code=cast(
                    Any,
                    Status.STATUS_CODE_OK
                    if record.status is TelemetryRecordStatus.OK
                    else Status.STATUS_CODE_ERROR,
                )
            ),
        )
        request = ExportTraceServiceRequest(
            resource_spans=[
                ResourceSpans(
                    resource=Resource(
                        attributes=[
                            KeyValue(
                                key="service.name",
                                value=AnyValue(string_value=self._service_name),
                            )
                        ]
                    ),
                    scope_spans=[
                        ScopeSpans(
                            scope=InstrumentationScope(name="mishkan.telemetry", version="1.0"),
                            spans=[span],
                        )
                    ],
                )
            ]
        )
        exchange = self._transport.request(
            "POST",
            self._endpoint,
            profile=self._profile,
            headers={"content-type": "application/x-protobuf", **self._headers},
            content=request.SerializeToString(),
            timeout_seconds=self._timeout_seconds,
        )
        return 200 <= exchange.status_code < 300
