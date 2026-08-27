from __future__ import annotations

import hashlib
import json
from datetime import timedelta

import pytest
from pydantic import ValidationError

from mishkan.config.models import McpProtocolStrategy, McpTransport
from mishkan.domain.time import utc_now
from mishkan.mcp import (
    McpCallRequest,
    McpConnectionRecord,
    McpDirection,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpProgressEvent,
    McpSessionState,
)


def _primitive(name: str = "repository.read") -> McpPrimitiveDescriptor:
    input_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"path": {"type": "string"}},
    }
    annotations = {"readOnlyHint": True}
    return McpPrimitiveDescriptor(
        connection_id="graph",
        protocol_version="2025-11-25",
        kind=McpPrimitiveKind.TOOL,
        name=name,
        input_schema=input_schema,
        annotations=annotations,
        effect_disposition=McpEffectDisposition.READ_ONLY,
        schema_hash=McpPrimitiveDescriptor.claim_hash(
            McpPrimitiveKind.TOOL,
            name,
            input_schema,
            None,
            annotations,
        ),
        provenance="configured:mcp:graph",
    )


def test_connection_protocol_and_discovery_hashes_are_structural() -> None:
    connection = McpConnectionRecord(
        connection_id="graph",
        direction=McpDirection.OUTBOUND,
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        configured_protocol_versions=("2025-11-25",),
        negotiated_protocol_version="2025-11-25",
        trust="project-configured",
        exposure_profile="graph-read",
        state=McpSessionState.READY,
        revision=1,
        health="healthy",
    )
    primitive = _primitive()
    serialized = [
        {
            "kind": primitive.kind.value,
            "name": primitive.name,
            "schema_hash": primitive.schema_hash,
        }
    ]
    fingerprint = hashlib.sha256(
        json.dumps(serialized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = McpDiscoverySnapshot(
        connection_id=connection.connection_id,
        protocol_version=connection.negotiated_protocol_version,
        primitives=(primitive,),
        schema_fingerprint=fingerprint,
    )

    assert snapshot.primitives == (primitive,)
    with pytest.raises(ValidationError):
        McpConnectionRecord.model_validate(
            {**connection.model_dump(), "negotiated_protocol_version": "unknown"}
        )
    with pytest.raises(ValidationError):
        McpDiscoverySnapshot(
            connection_id="graph",
            protocol_version="2025-11-25",
            primitives=(primitive,),
            schema_fingerprint="wrong",
        )


def test_call_and_progress_preserve_caller_deadline_and_cursor() -> None:
    request = McpCallRequest(
        connection_id="graph",
        primitive_name="repository.read",
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task:1",
        arguments={"path": "src"},
        declared_effects=("external_read",),
        effect_disposition=McpEffectDisposition.READ_ONLY,
        expected_schema_hash=_primitive().schema_hash,
        deadline=utc_now() + timedelta(seconds=30),
    )
    progress = McpProgressEvent(
        request_id=request.id,
        cursor=0,
        progress=1,
        total=3,
        message="discovered repository",
    )

    assert progress.request_id == request.id
    with pytest.raises(ValidationError):
        McpProgressEvent.model_validate({**progress.model_dump(), "progress": -1})
