"""Authenticated HTTP/OpenAPI and resumable SSE facade for mishkand."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mishkan.application import ApplicationCommand, CommandResult, SnapshotEnvelope
from mishkan.config.models import MishkanConfig
from mishkan.daemon.auth import TokenFile, TokenRecord
from mishkan.daemon.bootstrap import DaemonPaths
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.events import EventPage
from mishkan.persistence import SchemaManager, SQLiteApplicationRepository


def _http_status(error: MishkanError) -> int:
    code = error.envelope.code
    if code in {ErrorCode.AUTHORITY_NOT_GRANTED, ErrorCode.AUTHORIZATION_MISSING}:
        return 403
    if code in {ErrorCode.REVISION_MISMATCH, ErrorCode.DUPLICATE_RESULT}:
        return 409
    if code in {ErrorCode.VERSION, ErrorCode.REQUIRED_DEPENDENCY}:
        return 503
    if code in {ErrorCode.OUTPUT_CONTRACT, ErrorCode.CONFIGURATION}:
        return 422
    return 400


def create_app(config: MishkanConfig) -> FastAPI:
    paths = DaemonPaths.from_config(config)
    SchemaManager(paths.database).require_current()
    token_file = TokenFile(paths.token_file)
    token_file.read()
    repository = SQLiteApplicationRepository(paths.database)
    security = HTTPBearer(auto_error=False)
    security_dependency = Depends(security)
    daemon = config.daemon
    assert daemon is not None

    app = FastAPI(
        title="MISHKAN application API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.exception_handler(MishkanError)
    async def mishkan_error_handler(_request: Request, error: MishkanError) -> JSONResponse:
        return JSONResponse(
            status_code=_http_status(error),
            content=error.envelope.model_dump(mode="json"),
        )

    async def authenticate(
        credentials: HTTPAuthorizationCredentials | None = security_dependency,
    ) -> TokenRecord:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "authenticated daemon client identity is required",
            )
        record = token_file.authenticate(credentials.credentials)
        if record is None:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "daemon bearer credential is invalid",
            )
        return record

    authenticated = Depends(authenticate)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        status = SchemaManager(paths.database).status()
        return {"status": "ready", "schema": status.head_revision}

    @app.post("/v1/commands", response_model=CommandResult)
    async def command(
        command: ApplicationCommand,
        principal: TokenRecord = authenticated,
    ) -> CommandResult:
        if command.actor_id != principal.principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "command actor does not match the authenticated client identity",
                details={"actor_id": command.actor_id},
            )
        if command.command_type != "system.checkpoint" or command.target_type != "system":
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "application command type has no registered I03 handler",
                details={"command_type": command.command_type},
            )
        target_id = command.target_id or "local-instance"
        return repository.accept(
            command,
            target_id=target_id,
            event_type="system.checkpoint_recorded",
            result_payload={"recorded": True},
            event_payload=command.payload,
            source="mishkand",
        )

    @app.get("/v1/snapshot")
    async def snapshot(
        _principal: TokenRecord = authenticated,
    ) -> SnapshotEnvelope:
        return repository.snapshot()

    @app.get("/v1/events")
    async def events(
        _principal: TokenRecord = authenticated,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=1_000)] = None,
        event_type: Annotated[list[str] | None, Query()] = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> EventPage:
        return repository.events(
            after_cursor=after,
            limit=limit or daemon.event_page_limit,
            event_types=tuple(event_type or ()),
            entity_type=entity_type,
            entity_id=entity_id,
        )

    @app.get("/v1/events/stream")
    async def event_stream(
        request: Request,
        _principal: TokenRecord = authenticated,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        cursor = after
        if last_event_id is not None:
            try:
                cursor = max(cursor, int(last_event_id))
            except ValueError as exc:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "Last-Event-ID must contain an integer event cursor",
                ) from exc
        initial = repository.events(
            after_cursor=cursor,
            limit=daemon.event_page_limit,
        )

        async def stream() -> AsyncIterator[str]:
            current = cursor
            page = initial
            heartbeat_elapsed = 0.0
            while True:
                if await request.is_disconnected():
                    return
                if page.events:
                    for item in page.events:
                        data = json.dumps(
                            item.model_dump(mode="json"),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        yield f"id: {item.cursor}\nevent: {item.event_type}\ndata: {data}\n\n"
                    current = page.next_cursor
                    heartbeat_elapsed = 0.0
                await asyncio.sleep(daemon.event_poll_seconds)
                heartbeat_elapsed += daemon.event_poll_seconds
                if heartbeat_elapsed >= daemon.heartbeat_seconds:
                    yield f": heartbeat {current}\n\n"
                    heartbeat_elapsed = 0.0
                page = repository.events(
                    after_cursor=current,
                    limit=daemon.event_page_limit,
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app
