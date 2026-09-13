from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import NetworkProfileConfig, ProjectConfig, TelemetryConfig
from mishkan.config.presets import preset_text
from mishkan.crewai.credentials import CredentialPoolResolver
from mishkan.daemon import api as daemon_api
from mishkan.domain.time import utc_now
from mishkan.telemetry.exporters import OtlpHttpTelemetryExporter
from mishkan.telemetry.models import (
    TelemetryDisclosure,
    TelemetryExporterKind,
    TelemetryExportState,
    TelemetryRecord,
    TelemetryRecordStatus,
)
from mishkan.telemetry.service import TelemetryService
from mishkan.tools.inspection import (
    ContentInspector,
    InspectionAction,
    InspectionProfile,
    InspectionRule,
)
from mishkan.web.network import ConnectionEvidence, HttpExchange


class _Exporter:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.records: list[TelemetryRecord] = []

    def export(self, record: TelemetryRecord) -> bool:
        self.records.append(record)
        return self.succeeds


class _Transport:
    def __init__(self) -> None:
        self.content = b""
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, **kwargs: object) -> HttpExchange:
        assert method == "POST"
        assert url == "https://telemetry.example/v1/traces"
        self.content = bytes(kwargs["content"])
        self.headers = dict(kwargs["headers"])  # type: ignore[arg-type]
        return HttpExchange(
            status_code=200,
            headers={},
            content=b"",
            wire_bytes=0,
            decoded_bytes=0,
            connection=ConnectionEvidence(("203.0.113.1",), "203.0.113.1"),
        )


def _inspector() -> ContentInspector:
    return ContentInspector(
        InspectionProfile(
            profile_id="telemetry-test",
            revision="1",
            adoption_authority="test",
            rules=(
                InspectionRule(
                    rule_id="token",
                    pattern=r"token-[a-z]+",
                    action=InspectionAction.REDACT,
                ),
            ),
        )
    )


def _record() -> TelemetryRecord:
    started = utc_now()
    return TelemetryRecord(
        name="mishkan.command.completed",
        started_at=started,
        completed_at=started + timedelta(milliseconds=5),
        status=TelemetryRecordStatus.OK,
        attributes={
            "mishkan.command.id": "command-1",
            "mishkan.command.type": "run.initialize",
            "mishkan.payload": "token-secret",
        },
        content_attribute_names=("mishkan.payload",),
    )


def test_off_and_metadata_only_disclosures_do_not_leak_content() -> None:
    exporter = _Exporter()
    disabled = TelemetryService(TelemetryConfig(), _inspector(), lambda: exporter)
    assert disabled.observe(_record()).state is TelemetryExportState.DISABLED
    assert exporter.records == []

    metadata = TelemetryService(
        TelemetryConfig(
            disclosure=TelemetryDisclosure.METADATA_ONLY,
            exporter={
                "kind": "otlp_http",
                "endpoint": "https://telemetry.example/v1/traces",
                "network_profile": "public-read",
            },
            metadata_attributes=("mishkan.command.id", "mishkan.payload"),
        ),
        _inspector(),
        lambda: exporter,
    )
    evidence = metadata.observe(_record())
    assert evidence.state is TelemetryExportState.EXPORTED
    assert exporter.records[-1].attributes == {"mishkan.command.id": "command-1"}


def test_sanitized_disclosure_inspects_and_export_failure_only_degrades() -> None:
    exporter = _Exporter(succeeds=False)
    service = TelemetryService(
        TelemetryConfig(
            disclosure=TelemetryDisclosure.SANITIZED,
            exporter={
                "kind": "otlp_http",
                "endpoint": "https://telemetry.example/v1/traces",
                "network_profile": "public-read",
            },
        ),
        _inspector(),
        lambda: exporter,
    )
    evidence = service.observe(_record())
    assert evidence.state is TelemetryExportState.DEGRADED
    assert exporter.records[-1].attributes["mishkan.payload"] == "[REDACTED]"
    assert service.status().degraded == 1

    blocked = service.observe(_record(), resolved_secrets=("token-secret",))
    assert blocked.state is TelemetryExportState.BLOCKED
    assert len(exporter.records) == 1


def test_otlp_http_adapter_emits_a_standard_bounded_span() -> None:
    transport = _Transport()
    profile = NetworkProfileConfig(
        allowed_schemes=("https",),
        allowed_ports=(443,),
        allow_public=True,
        allow_private=False,
        allow_loopback=False,
        allow_link_local=False,
        allow_multicast=False,
        max_redirects=0,
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_response_bytes=1_024,
        max_decompressed_bytes=1_024,
        max_concurrency=1,
        credential_header_names=("x-api-key",),
    )
    exporter = OtlpHttpTelemetryExporter(
        endpoint="https://telemetry.example/v1/traces",
        profile=profile,
        service_name="mishkand-test",
        headers={"x-api-key": "credential"},
        timeout_seconds=1,
        transport=transport,  # type: ignore[arg-type]
    )

    assert exporter.export(_record()) is True
    request = ExportTraceServiceRequest.FromString(transport.content)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.name == "mishkan.command.completed"
    assert transport.headers["content-type"] == "application/x-protobuf"
    assert TelemetryExporterKind.LANGSMITH_OTLP.value == "langsmith_otlp"


def test_langsmith_adapter_resolves_exact_headers_late(
    tmp_path: Path,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    base = ConfigLoader().load([source]).value
    telemetry = TelemetryConfig(
        disclosure=TelemetryDisclosure.METADATA_ONLY,
        exporter={
            "kind": "langsmith_otlp",
            "endpoint": "https://api.smith.langchain.com/otel/v1/traces",
            "network_profile": "public-read",
            "credential_ref": {"source": "env", "locator": "LANGSMITH_TEST_KEY"},
            "credential_header": "x-api-key",
            "credential_prefix": "",
            "project": "mishkan-disposable-test",
        },
        metadata_attributes=("mishkan.command.id",),
    )
    config = base.model_copy(
        update={
            "project": ProjectConfig(workspace=tmp_path),
            "telemetry": telemetry,
        }
    )
    captured: dict[str, object] = {}

    class Exporter(_Exporter):
        pass

    def build(**kwargs: object) -> Exporter:
        captured.update(kwargs)
        return Exporter()

    monkeypatch.setenv("LANGSMITH_TEST_KEY", "temporary-secret")
    monkeypatch.setattr(daemon_api, "OtlpHttpTelemetryExporter", build)

    exporter = daemon_api._build_telemetry_exporter(config, CredentialPoolResolver())

    assert isinstance(exporter, Exporter)
    assert captured["headers"] == {
        "x-api-key": "temporary-secret",
        "Langsmith-Project": "mishkan-disposable-test",
    }
    assert "temporary-secret" not in config.model_dump_json()
