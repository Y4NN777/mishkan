from __future__ import annotations

import asyncio
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
    McpCallResult,
    McpCallState,
    McpClientCallOutcome,
    McpContractFactory,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpRemoteTaskTerminal,
    McpRepository,
    McpService,
    McpServiceRunner,
    McpSessionState,
)
from mishkan.mcp.tools import McpPrimitiveToolAdapter
from mishkan.persistence.migration import SchemaManager
from mishkan.policy.models import ResourceRequest
from mishkan.tools.adapters import AdapterCall
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.gateway_models import CallStatus, ResolvedTargets
from mishkan.tools.inspection import ContentInspector, InspectionProfile


def _configured(*, remote_tasks_enabled: bool = False) -> McpConnectionConfig:
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
        remote_tasks_enabled=remote_tasks_enabled,
    )


def _config(*, remote_tasks_enabled: bool = False) -> McpConfig:
    return McpConfig(
        connections={"graph": _configured(remote_tasks_enabled=remote_tasks_enabled)},
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
        cancellation_poll_seconds=0.01,
    )


def _primitive(
    disposition: McpEffectDisposition = McpEffectDisposition.READ_ONLY,
    *,
    require_content: bool = False,
) -> McpPrimitiveDescriptor:
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    output_schema = (
        {
            "type": "object",
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
        }
        if require_content
        else None
    )
    annotations = {"readOnlyHint": disposition is McpEffectDisposition.READ_ONLY}
    return McpPrimitiveDescriptor(
        connection_id="graph",
        protocol_version="2025-11-25",
        kind=McpPrimitiveKind.TOOL,
        name="repository.read",
        input_schema=schema,
        output_schema=output_schema,
        annotations=annotations,
        effect_disposition=disposition,
        schema_hash=McpPrimitiveDescriptor.claim_hash(
            McpPrimitiveKind.TOOL,
            "repository.read",
            schema,
            output_schema,
            annotations,
        ),
        provenance="test:mcp:graph",
    )


def _snapshot(
    primitive: McpPrimitiveDescriptor,
    *,
    task_tool_calls_supported: bool = False,
    task_cancellation_supported: bool = False,
) -> McpDiscoverySnapshot:
    fingerprint = McpDiscoverySnapshot.claim_fingerprint(
        (primitive,),
        task_tool_calls_supported=task_tool_calls_supported,
        task_cancellation_supported=task_cancellation_supported,
    )
    return McpDiscoverySnapshot(
        connection_id="graph",
        protocol_version="2025-11-25",
        primitives=(primitive,),
        schema_fingerprint=fingerprint,
        task_tool_calls_supported=task_tool_calls_supported,
        task_cancellation_supported=task_cancellation_supported,
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
        remote_task_allowed: bool,
        remote_task_id: str | None,
        remote_task_started: Any,
        task_poll_min_seconds: float,
        task_poll_max_seconds: float,
    ) -> McpClientCallOutcome:
        del (
            configured,
            name,
            caller_identity,
            run_id,
            task_attempt_id,
            timeout_seconds,
            credentials,
            workspace,
            remote_task_allowed,
            remote_task_id,
            remote_task_started,
            task_poll_min_seconds,
            task_poll_max_seconds,
        )
        self.calls += 1
        await progress(1, 1, "complete")
        if self.failure is not None:
            raise self.failure
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="accepted")],
            structuredContent={"path": arguments["path"]},
        )
        return McpClientCallOutcome(
            output=result.model_dump(mode="json", by_alias=True, exclude_none=True),
            terminal=McpRemoteTaskTerminal.IMMEDIATE,
            reason="remote MCP tool result accepted",
        )

    async def cancel_remote_task(
        self,
        configured: McpConnectionConfig,
        *,
        remote_task_id: str,
        timeout_seconds: float,
        credentials: Any,
        workspace: Path,
    ) -> McpClientCallOutcome:
        del configured, timeout_seconds, credentials, workspace
        return McpClientCallOutcome(
            remote_task_id=remote_task_id,
            terminal=McpRemoteTaskTerminal.CANCELLED,
            reason="remote MCP task cancellation confirmed",
        )


