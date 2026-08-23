from datetime import UTC, datetime

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
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
        "ERR-SCH-001",
        "ERR-WRK-001",
        "ERR-VER-001",
    }
