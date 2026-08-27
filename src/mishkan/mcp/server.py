"""Authenticated, stateless MCP facade over the mishkand application authority."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from importlib.metadata import version
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyUrl
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mishkan.application import ApplicationCommand
from mishkan.daemon.auth import TokenFile
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.facade import EventQuery, McpFacadePort, McpFacadeRouter, RunQuery

_principal: ContextVar[str | None] = ContextVar("mishkan_mcp_principal", default=None)


class BearerAuthenticatedMcpApp:
    """Authenticate each inbound request before entering the MCP transport."""

    def __init__(self, app: ASGIApp, token_file: TokenFile) -> None:
        self._app = app
        self._token_file = token_file

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        scheme, separator, credential = authorization.partition(" ")
        record = (
            self._token_file.authenticate(credential)
            if separator and scheme.lower() == "bearer" and credential
            else None
        )
        if record is None:
            response = JSONResponse(
                status_code=403,
                content={
                    "code": ErrorCode.AUTHORITY_NOT_GRANTED.value,
                    "message": "authenticated MCP client identity is required",
                },
            )
            await response(scope, receive, send)
            return
        token = _principal.set(record.principal_id)
        try:
            await self._app(scope, receive, send)
        finally:
            _principal.reset(token)


class McpHttpFacade:
    """Translate official MCP protocol operations into facade-router calls."""

    def __init__(
        self,
        router: McpFacadeRouter,
        token_file: TokenFile,
        *,
        daemon_host: str,
        daemon_port: int,
    ) -> None:
        self.protocol = McpProtocolFacade(router, self._require_principal)
        authority = f"{daemon_host}:{daemon_port}"
        self._manager = StreamableHTTPSessionManager(
            self.protocol.server,
            json_response=True,
            stateless=True,
            security_settings=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=[authority],
                allowed_origins=[f"http://{authority}"],
            ),
        )
        self.app = BearerAuthenticatedMcpApp(self._manager.handle_request, token_file)

    @asynccontextmanager
    async def lifespan(self) -> AsyncIterator[None]:
        async with self._manager.run():
            yield

    @staticmethod
    def _require_principal() -> str:
        principal = _principal.get()
        if principal is None:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "MCP request has no authenticated daemon principal",
            )
        return principal


class McpProtocolFacade:
    """Protocol handlers shared by HTTP and stateless STDIO transports."""

    def __init__(self, router: McpFacadePort, principal: Callable[[], str]) -> None:
        self._router = router
        self._principal = principal
        self.server: Server[None] = Server(
            "mishkan",
            version=version("mishkan"),
            instructions=(
                "MISHKAN application facade. Discovery never grants authority; "
                "all mutations remain governed daemon commands."
            ),
        )
        self._register_handlers(self.server)

    def _register_handlers(self, server: Server[None]) -> None:
        @server.list_tools()  # type: ignore[untyped-decorator,no-untyped-call]
        async def list_tools() -> list[types.Tool]:
            schemas = self._operation_schemas()
            descriptions = {
                "system.health": "Read the daemon health and schema revision.",
                "system.snapshot": "Read a bounded, cursor-consistent daemon snapshot.",
                "events.list": "Read a bounded page of durable application events.",
                "run.get": "Read one durable run projection by identifier.",
                "command.submit": "Submit one governed, idempotent application command.",
            }
            return [
                types.Tool(
                    name=name,
                    description=descriptions[name],
                    inputSchema=schemas[name],
                )
                for name in self._router.operations
            ]

        @server.call_tool()  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            try:
                result = await self._router.invoke(
                    name,
                    arguments,
                    principal_id=self._principal(),
                )
            except MishkanError as error:
                envelope = error.envelope.model_dump(mode="json")
                return types.CallToolResult(
                    isError=True,
                    content=[types.TextContent(type="text", text=json.dumps(envelope))],
                    structuredContent={"error": envelope},
                )
            return types.CallToolResult(
                isError=False,
                content=[types.TextContent(type="text", text=json.dumps(result))],
                structuredContent=result,
            )

        @server.list_resources()  # type: ignore[untyped-decorator,no-untyped-call]
        async def list_resources() -> list[types.Resource]:
            labels = {
                "mishkan://snapshot": "MISHKAN snapshot",
                "mishkan://runs": "MISHKAN runs",
                "mishkan://events": "MISHKAN events",
            }
            return [
                types.Resource(
                    uri=AnyUrl(uri),
                    name=labels[uri],
                    mimeType="application/json",
                    description="Bounded authoritative mishkand projection.",
                )
                for uri in self._router.resources
            ]

        @server.read_resource()  # type: ignore[untyped-decorator,no-untyped-call]
        async def read_resource(uri: Any) -> list[ReadResourceContents]:
            result = await self._router.read_resource(
                str(uri),
                principal_id=self._principal(),
            )
            return [
                ReadResourceContents(
                    content=json.dumps(result, sort_keys=True, separators=(",", ":")),
                    mime_type="application/json",
                )
            ]

        @server.list_prompts()  # type: ignore[untyped-decorator,no-untyped-call]
        async def list_prompts() -> list[types.Prompt]:
            return []

    @staticmethod
    def _operation_schemas() -> dict[str, dict[str, Any]]:
        empty = {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
        return {
            "system.health": empty,
            "system.snapshot": empty,
            "events.list": EventQuery.model_json_schema(),
            "run.get": RunQuery.model_json_schema(),
            "command.submit": ApplicationCommand.model_json_schema(),
        }
