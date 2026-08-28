from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from mishkan.config.models import McpProtocolStrategy, McpTransport
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.mcp import (
    McpCallRequest,
    McpCallResult,
    McpCallState,
    McpConnectionRecord,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpProgressEvent,
    McpRepository,
    McpSessionState,
)
from mishkan.persistence.migration import SchemaManager


def _repository(tmp_path: Path) -> McpRepository:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    return McpRepository(database)


def _connection() -> McpConnectionRecord:
    return McpConnectionRecord(
        connection_id="project-graph",
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        configured_protocol_versions=("2025-11-25",),
        trust="project-configured",
        exposure_profile="repository-read",
        server_identity="stdio:test-server",
        policy_fingerprint="policy:test",
        state=McpSessionState.READY,
        revision=0,
        health="healthy",
    )


def _primitive(name: str = "repository.read") -> McpPrimitiveDescriptor:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}},
    }
    annotations = {"readOnlyHint": True}
    return McpPrimitiveDescriptor(
        connection_id="project-graph",
        protocol_version="2025-11-25",
        kind=McpPrimitiveKind.TOOL,
        name=name,
        input_schema=input_schema,
        annotations=annotations,
        effect_disposition=McpEffectDisposition.READ_ONLY,
        invocation_supported=True,
        schema_hash=McpPrimitiveDescriptor.claim_hash(
            McpPrimitiveKind.TOOL,
            name,
            input_schema,
            None,
            annotations,
        ),
        provenance="configured:mcp:project-graph",
    )


def _snapshot(*primitives: McpPrimitiveDescriptor) -> McpDiscoverySnapshot:
    claims = [
        {"kind": item.kind.value, "name": item.name, "schema_hash": item.schema_hash}
        for item in sorted(primitives, key=lambda item: (item.kind, item.name))
    ]
    fingerprint = hashlib.sha256(
        json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return McpDiscoverySnapshot(
        connection_id="project-graph",
        protocol_version="2025-11-25",
        primitives=primitives,
        schema_fingerprint=fingerprint,
    )


def _request(
    primitive: McpPrimitiveDescriptor,
    *,
    disposition: McpEffectDisposition = McpEffectDisposition.READ_ONLY,
) -> McpCallRequest:
    return McpCallRequest(
        connection_id=primitive.connection_id,
        primitive_name=primitive.name,
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task-1:attempt-1",
        arguments={"path": "src"},
        declared_effects=("external_read",),
        effect_disposition=disposition,
        expected_schema_hash=primitive.schema_hash,
        deadline=utc_now() + timedelta(seconds=30),
    )


def _ready_repository(tmp_path: Path) -> tuple[McpRepository, McpPrimitiveDescriptor]:
    repository = _repository(tmp_path)
    repository.create_connection(_connection())
    primitive = _primitive()
    repository.replace_discovery(
        _snapshot(primitive),
        expected_connection_revision=0,
        expected_schema_fingerprint=None,
    )
    return repository, primitive


def test_discovery_replacement_is_atomic_and_rejects_drift(tmp_path: Path) -> None:
    repository, primitive = _ready_repository(tmp_path)

    connection = repository.get_connection("project-graph")
    assert connection.revision == 1
    assert connection.schema_fingerprint == _snapshot(primitive).schema_fingerprint
    assert repository.list_primitives("project-graph") == (primitive,)

    changed = _primitive("repository.search")
    with pytest.raises(MishkanError) as raised:
        repository.replace_discovery(
            _snapshot(changed),
            expected_connection_revision=1,
            expected_schema_fingerprint=connection.schema_fingerprint,
        )

    assert raised.value.envelope.code is ErrorCode.TOOL_DRIFT
    assert repository.list_primitives("project-graph") == (primitive,)
    assert repository.get_connection("project-graph") == connection


def test_call_reservation_is_idempotent_and_bound_to_discovery(tmp_path: Path) -> None:
    repository, primitive = _ready_repository(tmp_path)
    request = _request(primitive)

    assert repository.reserve_call(request).created is True
    assert repository.reserve_call(request).created is False
    pending = repository.list_calls()
    assert pending[0]["request"]["id"] == str(request.id)
    assert pending[0]["state"] == McpCallState.RESERVED.value
    assert pending[0]["result"] is None
    with pytest.raises(MishkanError) as conflict:
        repository.reserve_call(request.model_copy(update={"arguments": {"path": "tests"}}))
    assert conflict.value.envelope.code is ErrorCode.DUPLICATE_RESULT

    unknown = _request(_primitive("repository.unknown"))
    with pytest.raises(MishkanError) as drift:
        repository.reserve_call(unknown)
    assert drift.value.envelope.code is ErrorCode.TOOL_DRIFT

    for offset, limit in ((-1, 1), (0, 0), (0, 1_001)):
        with pytest.raises(MishkanError) as invalid_bound:
            repository.list_calls(offset=offset, limit=limit)
        assert invalid_bound.value.envelope.code is ErrorCode.OUTPUT_CONTRACT


def test_call_lifecycle_progress_and_completion_are_monotone(tmp_path: Path) -> None:
    repository, primitive = _ready_repository(tmp_path)
    request = _request(primitive)
    repository.reserve_call(request)

    repository.set_call_state(request.id, McpCallState.DISPATCHING)
    repository.set_call_state(request.id, McpCallState.RUNNING)
    with pytest.raises(MishkanError) as regression:
        repository.set_call_state(request.id, McpCallState.RESERVED)
    assert regression.value.envelope.code is ErrorCode.REVISION_MISMATCH

    first = McpProgressEvent(request_id=request.id, cursor=0, message="started")
    repository.append_progress(first, max_events=100)
    with pytest.raises(MishkanError):
        repository.append_progress(
            McpProgressEvent(request_id=request.id, cursor=2, message="skipped"),
            max_events=100,
        )
    assert repository.progress_after(request.id, 0, limit=100) == (first,)

    result = McpCallResult(
        request_id=request.id,
        connection_id=request.connection_id,
        primitive_name=request.primitive_name,
        state=McpCallState.COMPLETED,
        output={"content": "ok"},
        schema_hash=request.expected_schema_hash,
        reason="remote MCP call completed",
    )
    assert repository.complete_call(result) == result
    assert repository.complete_call(result) == result
    assert repository.reserve_call(request).existing_result == result
    with pytest.raises(MishkanError):
        repository.append_progress(
            McpProgressEvent(request_id=request.id, cursor=1, message="late"),
            max_events=100,
        )


def test_restart_reconciliation_distinguishes_effect_uncertainty(tmp_path: Path) -> None:
    repository, primitive = _ready_repository(tmp_path)
    read = _request(primitive)
    mutation = _request(primitive, disposition=McpEffectDisposition.NON_IDEMPOTENT)
    for request in (read, mutation):
        repository.reserve_call(request)
        repository.set_call_state(request.id, McpCallState.DISPATCHING)
        repository.set_call_state(request.id, McpCallState.RUNNING)

    results = {result.request_id: result for result in repository.reconcile_incomplete()}

    assert results[read.id].state is McpCallState.LOST
    assert results[mutation.id].state is McpCallState.UNCERTAIN
    assert repository.reconcile_incomplete() == ()