class BlockingClient(FakeClient):
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
        remote_task_allowed: bool,
        remote_task_id: str | None,
        remote_task_started: Any,
        task_poll_min_seconds: float,
        task_poll_max_seconds: float,
    ) -> McpClientCallOutcome:
        del (
            configured,
            name,
            arguments,
            caller_identity,
            run_id,
            task_attempt_id,
            timeout_seconds,
            credentials,
            workspace,
            progress,
            remote_task_allowed,
            remote_task_id,
            remote_task_started,
            task_poll_min_seconds,
            task_poll_max_seconds,
        )
        await asyncio.Event().wait()
        raise AssertionError("cancelled MCP fixture resumed unexpectedly")


class RecoverableRemoteTaskClient(FakeClient):
    def __init__(self, snapshot: McpDiscoverySnapshot) -> None:
        super().__init__(snapshot)
        self.crash_after_binding = True
        self.resumed_task_id: str | None = None

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
        remote_task_allowed: bool,
        remote_task_id: str | None,
        remote_task_started: Any,
        task_poll_min_seconds: float,
        task_poll_max_seconds: float,
    ) -> McpClientCallOutcome:
        del (
            configured,
            name,
            caller_identity,
            run_id,
            task_attempt_id,
            timeout_seconds,
            credentials,
            workspace,
            task_poll_min_seconds,
            task_poll_max_seconds,
        )
        assert remote_task_allowed is True
        if remote_task_id is None:
            remote_task_started("remote-task-1")
            if self.crash_after_binding:
                self.crash_after_binding = False
                raise RuntimeError("simulated transport loss after durable remote identity")
        self.resumed_task_id = remote_task_id
        await progress(1, 1, "remote task completed")
        result = types.CallToolResult(
            content=[types.TextContent(type="text", text="accepted")],
            structuredContent={"path": arguments["path"]},
        )
        return McpClientCallOutcome(
            output=result.model_dump(mode="json", by_alias=True, exclude_none=True),
            remote_task_id=remote_task_id,
            terminal=McpRemoteTaskTerminal.COMPLETED,
            reason="remote MCP task result accepted",
        )


def _service(
    tmp_path: Path,
    client: FakeClient,
    *,
    config: McpConfig | None = None,
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
    return McpService(tmp_path, config or _config(), repository, client, inspector), repository


def _request(
    primitive: McpPrimitiveDescriptor,
    *,
    remote_task_allowed: bool = False,
) -> McpCallRequest:
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
        remote_task_allowed=remote_task_allowed,
        deadline=utc_now() + timedelta(seconds=30),
    )


@pytest.mark.anyio
async def test_connect_invoke_progress_and_exact_replay_are_durable(tmp_path: Path) -> None:
    primitive = _primitive()
    client = FakeClient(_snapshot(primitive))
    service, repository = _service(tmp_path, client)

    connected = await service.connect(
        "graph",
        principal="role:Engineer",
        policy_fingerprint="policy:test-connect",
        credentials={},
    )
    request = _request(primitive)
    result = await service.invoke(request, credentials={})
    replay = await service.invoke(request, credentials={})

    assert connected.state is McpSessionState.READY
    assert connected.server_identity.startswith("stdio:fixture-mcp#sha256:")
    assert connected.credential_references == ()
    assert connected.policy_fingerprint == "policy:test-connect"
    assert connected.revision == 2
    assert result.state is McpCallState.COMPLETED
    assert replay == result
    assert client.calls == 1
    assert repository.progress_after(request.id, 0)[0].message == "complete"
    journal = repository.list_calls()
    assert journal[0]["request"]["id"] == str(request.id)
    assert journal[0]["state"] == McpCallState.COMPLETED.value
    assert journal[0]["result"] == result.model_dump(mode="json")
    assert journal[0]["remote_task_id"] is None


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


