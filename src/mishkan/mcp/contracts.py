"""Derive registry contracts from bound MCP discovery without trusting server authority."""

from __future__ import annotations

import hashlib
import math
import re

from mishkan.config.models import McpConfig, McpTransport
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.models import (
    McpCallRequest,
    McpCallResult,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
)
from mishkan.policy.models import ResourceRequest
from mishkan.tools.models import EffectClass, Idempotency, SourceKind, ToolContract


class McpContractFactory:
    """Create candidate contracts; registry adoption and policy remain separate decisions."""

    def __init__(self, config: McpConfig) -> None:
        self._config = config

    def build(
        self,
        connection_id: str,
        primitive: McpPrimitiveDescriptor,
    ) -> ToolContract:
        configured = self._config.connections.get(connection_id)
        if configured is None or not configured.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        if primitive.connection_id != connection_id or primitive.kind is not McpPrimitiveKind.TOOL:
            raise MishkanError(ErrorCode.TOOL_SCHEMA, "MCP primitive is not an outbound tool")
        exposure = self._config.exposure_profiles[configured.exposure_profile]
        if primitive.name not in exposure.operations:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "MCP primitive is outside the configured exposure profile",
            )
        suffix = hashlib.sha256(
            f"{connection_id}\0{primitive.name}\0{primitive.schema_hash}".encode()
        ).hexdigest()[:10]
        connection_slug = self._slug(connection_id, 32)
        primitive_slug = self._slug(primitive.name, 48)
        tool_id = f"mcp.{connection_slug}.{primitive_slug}.{suffix}"
        crewai_name = f"mcp_{connection_slug}_{primitive_slug}_{suffix}".replace(".", "_")[:64]
        target_scopes = ["external_resource"]
        target_arguments: dict[str, tuple[str, ...]] = {
            "external_resource": ("external_resources",)
        }
        if configured.transport is McpTransport.STDIO:
            target_scopes.append("executable")
            target_arguments["executable"] = ("executables",)
            effect_class = EffectClass.COMMAND
        else:
            target_scopes.append("network")
            target_arguments["network"] = ("network_destinations",)
            effect_class = EffectClass.NETWORK
        request_schema = McpCallRequest.model_json_schema()
        definitions = request_schema.pop("$defs", {})
        input_schema = {
            "type": "object",
            "additionalProperties": False,
            "$defs": definitions,
            "required": [
                "request",
                "executables",
                "network_destinations",
                "external_resources",
                "credential_refs",
            ],
            "properties": {
                "request": request_schema,
                "executables": {"type": "array", "items": {"type": "string"}},
                "network_destinations": {"type": "array", "items": {"type": "string"}},
                "external_resources": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 1,
                },
                "credential_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
            },
        }
        return ToolContract(
            tool_id=tool_id,
            version="1.0.0",
            crewai_name=crewai_name,
            summary="Invoke one explicitly bound and governed MCP tool primitive.",
            effect_class=effect_class,
            source_id=f"mcp.{connection_slug}",
            source_kind=SourceKind.MCP,
            adapter="mcp.outbound.tool",
            input_schema=input_schema,
            result_schema=McpCallResult.model_json_schema(),
            timeout_behavior="uncertain_after_dispatch",
            idempotency=Idempotency.DEDUPLICATION_KEY,
            target_scopes=tuple(target_scopes),
            target_arguments=target_arguments,
            credential_refs=tuple(item.locator for item in configured.credential_refs),
            credential_arguments=("credential_refs",),
            policy_arguments=("request.declared_effects",),
            resources=ResourceRequest(
                timeout_seconds=math.ceil(configured.call_timeout_seconds),
                network=configured.transport is McpTransport.STREAMABLE_HTTP,
                concurrency=1,
            ),
            adapter_config={
                "connection_id": connection_id,
                "primitive_name": primitive.name,
                "schema_hash": primitive.schema_hash,
                "effect_disposition": primitive.effect_disposition.value,
            },
        )

    @staticmethod
    def _slug(value: str, limit: int) -> str:
        normalized = re.sub(r"[^a-z0-9_.-]+", "-", value.casefold()).strip("-._")
        return (normalized or "primitive")[:limit]
