"""Official MCP SDK transports behind MISHKAN's normalized boundary."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path
from typing import Any

import anyio
import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.session import ProgressFnT

from mishkan.config.models import McpConnectionConfig, McpTransport, NetworkProfileConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.models import (
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
)
from mishkan.web.network import GuardedAsyncHTTPTransport, NetworkGuard


class McpSdkClient:
    """Open bounded SDK sessions without persisting transport secrets or handles."""

    def __init__(self, network_profiles: Mapping[str, NetworkProfileConfig]) -> None:
        self._network_profiles = dict(network_profiles)

    async def discover(
        self,
        connection_id: str,
        configured: McpConnectionConfig,
        *,
        credentials: Mapping[str, str],
        workspace: Path,
    ) -> McpDiscoverySnapshot:
        async with self._session(configured, credentials=credentials, workspace=workspace) as (
            session,
            protocol_version,
        ):
            tools = await self._all_pages(session.list_tools, "tools")
            resources = await self._all_pages(session.list_resources, "resources")
            prompts = await self._all_pages(session.list_prompts, "prompts")
        primitives = (
            tuple(self._tool(connection_id, protocol_version, item) for item in tools)
            + tuple(self._resource(connection_id, protocol_version, item) for item in resources)
            + tuple(self._prompt(connection_id, protocol_version, item) for item in prompts)
        )
        return self._snapshot(connection_id, protocol_version, primitives)

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
        credentials: Mapping[str, str],
        workspace: Path,
        progress: ProgressFnT,
    ) -> types.CallToolResult:
        async with self._session(configured, credentials=credentials, workspace=workspace) as (
            session,
            _protocol_version,
        ):
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
                progress_callback=progress,
                meta={
                    "mishkan": {
                        "caller_identity": caller_identity,
                        "run_id": run_id,
                        "task_attempt_id": task_attempt_id,
                    }
                },
            )
        encoded = json.dumps(
            result.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > configured.max_result_bytes:
            raise MishkanError(
                ErrorCode.MCP,
                "MCP result exceeds the configured transport bound",
                details={"received_bytes": len(encoded), "limit": configured.max_result_bytes},
            )
        return result

    @asynccontextmanager
    async def _session(
        self,
        configured: McpConnectionConfig,
        *,
        credentials: Mapping[str, str],
        workspace: Path,
    ) -> AsyncIterator[tuple[ClientSession, str]]:
        resolved = self._require_credentials(configured, credentials)
        client_info = types.Implementation(name="mishkan", version=version("mishkan"))
        timeout = timedelta(seconds=configured.call_timeout_seconds)
        if configured.transport is McpTransport.STDIO:
            assert configured.command is not None
            inherited = get_default_environment()
            environment = {name: "" for name in inherited}
            environment.update(
                {
                    name: os.environ[name]
                    for name in configured.inherit_environment
                    if name in os.environ
                }
            )
            environment.update(
                {
                    name: resolved[reference.locator]
                    for name, reference in configured.environment.items()
                }
            )
            parameters = StdioServerParameters(
                command=configured.command,
                args=list(configured.arguments),
                env=environment,
                cwd=workspace,
            )
            async with (
                stdio_client(parameters) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timeout,
                    client_info=client_info,
                ) as session,
            ):
                with anyio.fail_after(configured.connect_timeout_seconds):
                    initialized = await session.initialize()
                protocol = str(initialized.protocolVersion)
                self._require_protocol(configured, protocol)
                yield session, protocol
            return

        assert configured.endpoint is not None
        assert configured.network_profile is not None
        try:
            profile = self._network_profiles[configured.network_profile]
        except KeyError as exc:
            raise MishkanError(ErrorCode.MCP, "MCP network profile is unavailable") from exc
        guard = NetworkGuard(profile)
        endpoint = guard.validate_url(str(configured.endpoint)).value
        headers = {
            name: resolved[reference.locator] for name, reference in configured.headers.items()
        }
        transport = GuardedAsyncHTTPTransport(guard)
        http_timeout = httpx.Timeout(
            configured.call_timeout_seconds,
            connect=configured.connect_timeout_seconds,
        )
        async with (
            httpx.AsyncClient(
                transport=transport,
                headers=headers,
                timeout=http_timeout,
                follow_redirects=False,
            ) as http_client,
            streamable_http_client(
                endpoint,
                http_client=http_client,
                terminate_on_close=True,
            ) as (read_stream, write_stream, _session_id),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timeout,
                client_info=client_info,
            ) as session,
        ):
            with anyio.fail_after(configured.connect_timeout_seconds):
                initialized = await session.initialize()
            protocol = str(initialized.protocolVersion)
            self._require_protocol(configured, protocol)
            yield session, protocol

    @staticmethod
    def _require_credentials(
        configured: McpConnectionConfig,
        credentials: Mapping[str, str],
    ) -> dict[str, str]:
        required = {item.locator for item in configured.credential_refs}
        if set(credentials) != required:
            raise MishkanError(
                ErrorCode.AUTHORIZATION_MISSING,
                "resolved MCP credentials differ from configured references",
                details={"required": sorted(required), "received": sorted(credentials)},
            )
        return dict(credentials)

    @staticmethod
    def _require_protocol(configured: McpConnectionConfig, protocol: str) -> None:
        if protocol not in configured.protocol_versions:
            raise MishkanError(
                ErrorCode.MCP,
                "MCP server negotiated an unconfigured protocol version",
                details={"negotiated": protocol},
            )

    @staticmethod
    async def _all_pages(
        operation: Callable[..., Awaitable[Any]],
        attribute: str,
    ) -> tuple[Any, ...]:
        cursor: str | None = None
        values: list[Any] = []
        while True:
            page = await operation(cursor=cursor)
            values.extend(getattr(page, attribute))
            cursor = page.nextCursor
            if cursor is None:
                return tuple(values)

    @classmethod
    def _tool(
        cls,
        connection_id: str,
        protocol: str,
        item: types.Tool,
    ) -> McpPrimitiveDescriptor:
        annotations = (
            item.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if item.annotations is not None
            else {}
        )
        return cls._primitive(
            connection_id,
            protocol,
            McpPrimitiveKind.TOOL,
            item.name,
            item.title,
            item.description,
            item.inputSchema,
            item.outputSchema,
            annotations,
        )

    @classmethod
    def _resource(
        cls,
        connection_id: str,
        protocol: str,
        item: types.Resource,
    ) -> McpPrimitiveDescriptor:
        annotations = (
            item.annotations.model_dump(mode="json", by_alias=True, exclude_none=True)
            if item.annotations is not None
            else {}
        )
        annotations.update({"uri": str(item.uri), "mimeType": item.mimeType})
        return cls._primitive(
            connection_id,
            protocol,
            McpPrimitiveKind.RESOURCE,
            item.name,
            item.title,
            item.description,
            None,
            None,
            annotations,
        )

    @classmethod
    def _prompt(
        cls,
        connection_id: str,
        protocol: str,
        item: types.Prompt,
    ) -> McpPrimitiveDescriptor:
        properties = {
            argument.name: {"type": "string", "description": argument.description}
            for argument in item.arguments or ()
        }
        required = [argument.name for argument in item.arguments or () if argument.required]
        input_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": properties,
            "required": required,
        }
        return cls._primitive(
            connection_id,
            protocol,
            McpPrimitiveKind.PROMPT,
            item.name,
            item.title,
            item.description,
            input_schema,
            None,
            {},
        )

    @staticmethod
    def _primitive(
        connection_id: str,
        protocol: str,
        kind: McpPrimitiveKind,
        name: str,
        title: str | None,
        description: str | None,
        input_schema: dict[str, Any] | None,
        output_schema: dict[str, Any] | None,
        annotations: dict[str, Any],
    ) -> McpPrimitiveDescriptor:
        disposition = McpSdkClient._disposition(annotations)
        schema_hash = McpPrimitiveDescriptor.claim_hash(
            kind,
            name,
            input_schema,
            output_schema,
            annotations,
        )
        return McpPrimitiveDescriptor(
            connection_id=connection_id,
            protocol_version=protocol,
            kind=kind,
            name=name,
            title=title,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            annotations=annotations,
            effect_disposition=disposition,
            schema_hash=schema_hash,
            provenance=f"mcp:{connection_id}:{protocol}",
        )

    @staticmethod
    def _disposition(annotations: Mapping[str, Any]) -> McpEffectDisposition:
        if annotations.get("readOnlyHint") is True:
            return McpEffectDisposition.READ_ONLY
        if annotations.get("destructiveHint") is True:
            return McpEffectDisposition.NON_IDEMPOTENT
        if annotations.get("idempotentHint") is True:
            return McpEffectDisposition.IDEMPOTENT
        return McpEffectDisposition.UNKNOWN

    @staticmethod
    def _snapshot(
        connection_id: str,
        protocol: str,
        primitives: tuple[McpPrimitiveDescriptor, ...],
    ) -> McpDiscoverySnapshot:
        normalized = [
            {"kind": item.kind.value, "name": item.name, "schema_hash": item.schema_hash}
            for item in sorted(primitives, key=lambda item: (item.kind, item.name))
        ]
        import hashlib

        fingerprint = hashlib.sha256(
            json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return McpDiscoverySnapshot(
            connection_id=connection_id,
            protocol_version=protocol,
            primitives=primitives,
            schema_fingerprint=fingerprint,
        )
