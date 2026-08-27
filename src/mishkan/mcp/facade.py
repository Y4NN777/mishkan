"""Inbound harness facade over the same daemon command and query authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mishkan.application import ApplicationCommand, CommandResult
from mishkan.config.models import McpConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.events import EventPage
from mishkan.persistence import SQLiteApplicationRepository

CommandExecutor = Callable[[ApplicationCommand, str], Awaitable[CommandResult]]


class McpFacadePort(Protocol):
    operations: tuple[str, ...]
    resources: tuple[str, ...]

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]: ...

    async def read_resource(self, uri: str, *, principal_id: str) -> dict[str, Any]: ...


class FacadeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventQuery(FacadeModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000)
    event_types: tuple[str, ...] = ()
    entity_type: str | None = None
    entity_id: str | None = None


class RunQuery(FacadeModel):
    run_id: str = Field(min_length=1)


class McpFacadeRouter:
    """Expose only allowlisted operations that have an executable daemon handler."""

    _QUERY_OPERATIONS = frozenset({"system.health", "system.snapshot", "events.list", "run.get"})

    def __init__(
        self,
        config: McpConfig,
        repository: SQLiteApplicationRepository,
        command_executor: CommandExecutor,
        *,
        schema_revision: str,
        event_page_limit: int,
    ) -> None:
        profile = config.exposure_profiles[config.facade.exposure_profile]
        supported = self._QUERY_OPERATIONS | {"command.submit"}
        self.operations = tuple(item for item in profile.operations if item in supported)
        self.resources = tuple(
            item
            for item in profile.resources
            if item in {"mishkan://snapshot", "mishkan://runs", "mishkan://events"}
        )
        self._repository = repository
        self._execute = command_executor
        self._schema_revision = schema_revision
        self._event_page_limit = event_page_limit

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]:
        if operation not in self.operations:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness operation is outside the active MCP exposure profile",
            )
        if operation == "system.health":
            self._require_empty(arguments)
            return {"status": "ready", "schema": self._schema_revision}
        if operation == "system.snapshot":
            self._require_empty(arguments)
            return self._repository.snapshot(limit=self._event_page_limit).model_dump(mode="json")
        if operation == "events.list":
            query = self._validate(EventQuery, arguments)
            return self._events(query).model_dump(mode="json")
        if operation == "run.get":
            query = self._validate(RunQuery, arguments)
            runs = self._repository.runs(offset=0, limit=1_000)
            found = next((item for item in runs if item.get("id") == query.run_id), None)
            if found is None:
                raise MishkanError(ErrorCode.MISSION, "requested run does not exist")
            return dict(found)
        command = self._validate(ApplicationCommand, arguments)
        if command.actor_id != principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness command actor differs from its authenticated principal",
            )
        return (await self._execute(command, principal_id)).model_dump(mode="json")

    async def read_resource(self, uri: str, *, principal_id: str) -> dict[str, Any]:
        del principal_id
        if uri not in self.resources:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "MCP resource is not exposed")
        if uri == "mishkan://snapshot":
            return self._repository.snapshot(limit=self._event_page_limit).model_dump(mode="json")
        if uri == "mishkan://runs":
            return {"runs": list(self._repository.runs(offset=0, limit=self._event_page_limit))}
        return self._events(EventQuery(limit=self._event_page_limit)).model_dump(mode="json")

    def _events(self, query: EventQuery) -> EventPage:
        return self._repository.events(
            after_cursor=query.after,
            limit=query.limit,
            event_types=query.event_types,
            entity_type=query.entity_type,
            entity_id=query.entity_id,
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
