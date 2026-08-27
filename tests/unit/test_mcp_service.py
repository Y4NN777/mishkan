from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import types
from mcp.shared.session import ProgressFnT

from mishkan.config.models import (
    McpConfig,
    McpConnectionConfig,
    McpExposureProfileConfig,
    McpFacadeConfig,
    McpProtocolStrategy,
    McpTransport,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.mcp import (
    McpCallRequest,
    McpCallState,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpRepository,
    McpService,
    McpSessionState,
)
from mishkan.persistence.migration import SchemaManager
from mishkan.tools.inspection import ContentInspector, InspectionProfile


def _configured() -> McpConnectionConfig:
    return McpConnectionConfig(
        transport=McpTransport.STDIO,
        protocol_strategy=McpProtocolStrategy.PINNED,
        protocol_versions=("2025-11-25",),
        trust="project-configured",
        exposure_profile="repository-read",
        command="fixture-mcp",
        connect_timeout_seconds=5,
        call_timeout_seconds=5,
        max_result_bytes=16_384,
    )


def _config() -> McpConfig:
    return McpConfig(
        connections={"graph": _configured()},
        exposure_profiles={
            "repository-read": McpExposureProfileConfig(operations=("repository.read",))
        },
        facade=McpFacadeConfig(
            enabled=False,
            streamable_http_path="/mcp",
            stdio_bridge_enabled=False,
            exposure_profile="repository-read",
        ),
        progress_retention_seconds=60,
    )


def _primitive(
    disposition: McpEffectDisposition = McpEffectDisposition.READ_ONLY,
) -> McpPrimitiveDescriptor:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    annotations = {"readOnlyHint": disposition is McpEffectDisposition.READ_ONLY}
    return McpPrimitiveDescriptor(
        connection_id="graph",
        protocol_version="2025-11-25",
        kind=McpPrimitiveKind.TOOL,
        name="repository.read",
        input_schema=schema,
        annotations=annotations,
        effect_disposition=disposition,
        schema_hash=McpPrimitiveDescriptor.claim_hash(
            McpPrimitiveKind.TOOL,
            "repository.read",
            schema,
            None,
            annotations,
        ),
        provenance="test:mcp:graph",
    )


def _snapshot(primitive: McpPrimitiveDescriptor) -> McpDiscoverySnapshot:
    normalized = [
        {
            "kind": primitive.kind.value,
            "name": primitive.name,
            "schema_hash": primitive.schema_hash,
        }
    ]
    fingerprint = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return McpDiscoverySnapshot(
        connection_id="graph",
        protocol_version="2025-11-25",
        primitives=(primitive,),
        schema_fingerprint=fingerprint,
    )


class FakeClient:
    def __init__(self, snapshot: McpDiscoverySnapshot) -> None:
        self.snapshot = snapshot
        self.failure: Exception | None = None
        self.calls = 0

    async def discover(
        self,
        connection_id: str,
        configured: McpConnectionConfig,
        *,
        credentials: Any,
        workspace: Path,
    ) -> McpDiscoverySnapshot:
        del connection_id, configured, credentials, workspace
        return self.snapshot

    async def call_tool(
        self,
        configured: McpConnectionConfig,
        *,
        name: str,
        arguments: dict[str, Any],
        caller_identity: str,
        run_id: str,
        task_attempt_id: str,
        timeout_seconds: float,
        credentials: Any,
        workspace: Path,
        progress: ProgressFnT,
    ) -> types.CallToolResult:
        del (
            configured,
            name,
            caller_identity,
            run_id,
            task_attempt_id,
            timeout_seconds,
            credentials,
            workspace,
        )
        self.calls += 1
        await progress(1, 1, "complete")
        if self.failure is not None:
            raise self.failure
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="accepted")],
            structuredContent={"path": arguments["path"]},
        )


def _service(
    tmp_path: Path,
    client: FakeClient,
) -> tuple[McpService, McpRepository]:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    repository = McpRepository(database)
    inspector = ContentInspector(
        InspectionProfile(
            profile_id="test",
            revision="1",
            adoption_authority="test",
            rules=(),
        )
    )
    return McpService(tmp_path, _config(), repository, client, inspector), repository


def _request(primitive: McpPrimitiveDescriptor) -> McpCallRequest:
    return McpCallRequest(
        connection_id="graph",
        primitive_name=primitive.name,
        caller_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task-1:attempt-1",
        arguments={"path": "src"},
        declared_effects=("external_read",),
        effect_disposition=primitive.effect_disposition,
        expected_schema_hash=primitive.schema_hash,
        deadline=utc_now() + timedelta(seconds=30),
    )


@pytest.mark.anyio
async def test_connect_invoke_progress_and_exact_replay_are_durable(tmp_path: Path) -> None:
    primitive = _primitive()
    client = FakeClient(_snapshot(primitive))
    service, repository = _service(tmp_path, client)

    connected = await service.connect("graph", principal="role:Engineer", credentials={})
    request = _request(primitive)
    result = await service.invoke(request, credentials={})
    replay = await service.invoke(request, credentials={})

    assert connected.state is McpSessionState.READY
    assert connected.revision == 2
    assert result.state is McpCallState.COMPLETED
    assert replay == result
    assert client.calls == 1
    assert repository.progress_after(request.id, 0)[0].message == "complete"


@pytest.mark.anyio
async def test_reconnect_schema_drift_preserves_bound_primitives_and_degrades(
    tmp_path: Path,
) -> None:
    primitive = _primitive()
    client = FakeClient(_snapshot(primitive))
    service, repository = _service(tmp_path, client)
    await service.connect("graph", principal="role:Engineer", credentials={})
    changed = primitive.model_copy(
        update={
            "description": "changed",
            "annotations": {"readOnlyHint": True, "changed": True},
            "schema_hash": McpPrimitiveDescriptor.claim_hash(
                McpPrimitiveKind.TOOL,
                primitive.name,
                primitive.input_schema,
                None,
                {"readOnlyHint": True, "changed": True},
            ),
        }
    )
    client.snapshot = _snapshot(changed)

    with pytest.raises(MishkanError) as drift:
        await service.connect("graph", principal="role:Engineer", credentials={})

    assert drift.value.envelope.code is ErrorCode.TOOL_DRIFT
    assert repository.get_connection("graph").state is McpSessionState.DEGRADED
    assert repository.list_primitives("graph") == (primitive,)


@pytest.mark.anyio
async def test_transport_loss_marks_non_idempotent_call_uncertain(tmp_path: Path) -> None:
    primitive = _primitive(McpEffectDisposition.NON_IDEMPOTENT)
    client = FakeClient(_snapshot(primitive))
    client.failure = RuntimeError("connection lost")
    service, _repository = _service(tmp_path, client)
    await service.connect("graph", principal="role:Engineer", credentials={})

    result = await service.invoke(_request(primitive), credentials={})

    assert result.state is McpCallState.UNCERTAIN
    assert result.output is None
