"""Official MCP SDK transports behind MISHKAN's normalized boundary."""

from __future__ import annotations

import asyncio
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
from mcp.client.session import ElicitationFnT
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.session import ProgressFnT

from mishkan.config.models import McpConnectionConfig, McpTransport, NetworkProfileConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.models import (
    McpClientCallOutcome,
    McpDiscoverySnapshot,
    McpEffectDisposition,
    McpPrimitiveDescriptor,
    McpPrimitiveKind,
    McpRemoteTaskTerminal,
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
            capabilities,
        ):
            tools = await self._all_pages(session.list_tools, "tools")
            resources = await self._all_pages(session.list_resources, "resources")
            prompts = await self._all_pages(session.list_prompts, "prompts")
        primitives = (
            tuple(self._tool(connection_id, protocol_version, item) for item in tools)
            + tuple(self._resource(connection_id, protocol_version, item) for item in resources)
            + tuple(self._prompt(connection_id, protocol_version, item) for item in prompts)
        )
        task_tools, task_cancel = self._task_capabilities(capabilities)
        return self._snapshot(
            connection_id,
            protocol_version,
            primitives,
            task_tool_calls_supported=task_tools,
            task_cancellation_supported=task_cancel,
        )

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
        elicitation: ElicitationFnT | None = None,
        remote_task_allowed: bool = False,
        remote_task_id: str | None = None,
        remote_task_started: Callable[[str], None] | None = None,
        task_poll_min_seconds: float | None = None,
        task_poll_max_seconds: float | None = None,
    ) -> McpClientCallOutcome:
        async with self._session(
            configured,
            credentials=credentials,
            workspace=workspace,
            elicitation=elicitation,
        ) as (
            session,
            _protocol_version,
            capabilities,
        ):
            metadata = {
                "mishkan": {
                    "caller_identity": caller_identity,
                    "run_id": run_id,
                    "task_attempt_id": task_attempt_id,
                }
            }
            if remote_task_allowed or remote_task_id is not None:
                if (
                    remote_task_started is None
                    or task_poll_min_seconds is None
                    or task_poll_max_seconds is None
                ):
                    raise MishkanError(
                        ErrorCode.CONFIGURATION,
                        "MCP remote task execution requires public polling bounds and journaling",
                    )
                return await self._call_remote_task(
                    session,
                    configured,
                    capabilities,
                    name=name,
                    arguments=arguments,
                    timeout_seconds=timeout_seconds,
                    metadata=metadata,
                    progress=progress,
                    remote_task_id=remote_task_id,
                    remote_task_started=remote_task_started,
                    task_poll_min_seconds=task_poll_min_seconds,
                    task_poll_max_seconds=task_poll_max_seconds,
                )
            result = await session.call_tool(
                name,
                arguments,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
                progress_callback=progress,
                meta=metadata,
            )
        return self._outcome(configured, result, None, McpRemoteTaskTerminal.IMMEDIATE)

    async def _call_remote_task(
        self,
        session: ClientSession,
        configured: McpConnectionConfig,
        capabilities: types.ServerCapabilities,
        *,
        name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        metadata: dict[str, Any],
        progress: ProgressFnT,
        remote_task_id: str | None,
        remote_task_started: Callable[[str], None],
        task_poll_min_seconds: float,
        task_poll_max_seconds: float,
    ) -> McpClientCallOutcome:
        task_tools, task_cancel = self._task_capabilities(capabilities)
        if not configured.remote_tasks_enabled or not task_tools:
            raise MishkanError(
                ErrorCode.MCP,
                "MCP remote task execution is not configured and negotiated",
            )
        task_id = remote_task_id
        try:
            with anyio.fail_after(timeout_seconds):
                if task_id is None:
                    ttl = max(1, int(timeout_seconds * 1_000))
                    created = await session.send_request(
                        types.ClientRequest(
                            types.CallToolRequest(
                                params=types.CallToolRequestParams(
                                    name=name,
                                    arguments=arguments,
                                    task=types.TaskMetadata(ttl=ttl),
                                    _meta=types.RequestParams.Meta(**metadata),
                                )
                            )
                        ),
                        types.CreateTaskResult,
                    )
                    task_id = created.task.taskId
                    remote_task_started(task_id)
                    status: types.Task = created.task
                else:
                    status = await self._get_task(session, task_id)
                cursor = 0
                while True:
                    await progress(
                        float(cursor),
                        None,
                        status.statusMessage or f"remote MCP task {status.status}",
                    )
                    cursor += 1
                    if status.status == types.TASK_STATUS_COMPLETED:
                        result = await self._get_task_result(session, task_id)
                        return self._outcome(
                            configured,
                            result,
                            task_id,
                            McpRemoteTaskTerminal.COMPLETED,
                        )
                    if status.status == types.TASK_STATUS_FAILED:
                        return McpClientCallOutcome(
                            remote_task_id=task_id,
                            terminal=McpRemoteTaskTerminal.FAILED,
                            reason=status.statusMessage or "remote MCP task failed",
                        )
                    if status.status == types.TASK_STATUS_CANCELLED:
                        return McpClientCallOutcome(
                            remote_task_id=task_id,
                            terminal=McpRemoteTaskTerminal.CANCELLED,
                            reason=status.statusMessage or "remote MCP task was cancelled",
                        )
                    interval = (status.pollInterval or int(task_poll_min_seconds * 1_000)) / 1_000
                    await anyio.sleep(
                        min(task_poll_max_seconds, max(task_poll_min_seconds, interval))
                    )
                    status = await self._get_task(session, task_id)
        except asyncio.CancelledError:
            if task_id is None or not task_cancel:
                raise MishkanError(
                    ErrorCode.MCP,
                    "remote MCP task cancellation could not be confirmed",
                    retryable=True,
                ) from None
            try:
                cancelled = await session.send_request(
                    types.ClientRequest(
                        types.CancelTaskRequest(
                            params=types.CancelTaskRequestParams(taskId=task_id)
                        )
                    ),
                    types.CancelTaskResult,
                )
            except Exception as exc:
                raise MishkanError(
                    ErrorCode.MCP,
                    "remote MCP task cancellation could not be confirmed",
                    details={"reason": type(exc).__name__},
                    retryable=True,
                ) from exc
            if cancelled.status != types.TASK_STATUS_CANCELLED:
                raise MishkanError(
                    ErrorCode.MCP,
                    "remote MCP task cancellation did not reach a terminal cancelled state",
                    retryable=True,
                ) from None
            return McpClientCallOutcome(
                remote_task_id=task_id,
                terminal=McpRemoteTaskTerminal.CANCELLED,
                reason=cancelled.statusMessage or "remote MCP task cancellation confirmed",
            )

    async def cancel_remote_task(
        self,
        configured: McpConnectionConfig,
        *,
        remote_task_id: str,
        timeout_seconds: float,
        credentials: Mapping[str, str],
        workspace: Path,
    ) -> McpClientCallOutcome:
        async with self._session(configured, credentials=credentials, workspace=workspace) as (
            session,
            _protocol_version,
            capabilities,
        ):
            _task_tools, task_cancel = self._task_capabilities(capabilities)
            if not configured.remote_tasks_enabled or not task_cancel:
                raise MishkanError(
                    ErrorCode.MCP,
                    "MCP remote task cancellation is not configured and negotiated",
                )
            with anyio.fail_after(timeout_seconds):
                cancelled = await session.send_request(
                    types.ClientRequest(
                        types.CancelTaskRequest(
                            params=types.CancelTaskRequestParams(taskId=remote_task_id)
                        )
                    ),
                    types.CancelTaskResult,
                )
        if cancelled.status != types.TASK_STATUS_CANCELLED:
            raise MishkanError(
                ErrorCode.MCP,
                "remote MCP task cancellation was not confirmed",
                retryable=True,
            )
        return McpClientCallOutcome(
            remote_task_id=remote_task_id,
            terminal=McpRemoteTaskTerminal.CANCELLED,
            reason=cancelled.statusMessage or "remote MCP task cancellation confirmed",
        )

    @staticmethod
    async def _get_task(session: ClientSession, task_id: str) -> types.GetTaskResult:
        return await session.send_request(
            types.ClientRequest(
                types.GetTaskRequest(params=types.GetTaskRequestParams(taskId=task_id))
            ),
            types.GetTaskResult,
        )

    @staticmethod
    async def _get_task_result(session: ClientSession, task_id: str) -> types.CallToolResult:
        return await session.send_request(
            types.ClientRequest(
                types.GetTaskPayloadRequest(
                    params=types.GetTaskPayloadRequestParams(taskId=task_id)
                )
            ),
            types.CallToolResult,
        )

    @staticmethod
    def _outcome(
        configured: McpConnectionConfig,
        result: types.CallToolResult,
        remote_task_id: str | None,
        terminal: McpRemoteTaskTerminal,
    ) -> McpClientCallOutcome:
        output = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        encoded = json.dumps(output, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > configured.max_result_bytes:
            raise MishkanError(
                ErrorCode.MCP,
                "MCP result exceeds the configured transport bound",
                details={"received_bytes": len(encoded), "limit": configured.max_result_bytes},
            )
        failed = bool(result.isError)
        return McpClientCallOutcome(
            output=output,
            remote_task_id=remote_task_id,
            terminal=McpRemoteTaskTerminal.FAILED if failed else terminal,
            reason=(
                "remote MCP server returned a tool error"
                if failed
                else "remote MCP tool result accepted"
            ),
        )

    @asynccontextmanager
    async def _session(
        self,
        configured: McpConnectionConfig,
        *,
        credentials: Mapping[str, str],
        workspace: Path,
        elicitation: ElicitationFnT | None = None,
    ) -> AsyncIterator[tuple[ClientSession, str, types.ServerCapabilities]]:
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
                    elicitation_callback=elicitation,
                    client_info=client_info,
                ) as session,
            ):
                with anyio.fail_after(configured.connect_timeout_seconds):
                    initialized = await session.initialize()
                protocol = str(initialized.protocolVersion)
                self._require_protocol(configured, protocol)
                yield session, protocol, initialized.capabilities
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
                elicitation_callback=elicitation,
                client_info=client_info,
            ) as session,
        ):
            with anyio.fail_after(configured.connect_timeout_seconds):
                initialized = await session.initialize()
            protocol = str(initialized.protocolVersion)
            self._require_protocol(configured, protocol)
            yield session, protocol, initialized.capabilities

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
        *,
        task_tool_calls_supported: bool,
        task_cancellation_supported: bool,
    ) -> McpDiscoverySnapshot:
        fingerprint = McpDiscoverySnapshot.claim_fingerprint(
            primitives,
            task_tool_calls_supported=task_tool_calls_supported,
            task_cancellation_supported=task_cancellation_supported,
        )
        return McpDiscoverySnapshot(
            connection_id=connection_id,
            protocol_version=protocol,
            primitives=primitives,
            schema_fingerprint=fingerprint,
            task_tool_calls_supported=task_tool_calls_supported,
            task_cancellation_supported=task_cancellation_supported,
        )

    @staticmethod
    def _task_capabilities(capabilities: types.ServerCapabilities) -> tuple[bool, bool]:
        tasks = capabilities.tasks
        requests = tasks.requests if tasks is not None else None
        tools = requests.tools if requests is not None else None
        return (
            tools is not None and tools.call is not None,
            tasks is not None and tasks.cancel is not None,
        )