@pytest.mark.anyio
async def test_discovered_output_schema_rejects_malformed_structured_result(tmp_path: Path) -> None:
    primitive = _primitive(require_content=True)
    client = FakeClient(_snapshot(primitive))
    service, _repository = _service(tmp_path, client)
    await service.connect("graph", principal="role:Engineer", credentials={})

    result = await service.invoke(_request(primitive), credentials={})

    assert result.state is McpCallState.FAILED
    assert result.output is None
    assert "validation" in result.reason


def test_dynamic_contract_and_common_adapter_preserve_exact_policy_targets(
    tmp_path: Path,
) -> None:
    primitive = _primitive()
    client = FakeClient(_snapshot(primitive))
    service, _repository = _service(tmp_path, client)
    request = _request(primitive)
    external = f"mcp:{request.connection_id}:{request.primitive_name}"
    contract = McpContractFactory(_config()).build("graph", primitive)

    with McpServiceRunner(service) as runner:
        runner.connect("graph", principal="role:Engineer", credentials={})
        adapter = McpPrimitiveToolAdapter(_config(), runner, {})
        result = adapter.invoke(
            AdapterCall(
                arguments={
                    "request": request.model_dump(mode="json"),
                    "executables": ["fixture-mcp"],
                    "network_destinations": [],
                    "external_resources": [external],
                    "credential_refs": [],
                },
                targets=ResolvedTargets(
                    executables=("fixture-mcp",),
                    external_resources=(external,),
                ),
                credentials={},
                execution_id="call-1",
                resources=ResourceRequest(timeout_seconds=30),
                isolation_profile=None,
                cancellation_requested=lambda: False,
                run_id=request.run_id,
                task_attempt_id=request.task_attempt_id,
                acting_identity=request.caller_identity,
                capability=contract.tool_id,
            )
        )

    assert contract.adapter == "mcp.outbound.tool"
    assert contract.adapter_config["schema_hash"] == primitive.schema_hash
    assert contract.target_scopes == ("external_resource", "executable")
    assert result.call_status is CallStatus.COMPLETED
    assert result.external_references == (external,)


def test_lost_non_idempotent_mcp_call_is_never_marked_retryable(tmp_path: Path) -> None:
    primitive = _primitive(McpEffectDisposition.NON_IDEMPOTENT)
    request = _request(primitive)
    external = f"mcp:{request.connection_id}:{request.primitive_name}"
    contract = McpContractFactory(_config()).build("graph", primitive)

    class LostRunner:
        def invoke(self, *_args: object, **_kwargs: object) -> McpCallResult:
            return McpCallResult(
                request_id=request.id,
                connection_id=request.connection_id,
                primitive_name=request.primitive_name,
                state=McpCallState.LOST,
                schema_hash=request.expected_schema_hash,
                reason="transport lost",
            )

    adapter = McpPrimitiveToolAdapter(_config(), LostRunner(), {})  # type: ignore[arg-type]
    result = adapter.invoke(
        AdapterCall(
            arguments={
                "request": request.model_dump(mode="json"),
                "executables": ["fixture-mcp"],
                "network_destinations": [],
                "external_resources": [external],
                "credential_refs": [],
            },
            targets=ResolvedTargets(
                executables=("fixture-mcp",),
                external_resources=(external,),
            ),
            credentials={},
            execution_id="call-lost",
            resources=ResourceRequest(timeout_seconds=30),
            isolation_profile=None,
            cancellation_requested=lambda: False,
            run_id=request.run_id,
            task_attempt_id=request.task_attempt_id,
            acting_identity=request.caller_identity,
            capability=contract.tool_id,
        )
    )

    assert result.call_status is CallStatus.FAILED
    assert result.retryable is False


