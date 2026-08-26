"""Authenticated HTTP/OpenAPI and resumable SSE facade for mishkand."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mishkan.application import ApplicationCommand, CommandResult, SnapshotEnvelope
from mishkan.artifacts import ArtifactManifest, ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import MishkanConfig
from mishkan.daemon.auth import TokenFile, TokenRecord
from mishkan.daemon.bootstrap import DaemonPaths
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.edits import ChangeSet, ChangeSetResult, ChangeSetService
from mishkan.events import EventPage
from mishkan.execution import CursorRead, SessionRecord, SessionRequest, SessionSupervisor
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
    artifact_config = config.artifacts
    assert daemon is not None
    assert artifact_config is not None
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=artifact_config.max_artifact_bytes,
        max_chunk_bytes=artifact_config.chunk_bytes,
    )
    changes = ChangeSetService(paths.database, paths.workspace, artifacts)
    session_config = config.sessions
    assert session_config is not None
    supervisor = SessionSupervisor(
        paths.database,
        paths.workspace,
        paths.sessions,
        session_config,
        artifacts,
    )
    supervisor.reconcile_all()
    command_lock = asyncio.Lock()

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
        async with command_lock:
            replayed = repository.replay(command)
            if replayed is not None:
                return replayed
            target_id = command.target_id or "local-instance"
            repository.require_expected_revision(command, target_id)
            try:
                event_type, result_payload = _dispatch(command, artifacts, changes, supervisor)
            except MishkanError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "application command payload does not match its registered contract",
                    details={"command_type": command.command_type},
                ) from exc
            return repository.accept(
                command,
                target_id=target_id,
                event_type=event_type,
                result_payload=result_payload,
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

    @app.get("/v1/artifacts")
    async def artifact_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[ArtifactManifest, ...]:
        return artifacts.list_manifests(offset=offset, limit=limit)

    @app.get("/v1/artifacts/{artifact_id}")
    async def artifact_manifest(
        artifact_id: str,
        _principal: TokenRecord = authenticated,
    ) -> ArtifactManifest:
        return artifacts.manifest(f"artifact:{artifact_id}")

    @app.get("/v1/artifacts/{artifact_id}/content")
    async def artifact_content(
        artifact_id: str,
        _principal: TokenRecord = authenticated,
    ) -> StreamingResponse:
        manifest = artifacts.manifest(f"artifact:{artifact_id}")
        path = artifacts.body_path(manifest.reference)

        async def body() -> AsyncIterator[bytes]:
            with path.open("rb") as stream:
                while chunk := stream.read(artifact_config.chunk_bytes):
                    yield chunk

        return StreamingResponse(
            body(),
            media_type=manifest.detected_media_type or manifest.declared_media_type,
            headers={"Content-Length": str(manifest.size_bytes)},
        )

    @app.get("/v1/change-sets")
    async def change_set_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[ChangeSetResult, ...]:
        return changes.list(offset=offset, limit=limit)

    @app.get("/v1/change-sets/{change_set_id}")
    async def change_set_get(
        change_set_id: UUID,
        _principal: TokenRecord = authenticated,
    ) -> ChangeSetResult:
        return changes.get(change_set_id)

    @app.get("/v1/sessions")
    async def session_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[SessionRecord, ...]:
        return supervisor.list(offset=offset, limit=limit)

    @app.get("/v1/sessions/{session_id}")
    async def session_get(
        session_id: UUID,
        _principal: TokenRecord = authenticated,
    ) -> SessionRecord:
        return supervisor.status(session_id)

    @app.get("/v1/sessions/{session_id}/output")
    async def session_output(
        session_id: UUID,
        _principal: TokenRecord = authenticated,
        channel: Annotated[str, Query(pattern=r"^(stdout|stderr)$")] = "stdout",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=16_777_216)] = 65_536,
        binary: bool = False,
    ) -> CursorRead:
        selected: Literal["stdout", "stderr"] = "stdout" if channel == "stdout" else "stderr"
        return supervisor.read(
            session_id,
            channel=selected,
            offset=offset,
            limit=limit,
            binary=binary,
        )

    return app


def _dispatch(
    command: ApplicationCommand,
    artifacts: DurableArtifactService,
    changes: ChangeSetService,
    supervisor: SessionSupervisor,
) -> tuple[str, dict[str, object]]:
    payload = command.payload
    if command.command_type == "system.checkpoint" and command.target_type == "system":
        return "system.checkpoint_recorded", {"recorded": True}
    if command.command_type == "artifact.upload.open":
        upload = artifacts.open_upload(
            expected_size=int(payload["expected_size"]),
            expected_digest=str(payload["expected_digest"]),
            media_type=str(payload["media_type"]),
            provenance=ArtifactProvenance.model_validate(payload["provenance"]),
            sensitivity=str(payload.get("sensitivity", "internal")),
            retention=str(payload.get("retention", "run")),
        )
        return "artifact.upload_opened", upload.model_dump(mode="json")
    if command.command_type == "artifact.upload.chunk" and command.target_id is not None:
        try:
            content = base64.b64decode(str(payload["content_base64"]), validate=True)
        except (ValueError, TypeError) as exc:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT, "artifact chunk is not valid base64"
            ) from exc
        upload = artifacts.append_chunk(
            UUID(command.target_id), offset=int(payload["offset"]), content=content
        )
        return "artifact.chunk_appended", upload.model_dump(mode="json")
    if command.command_type == "artifact.upload.commit" and command.target_id is not None:
        manifest = artifacts.commit_upload(UUID(command.target_id))
        return "artifact.available", manifest.model_dump(mode="json")
    if command.command_type == "artifact.reference.update":
        reference = artifacts.update_reference(
            str(payload["scope"]),
            str(payload["name"]),
            str(payload["artifact_reference"]),
            expected_revision=int(payload["expected_reference_revision"]),
        )
        return "artifact.reference_updated", reference.model_dump(mode="json")
    if command.command_type == "artifact.gc.plan":
        plan = artifacts.plan_gc(watermark=datetime.fromisoformat(str(payload["watermark"])))
        return "artifact.gc_planned", plan.model_dump(mode="json")
    if command.command_type == "artifact.gc.apply" and command.target_id is not None:
        plan = artifacts.apply_gc(UUID(command.target_id))
        return "artifact.gc_applied", plan.model_dump(mode="json")
    if command.command_type == "change.plan":
        result = changes.plan(ChangeSet.model_validate(payload["change_set"]))
        return "change_set.planned", result.model_dump(mode="json")
    if command.command_type == "change.apply" and command.target_id is not None:
        result = changes.apply(UUID(command.target_id))
        return "change_set.settled", result.model_dump(mode="json")
    if command.command_type == "change.reconcile" and command.target_id is not None:
        result = changes.reconcile(UUID(command.target_id))
        return "change_set.reconciled", result.model_dump(mode="json")
    if command.command_type == "session.start":
        record = supervisor.start(SessionRequest.model_validate(payload["request"]))
        return "session.started", record.model_dump(mode="json")
    if command.command_type == "session.write" and command.target_id is not None:
        content = base64.b64decode(str(payload["content_base64"]), validate=True)
        written = supervisor.write(UUID(command.target_id), content)
        return "session.input_written", {"written": written}
    if command.command_type == "session.resize" and command.target_id is not None:
        supervisor.resize(
            UUID(command.target_id), rows=int(payload["rows"]), columns=int(payload["columns"])
        )
        return "session.resized", {"rows": int(payload["rows"]), "columns": int(payload["columns"])}
    if command.command_type == "session.signal" and command.target_id is not None:
        record = supervisor.signal(UUID(command.target_id), str(payload["signal"]))
        return "session.signalled", record.model_dump(mode="json")
    if command.command_type == "session.cancel" and command.target_id is not None:
        record = supervisor.cancel(UUID(command.target_id))
        return "session.cancelled", record.model_dump(mode="json")
    if command.command_type == "session.settle" and command.target_id is not None:
        record = supervisor.settle(UUID(command.target_id))
        return "session.settled", record.model_dump(mode="json")
    raise MishkanError(
        ErrorCode.OUTPUT_CONTRACT,
        "application command type has no registered I03 handler",
        details={"command_type": command.command_type},
    )
