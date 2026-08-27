"""CapabilityGateway adapter for dynamically bound outbound MCP primitives."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from mishkan.config.models import McpConfig, McpTransport
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.models import McpCallRequest, McpCallState
from mishkan.mcp.runner import McpServiceRunner
from mishkan.tools.adapters import AdapterCall, CapabilityAdapter
from mishkan.tools.gateway_models import AdapterResult, CallStatus


class McpPrimitiveToolAdapter:
    adapter_id = "mcp.outbound.tool"

    def __init__(
        self,
        config: McpConfig,
        runner: McpServiceRunner,
        network_origins: Mapping[str, str],
    ) -> None:
        self._config = config
        self._runner = runner
        self._network_origins = dict(network_origins)

    def invoke(self, call: AdapterCall) -> AdapterResult:
        try:
            request = McpCallRequest.model_validate(call.arguments["request"])
        except (KeyError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.TOOL_SCHEMA,
                "MCP tool request does not match its public schema",
            ) from exc
        if (
            request.caller_identity != call.acting_identity
            or request.run_id != call.run_id
            or request.task_attempt_id != call.task_attempt_id
        ):
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "MCP caller identity differs from the authorized invocation",
            )
        configured = self._config.connections.get(request.connection_id)
        if configured is None or not configured.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        references = tuple(item.locator for item in configured.credential_refs)
        declared = call.arguments.get("credential_refs")
        if declared != list(references) or tuple(call.credentials) != references:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "MCP credential declarations differ")
        external = (f"mcp:{request.connection_id}:{request.primitive_name}",)
        executables = (
            (configured.command,)
            if configured.transport is McpTransport.STDIO and configured.command is not None
            else ()
        )
        network = (
            (self._require_network_origin(request.connection_id),)
            if configured.transport is McpTransport.STREAMABLE_HTTP
            else ()
        )
        if (
            call.targets.paths
            or call.targets.repositories
            or call.targets.remotes
            or call.targets.branches
            or call.targets.environments
            or call.targets.executables != executables
            or call.targets.network_destinations != network
            or call.targets.external_resources != external
        ):
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "MCP resolved targets differ")
        result = self._runner.invoke(
            request,
            credentials=call.credentials,
            cancellation_requested=call.cancellation_requested,
            poll_seconds=self._config.cancellation_poll_seconds,
        )
        status = {
            McpCallState.COMPLETED: CallStatus.COMPLETED,
            McpCallState.FAILED: CallStatus.FAILED,
            McpCallState.CANCELLED: CallStatus.CANCELLED,
            McpCallState.LOST: CallStatus.FAILED,
            McpCallState.UNCERTAIN: CallStatus.UNCERTAIN,
        }.get(result.state)
        if status is None:
            raise MishkanError(ErrorCode.MCP, "MCP result has no terminal Gateway status")
        return AdapterResult(
            output=result.model_dump(mode="json"),
            actual_targets=call.targets,
            external_references=(external[0], *result.content_artifact_references),
            evidence={
                "adapter": self.adapter_id,
                "connection_id": request.connection_id,
                "schema_hash": result.schema_hash,
            },
            call_status=status,
            retryable=result.state is McpCallState.LOST,
            error_code=result.error_code,
            reason=result.reason,
        )

    def _require_network_origin(self, connection_id: str) -> str:
        try:
            return self._network_origins[connection_id]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "MCP HTTP connection has no normalized network origin",
            ) from exc


def build_mcp_tool_adapters(
    config: McpConfig,
    runner: McpServiceRunner,
    network_origins: Mapping[str, str],
) -> dict[str, CapabilityAdapter]:
    adapter = McpPrimitiveToolAdapter(config, runner, network_origins)
    return {adapter.adapter_id: adapter}