def test_discovered_mcp_contract_enters_registry_with_runtime_provenance(tmp_path: Path) -> None:
    contract = McpContractFactory(_config()).build("graph", _primitive())
    catalog = ToolCatalog(
        ("package://mishkan.resources.tools/core-catalog.yaml",),
        tmp_path,
        available_adapters=frozenset({"mcp.outbound.tool"}),
        runtime_contracts=(contract,),
    )

    snapshot = catalog.snapshot((contract.tool_id,))

    assert snapshot.require(contract.tool_id) == contract
    assert (
        f"runtime:{contract.source_id}:{contract.tool_id}",
        contract.provenance_fingerprint,
    ) in snapshot.source_fingerprints


def test_runner_cancellation_settles_read_only_call_without_uncertainty(tmp_path: Path) -> None:
    primitive = _primitive()
    client = BlockingClient(_snapshot(primitive))
    service, _repository = _service(tmp_path, client)
    request = _request(primitive)

    with McpServiceRunner(service) as runner:
        runner.connect("graph", principal="role:Engineer", credentials={})
        result = runner.invoke(
            request,
            credentials={},
            cancellation_requested=lambda: True,
            poll_seconds=0.01,
        )

    assert result.state is McpCallState.CANCELLED


@pytest.mark.anyio
async def test_remote_task_identity_survives_restart_and_reconciles_without_replay(
    tmp_path: Path,
) -> None:
    primitive = _primitive()
    snapshot = _snapshot(
        primitive,
        task_tool_calls_supported=True,
        task_cancellation_supported=True,
    )
    client = RecoverableRemoteTaskClient(snapshot)
    config = _config(remote_tasks_enabled=True)
    service, repository = _service(tmp_path, client, config=config)
    connected = await service.connect("graph", principal="role:Engineer", credentials={})
    request = _request(primitive, remote_task_allowed=True)

    assert connected.remote_tasks_enabled is True
    assert connected.task_tool_calls_supported is True
    assert connected.task_cancellation_supported is True
    with pytest.raises(MishkanError) as interrupted:
        await service.invoke(request, credentials={})

    assert interrupted.value.envelope.retryable is True
    pending = repository.list_calls()[0]
    assert pending["state"] == McpCallState.RUNNING.value
    assert pending["remote_task_id"] == "remote-task-1"
    assert pending["result"] is None
    assert service.reconcile_after_restart() == ()

    resumed_service = McpService(
        tmp_path,
        config,
        McpRepository(tmp_path / "mishkan.db"),
        client,
        ContentInspector(
            InspectionProfile(
                profile_id="test",
                revision="1",
                adoption_authority="test",
                rules=(),
            )
        ),
    )
    with McpServiceRunner(resumed_service) as runner:
        result = runner.resume_remote_task(request.id, credentials={})

    assert result.state is McpCallState.COMPLETED
    assert result.remote_task_id == "remote-task-1"
    assert client.resumed_task_id == "remote-task-1"
    assert repository.reserve_call(request).existing_result == result


@pytest.mark.anyio
async def test_remote_task_cancellation_requires_negotiated_remote_confirmation(
    tmp_path: Path,
) -> None:
    primitive = _primitive()
    client = RecoverableRemoteTaskClient(
        _snapshot(
            primitive,
            task_tool_calls_supported=True,
            task_cancellation_supported=True,
        )
    )
    config = _config(remote_tasks_enabled=True)
    service, _repository = _service(tmp_path, client, config=config)
    await service.connect("graph", principal="role:Engineer", credentials={})
    request = _request(primitive, remote_task_allowed=True)
    with pytest.raises(MishkanError):
        await service.invoke(request, credentials={})

    with McpServiceRunner(service) as runner:
        result = runner.cancel_remote_task(request.id, credentials={})

    assert result.state is McpCallState.CANCELLED
    assert result.remote_task_id == "remote-task-1"
