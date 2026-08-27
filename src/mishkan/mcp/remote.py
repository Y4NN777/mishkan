"""Stateless facade client used by non-authoritative MCP bridges."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from mishkan.application import ApplicationCommand
from mishkan.config.models import DaemonConfig, McpConfig
from mishkan.daemon.auth import TokenFile
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.mcp.facade import EventQuery, RunQuery


class DaemonMcpFacade:
    """Forward exposed MCP operations to the authenticated mishkand HTTP API."""

    _SUPPORTED = frozenset(
        {"system.health", "system.snapshot", "events.list", "run.get", "command.submit"}
    )
    _RESOURCES = frozenset({"mishkan://snapshot", "mishkan://runs", "mishkan://events"})

    def __init__(
        self,
        mcp: McpConfig,
        daemon: DaemonConfig,
        token_file: TokenFile,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        profile = mcp.exposure_profiles[mcp.facade.exposure_profile]
        self.operations = tuple(item for item in profile.operations if item in self._SUPPORTED)
        self.resources = tuple(item for item in profile.resources if item in self._RESOURCES)
        host = f"[{daemon.host}]" if ":" in daemon.host else daemon.host
        self._base_url = f"http://{host}:{daemon.port}"
        self._token_file = token_file
        self._timeout = daemon.request_timeout_seconds
        self._transport = transport

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]:
        self._require_operation(operation)
        record = self._token_file.read()
        if record.principal_id != principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "STDIO bridge principal differs from the daemon token identity",
            )
        if operation == "system.health":
            self._require_empty(arguments)
            return await self._request_object("GET", "/v1/health")
        if operation == "system.snapshot":
            self._require_empty(arguments)
            return await self._request_object("GET", "/v1/snapshot")
        if operation == "events.list":
            query = self._validate(EventQuery, arguments)
            params: list[tuple[str, str | int]] = [
                ("after", query.after),
                ("limit", query.limit),
            ]
            params.extend(("event_type", value) for value in query.event_types)
            if query.entity_type is not None:
                params.append(("entity_type", query.entity_type))
            if query.entity_id is not None:
                params.append(("entity_id", query.entity_id))
            return await self._request_object("GET", "/v1/events", params=params)
        if operation == "run.get":
            query = self._validate(RunQuery, arguments)
            runs = await self._request("GET", "/v1/runs", params={"offset": 0, "limit": 1000})
            if not isinstance(runs, list):
                raise MishkanError(ErrorCode.MCP, "daemon returned an invalid run collection")
            found = next(
                (
                    item
                    for item in runs
                    if isinstance(item, dict) and item.get("id") == query.run_id
                ),
                None,
            )
            if found is None:
                raise MishkanError(ErrorCode.MISSION, "requested run does not exist")
            return dict(found)
        command = self._validate(ApplicationCommand, arguments)
        if command.actor_id != principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness command actor differs from its authenticated principal",
            )
        return await self._request_object(
            "POST",
            "/v1/commands",
            json_body=command.model_dump(mode="json"),
        )

    async def read_resource(self, uri: str, *, principal_id: str) -> dict[str, Any]:
        if uri not in self.resources:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "MCP resource is not exposed")
        if uri == "mishkan://snapshot":
            return await self.invoke("system.snapshot", {}, principal_id=principal_id)
        if uri == "mishkan://runs":
            runs = await self._request("GET", "/v1/runs", params={"offset": 0, "limit": 100})
            return {"runs": runs}
        return await self.invoke("events.list", {"limit": 100}, principal_id=principal_id)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        token = self._token_file.read().token
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise MishkanError(
                ErrorCode.MCP,
                "STDIO bridge could not reach mishkand",
                details={"reason": type(exc).__name__},
                retryable=True,
            ) from exc
        if response.is_error:
            self._raise_daemon_error(response)
        return response.json()

    async def _request_object(
        self,
        method: str,
        path: str,
        *,
        params: Any = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self._request(method, path, params=params, json_body=json_body)
        if not isinstance(result, dict):
            raise MishkanError(ErrorCode.MCP, "daemon returned an invalid object response")
        return dict(result)

    @staticmethod
    def _raise_daemon_error(response: httpx.Response) -> None:
        try:
            payload = response.json()
            code = ErrorCode(payload["code"])
            message = str(payload["message"])
            details = dict(payload.get("details", {}))
            retryable = bool(payload.get("retryable", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise MishkanError(
                ErrorCode.MCP,
                "daemon returned a non-contractual error to the STDIO bridge",
                details={"status_code": response.status_code},
            ) from exc
        raise MishkanError(code, message, details=details, retryable=retryable)

    def _require_operation(self, operation: str) -> None:
        if operation not in self.operations:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness operation is outside the active MCP exposure profile",
            )

    @staticmethod
    def _require_empty(arguments: dict[str, Any]) -> None:
        if arguments:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "operation accepts no arguments")

    @staticmethod
    def _validate(model: type[BaseModel], arguments: dict[str, Any]) -> Any:
        try:
            return model.model_validate(arguments)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "harness operation arguments are invalid",
            ) from exc
