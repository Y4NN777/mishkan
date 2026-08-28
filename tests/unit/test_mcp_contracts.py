from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from mcp import types
from pydantic import ValidationError

from mishkan.config.models import (
    CredentialReference,
    CredentialSource,
    McpConnectionConfig,
    McpProtocolStrategy,
    McpTransport,
    MishkanConfig,
)
from mishkan.config.presets import preset_text
from mishkan.domain.errors import ErrorCode, MishkanError
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
    McpSdkClient,
    McpSessionState,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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
        invocation_supported=True,
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
        server_identity="stdio:test-server",
        policy_fingerprint="policy:test",
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


@pytest.mark.anyio
async def test_discovery_refuses_an_unbounded_or_cyclic_page_stream() -> None:
    calls = 0

    async def endless(*, cursor: str | None):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            tools=(
                types.Tool(
                    name=f"fixture.{calls}",
                    inputSchema={"type": "object"},
                ),
            ),
            nextCursor=str(calls),
        )

    with pytest.raises(MishkanError) as bounded:
        await McpSdkClient._all_pages(
            endless,
            "tools",
            max_pages=2,
            max_values=10,
            max_bytes=10_000,
        )

    assert bounded.value.envelope.code is ErrorCode.MCP
    assert bounded.value.envelope.details["page_limit"] == 2

    async def cyclic(*, cursor: str | None):
        return SimpleNamespace(
            tools=(),
            nextCursor="same",
        )

    with pytest.raises(MishkanError) as cycle:
        await McpSdkClient._all_pages(
            cyclic,
            "tools",
            max_pages=10,
            max_values=10,
            max_bytes=10_000,
        )

    assert cycle.value.envelope.code is ErrorCode.MCP
    assert "cursor cycle" in cycle.value.envelope.message


@pytest.mark.anyio
async def test_http_mcp_refuses_credential_control_of_routing_headers(tmp_path: Path) -> None:
    mishkan = MishkanConfig.model_validate(yaml.safe_load(preset_text("local")))
    assert mishkan.web is not None
    reference = CredentialReference(source=CredentialSource.ENV, locator="MCP_HOST_HEADER")
    configured = McpConnectionConfig(
        transport=McpTransport.STREAMABLE_HTTP,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=("2025-11-25",),
        trust="test",
        exposure_profile="test",
        credential_refs=(reference,),
        network_profile="public-read",
        endpoint="https://example.com/mcp",
        headers={"Host": reference},
        connect_timeout_seconds=1,
        call_timeout_seconds=1,
        max_result_bytes=1_024,
    )
    client = McpSdkClient({"public-read": mishkan.web.network_profiles["public-read"]})

    with pytest.raises(MishkanError) as refused:
        await client.discover(
            "host-header",
            configured,
            credentials={reference.locator: "attacker.example"},
            workspace=tmp_path,
        )

    assert refused.value.envelope.code is ErrorCode.AUTHORITY_NOT_GRANTED
