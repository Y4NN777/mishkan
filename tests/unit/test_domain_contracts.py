from datetime import UTC, datetime
from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.export import SCHEMAS, export_schemas
from mishkan.domain.identity import DomainRecord, new_id
from mishkan.domain.schema import SchemaRegistry
from mishkan.domain.time import render_timestamp


def test_generated_identifiers_are_unique() -> None:
    assert len({new_id() for _ in range(1_000)}) == 1_000


def test_domain_record_requires_an_unambiguous_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone offset"):
        DomainRecord(created_at=datetime(2026, 8, 23, 12, 0))


def test_timestamp_rendering_names_the_applied_timezone() -> None:
    rendered = render_timestamp(datetime(2026, 8, 23, 12, 0, tzinfo=UTC), "Africa/Ouagadougou")
    assert rendered == "2026-08-23T12:00:00+00:00 [Africa/Ouagadougou]"


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        render_timestamp(datetime.now(UTC), "Mars/Olympus_Mons")


def test_schema_registry_refuses_automatic_migration() -> None:
    with pytest.raises(MishkanError) as caught:
        SchemaRegistry.require_supported("mishkan.config", "2.0")
    assert caught.value.envelope.code is ErrorCode.VERSION
    assert caught.value.envelope.details["automatic_migration"] is False


def test_error_catalogue_matches_the_srs_namespace() -> None:
    assert {code.value for code in ErrorCode} == {
        "ERR-CFG-001",
        "ERR-PRJ-001",
        "ERR-PLN-001",
        "ERR-PLN-002",
        "ERR-DEC-001",
        "ERR-DEC-002",
        "ERR-POL-001",
        "ERR-POL-002",
        "ERR-ROL-001",
        "ERR-OUT-001",
        "ERR-REV-001",
        "ERR-RUN-001",
        "ERR-RUN-002",
        "ERR-DEP-001",
        "ERR-DEP-002",
        "ERR-SEC-001",
        "ERR-SKL-001",
        "ERR-SKL-002",
        "ERR-SKL-003",
        "ERR-TOL-001",
        "ERR-TOL-002",
        "ERR-TOL-003",
        "ERR-TOL-004",
        "ERR-TOL-005",
        "ERR-CTX-001",
        "ERR-MSN-001",
        "ERR-FIL-001",
        "ERR-EDT-001",
        "ERR-EXE-001",
        "ERR-WEB-001",
        "ERR-BRW-001",
        "ERR-ART-001",
        "ERR-MCP-001",
        "ERR-ENG-001",
        "ERR-SCH-001",
        "ERR-WRK-001",
        "ERR-VER-001",
    }


def test_public_contract_catalogue_exports_deterministically(tmp_path: Path) -> None:
    expected = {
        "application-command-v1.schema.json",
        "artifact-collection-v1.schema.json",
        "artifact-hold-v1.schema.json",
        "artifact-gc-plan-v1.schema.json",
        "artifact-manifest-v1.schema.json",
        "artifact-pin-v1.schema.json",
        "artifact-reconciliation-plan-v1.schema.json",
        "artifact-upload-session-v1.schema.json",
        "artifact-working-reference-v1.schema.json",
        "browser-action-request-v1.schema.json",
        "browser-action-result-v1.schema.json",
        "browser-diagnostic-request-v1.schema.json",
        "browser-diagnostic-result-v1.schema.json",
        "browser-observation-request-v1.schema.json",
        "browser-observation-v1.schema.json",
        "browser-session-request-v1.schema.json",
        "browser-session-v1.schema.json",
        "change-set-result-v1.schema.json",
        "change-set-v1.schema.json",
        "command-result-v1.schema.json",
        "config-v1.schema.json",
        "domain-record-v1.schema.json",
        "error-envelope-v1.schema.json",
        "event-envelope-v1.schema.json",
        "event-hold-v1.schema.json",
        "event-page-v1.schema.json",
        "event-retention-plan-v1.schema.json",
        "event-retention-policy-v1.schema.json",
        "execution-request-v1.schema.json",
        "execution-result-v1.schema.json",
        "execution-cursor-read-v1.schema.json",
        "execution-session-v1.schema.json",
        "git-effect-request-v1.schema.json",
        "git-effect-result-v1.schema.json",
        "mcp-call-request-v1.schema.json",
        "mcp-call-result-v1.schema.json",
        "mcp-connection-v1.schema.json",
        "mcp-discovery-v1.schema.json",
        "mcp-primitive-v1.schema.json",
        "mcp-progress-v1.schema.json",
        "run-initialization-request-v1.schema.json",
        "web-citation-evidence-v1.schema.json",
        "web-crawl-request-v1.schema.json",
        "web-crawl-result-v1.schema.json",
        "web-extraction-request-v1.schema.json",
        "web-extraction-result-v1.schema.json",
        "web-fetch-request-v1.schema.json",
        "web-fetch-result-v1.schema.json",
        "web-http-request-v1.schema.json",
        "web-http-result-v1.schema.json",
        "web-map-request-v1.schema.json",
        "web-map-result-v1.schema.json",
        "web-search-request-v1.schema.json",
        "web-search-response-v1.schema.json",
        "snapshot-envelope-v1.schema.json",
        "task-review-rejection-v1.schema.json",
    }
    assert set(SCHEMAS) == expected

    first = export_schemas(tmp_path)
    initial_content = {path.name: path.read_bytes() for path in first}
    second = export_schemas(tmp_path)

    assert {path.name for path in first} == expected
    assert {path.name: path.read_bytes() for path in second} == initial_content
