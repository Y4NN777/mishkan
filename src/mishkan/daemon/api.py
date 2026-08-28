"""Authenticated HTTP/OpenAPI and resumable SSE facade for mishkand."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from mishkan.application import (
    ApplicationCommand,
    CommandResult,
    RunInitializationRequest,
    SnapshotEnvelope,
)
from mishkan.application.authorization import (
    ApplicationCommandAuthority,
    AuthorizedApplicationCommand,
)
from mishkan.application.initialize import MishkanInitializer
from mishkan.artifacts import (
    ArtifactCollection,
    ArtifactManifest,
    ArtifactPin,
    ArtifactProvenance,
    UploadSession,
    WorkingReference,
)
from mishkan.artifacts import (
    ArtifactHold as ArtifactEvidenceHold,
)
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import CredentialReference, McpConfig, MishkanConfig
from mishkan.crewai.credentials import CredentialPoolResolver
from mishkan.daemon.auth import TokenFile, TokenRecord
from mishkan.daemon.bootstrap import DaemonPaths
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.edits import ChangeSet, ChangeSetResult, ChangeSetService
from mishkan.edits.git import GovernedGitService
from mishkan.events import (
    EventHold as EventEvidenceHold,
)
from mishkan.events import (
    EventHoldScope,
    EventPage,
    EventRetentionPlan,
    EventRetentionPolicy,
)
from mishkan.execution import CursorRead, ExecutionRequest, ExecutionSession, SessionSupervisor
from mishkan.mcp import (
    McpContractFactory,
    McpFacadeRouter,
    McpHttpFacade,
    McpPrimitiveKind,
    McpRepository,
    McpSdkClient,
    McpService,
    McpServiceRunner,
)
from mishkan.persistence import LocalRunRepository, SchemaManager, SQLiteApplicationRepository
from mishkan.policy import Decision
from mishkan.policy.models import EffectivePolicy
from mishkan.runtime import TaskReviewRejection
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


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
    persistence = config.persistence
    assert persistence is not None
    repository = SQLiteApplicationRepository(
        paths.database,
        busy_timeout_ms=persistence.busy_timeout_ms,
    )
    run_repository = LocalRunRepository(
        paths.database,
        busy_timeout_ms=persistence.busy_timeout_ms,
    )
    security = HTTPBearer(auto_error=False)
    security_dependency = Depends(security)
    daemon = config.daemon
    artifact_config = config.artifacts
    inspection_source = config.inspection_profile
    assert daemon is not None
    assert artifact_config is not None
    if inspection_source is None:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "daemon artifact persistence requires an inspection profile",
        )
    content_inspector = ContentInspector(
        InspectionProfileLoader().load(inspection_source, paths.workspace)
    )
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=artifact_config.max_artifact_bytes,
        max_chunk_bytes=artifact_config.chunk_bytes,
        busy_timeout_ms=persistence.busy_timeout_ms,
        staging_ttl_seconds=artifact_config.staging_ttl_seconds,
        content_inspector=content_inspector,
    )
    changes = ChangeSetService(
        paths.database,
        paths.workspace,
        artifacts,
        busy_timeout_ms=persistence.busy_timeout_ms,
    )
    git_effects = GovernedGitService(artifacts)
    session_config = config.sessions
    assert session_config is not None
    supervisor = SessionSupervisor(
        paths.database,
        paths.workspace,
        paths.sessions,
        session_config,
        artifacts,
        busy_timeout_ms=persistence.busy_timeout_ms,
        content_inspector=content_inspector,
    )
    supervisor.reconcile_all()
    command_lock = asyncio.Lock()
    run_execution_lock = asyncio.Lock()
    active_run_commands: dict[UUID, tuple[str, asyncio.Task[CommandResult]]] = {}
    mcp_repository: McpRepository | None = None
    mcp_runner: McpServiceRunner | None = None
    mcp_config = config.mcp
    if mcp_config is not None:
        web_config = config.web
        if web_config is None:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "daemon MCP mediation requires Web and inspection configuration",
            )
        mcp_repository = McpRepository(
            paths.database,
            busy_timeout_ms=persistence.busy_timeout_ms,
        )
        mcp_service = McpService(
            paths.workspace,
            mcp_config,
            mcp_repository,
            McpSdkClient(web_config.network_profiles),
            content_inspector,
        )
        mcp_service.reconcile_after_restart()
        mcp_runner = McpServiceRunner(mcp_service)
    credential_resolver = CredentialPoolResolver()
    command_authority = ApplicationCommandAuthority(
        config, paths.workspace, changes, supervisor, mcp_runner
    )

    async def execute_command(command: ApplicationCommand, principal_id: str) -> CommandResult:
        if command.actor_id != principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "command actor does not match the authenticated client identity",
                details={"actor_id": command.actor_id},
            )
        replayed = repository.replay(command)
        if replayed is not None:
            return replayed
        authorized = command_authority.authorize(command)
        if authorized.decision.decision is not Decision.ALLOW:
            code = (
                ErrorCode.AUTHORIZATION_MISSING
                if authorized.decision.decision is Decision.REQUIRE_APPROVAL
                else ErrorCode.AUTHORITY_NOT_GRANTED
            )
            refusal = MishkanError(
                code,
                "public policy did not authorize the exact application command scope",
                details={
                    "request_fingerprint": authorized.request.fingerprint,
                    "policy_fingerprint": authorized.decision.policy_fingerprint,
                    "policy_revisions": list(authorized.decision.policy_revisions),
                    "matched_rule_ids": list(authorized.decision.matched_rule_ids),
                    "decision": authorized.decision.decision.value,
                },
            )
            return repository.refuse(
                authorized.command,
                target_id=authorized.command.target_id or "local-instance",
                error=refusal,
                event_payload={
                    "command_type": authorized.command.command_type,
                    **_authorization_projection(authorized),
                    "error_code": refusal.envelope.code,
                },
            )
        command = authorized.command
        resolved_credentials = _resolve_command_credentials(
            authorized,
            credential_resolver,
            mcp_runner,
            mcp_config,
            config,
        )
        if command.command_type == "run.initialize":
            if command.target_type != "run" or command.target_id is not None:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "run.initialize targets the daemon's configured repository",
                )
            try:
                request = RunInitializationRequest.model_validate(command.payload)
            except (TypeError, ValueError) as exc:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "run.initialize payload does not match its public contract",
                ) from exc

            async with command_lock:
                active = active_run_commands.get(command.command_id)
                if active is not None:
                    fingerprint, task = active
                    if fingerprint != command.fingerprint:
                        raise MishkanError(
                            ErrorCode.DUPLICATE_RESULT,
                            "command identity was already used for different content",
                            details={"command_id": str(command.command_id)},
                        )
                else:
                    replayed = repository.reserve(command, target_id="local-instance")
                    if replayed is not None:
                        return replayed
                    accepted: list[CommandResult] = []

                    def accept_run(run_id: str) -> None:
                        accepted.append(
                            repository.complete_reserved(
                                command,
                                target_id="local-instance",
                                event_type="run.request_accepted",
                                result_payload={"run_id": run_id},
                                event_payload={
                                    "run_id": run_id,
                                    "request_schema_version": request.schema_version,
                                    **_authorization_projection(authorized),
                                },
                                source="mishkand",
                            )
                        )

                    async def execute_run() -> CommandResult:
                        async with run_execution_lock:
                            await asyncio.to_thread(
                                MishkanInitializer().run,
                                config,
                                paths.workspace,
                                request.objective,
                                on_run_started=accept_run,
                            )
                        if len(accepted) != 1:
                            raise MishkanError(
                                ErrorCode.RUN_INTERRUPTED,
                                "CrewAI run did not establish exactly one durable run identity",
                            )
                        return accepted[0]

                    task = asyncio.create_task(
                        execute_run(),
                        name=f"run.initialize:{command.command_id}",
                    )
                    active_run_commands[command.command_id] = (command.fingerprint, task)

                    def forget(completed: asyncio.Task[CommandResult]) -> None:
                        current = active_run_commands.get(command.command_id)
                        if current is not None and current[1] is completed:
                            active_run_commands.pop(command.command_id, None)
                        if not completed.cancelled():
                            completed.exception()

                    task.add_done_callback(forget)
            return await asyncio.shield(task)

        async with command_lock:
            target_id = command.target_id or "local-instance"
            replayed = repository.reserve(command, target_id=target_id)
            if replayed is not None:
                return replayed
            try:
                event_type, result_payload = _dispatch(
                    command,
                    authorized,
                    repository,
                    EventRetentionPolicy(
                        max_age_days=persistence.event_retention_days,
                        batch_size=daemon.event_page_limit,
                    ),
                    artifacts,
                    changes,
                    git_effects,
                    command_authority.policy,
                    supervisor,
                    run_repository,
                    mcp_runner,
                    mcp_config,
                    resolved_credentials,
                )
            except MishkanError as error:
                return repository.fail_reserved(
                    command,
                    target_id=target_id,
                    error=error,
                    event_payload=_authorization_projection(authorized),
                    sensitivity=(
                        "security"
                        if error.envelope.code
                        in {
                            ErrorCode.AUTHORITY_NOT_GRANTED,
                            ErrorCode.AUTHORIZATION_MISSING,
                            ErrorCode.POLICY_CONFLICT,
                            ErrorCode.SECRET_CONTENT,
                        }
                        else "internal"
                    ),
                )
            except (KeyError, TypeError, ValueError):
                payload_error = MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "application command payload does not match its registered contract",
                    details={"command_type": command.command_type},
                )
                return repository.fail_reserved(
                    command,
                    target_id=target_id,
                    error=payload_error,
                    event_payload=_authorization_projection(authorized),
                )
            return repository.complete_reserved(
                command,
                target_id=target_id,
                event_type=event_type,
                result_payload=result_payload,
                event_payload=_event_projection(command, result_payload, authorized),
                source="mishkand",
            )

    mcp_http: McpHttpFacade | None = None
    if mcp_config is not None and mcp_config.facade.enabled:
        schema_revision = SchemaManager(paths.database).status().head_revision
        router = McpFacadeRouter(
            mcp_config,
            repository,
            execute_command,
            schema_revision=schema_revision,
            event_page_limit=daemon.event_page_limit,
        )
        mcp_http = McpHttpFacade(
            router,
            token_file,
            daemon_host=daemon.host,
            daemon_port=daemon.port,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if mcp_http is None:
                yield
                return
            async with mcp_http.lifespan():
                yield
        finally:
            if mcp_runner is not None:
                mcp_runner.close()

    app = FastAPI(
        title="MISHKAN application API",
        version="1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
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
        return await execute_command(command, principal.principal_id)

    @app.get("/v1/snapshot")
    async def snapshot(
        _principal: TokenRecord = authenticated,
    ) -> SnapshotEnvelope:
        return repository.snapshot(limit=daemon.event_page_limit)

    @app.get("/v1/events")
    async def events(
        _principal: TokenRecord = authenticated,
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=1_000)] = None,
        event_type: Annotated[list[str] | None, Query()] = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        identity_id: str | None = None,
        team_id: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        security_relevant: bool | None = None,
    ) -> EventPage:
        return repository.events(
            after_cursor=after,
            limit=limit or daemon.event_page_limit,
            event_types=tuple(event_type or ()),
            entity_type=entity_type,
            entity_id=entity_id,
            run_id=run_id,
            task_id=task_id,
            identity_id=identity_id,
            team_id=team_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            security_relevant=security_relevant,
        )

    @app.get("/v1/events/holds")
    async def event_holds(
        _principal: TokenRecord = authenticated,
        active_only: bool = False,
    ) -> tuple[EventEvidenceHold, ...]:
        return repository.event_holds(active_only=active_only)

    @app.get("/v1/events/retention-policy")
    async def event_retention_policy_query(
        _principal: TokenRecord = authenticated,
    ) -> EventRetentionPolicy:
        return EventRetentionPolicy(
            max_age_days=persistence.event_retention_days,
            batch_size=daemon.event_page_limit,
        )

    @app.get("/v1/events/retention-plans")
    async def event_retention_plans(
        _principal: TokenRecord = authenticated,
    ) -> tuple[EventRetentionPlan, ...]:
        return repository.event_retention_plans()

    @app.get("/v1/events/stream")
    async def event_stream(
        request: Request,
        _principal: TokenRecord = authenticated,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
        event_type: Annotated[list[str] | None, Query()] = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        identity_id: str | None = None,
        team_id: str | None = None,
        occurred_after: datetime | None = None,
        occurred_before: datetime | None = None,
        security_relevant: bool | None = None,
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
            event_types=tuple(event_type or ()),
            entity_type=entity_type,
            entity_id=entity_id,
            run_id=run_id,
            task_id=task_id,
            identity_id=identity_id,
            team_id=team_id,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            security_relevant=security_relevant,
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
                    event_types=tuple(event_type or ()),
                    entity_type=entity_type,
                    entity_id=entity_id,
                    run_id=run_id,
                    task_id=task_id,
                    identity_id=identity_id,
                    team_id=team_id,
                    occurred_after=occurred_after,
                    occurred_before=occurred_before,
                    security_relevant=security_relevant,
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

    @app.get("/v1/artifact-uploads/{upload_id}")
    async def artifact_upload(
        upload_id: UUID,
        _principal: TokenRecord = authenticated,
    ) -> UploadSession:
        return artifacts.upload(upload_id)

    @app.get("/v1/artifact-collections")
    async def artifact_collection_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[ArtifactCollection, ...]:
        return artifacts.list_collections(offset=offset, limit=limit)

    @app.get("/v1/artifact-references")
    async def artifact_reference_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[WorkingReference, ...]:
        return artifacts.list_references(offset=offset, limit=limit)

    @app.get("/v1/artifact-holds")
    async def artifact_hold_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[ArtifactEvidenceHold, ...]:
        return artifacts.list_holds(offset=offset, limit=limit)

    @app.get("/v1/artifact-pins")
    async def artifact_pin_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[ArtifactPin, ...]:
        return artifacts.list_pins(offset=offset, limit=limit)

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
    ) -> tuple[ExecutionSession, ...]:
        return supervisor.list(offset=offset, limit=limit)

    @app.get("/v1/sessions/{session_id}")
    async def session_get(
        session_id: UUID,
        _principal: TokenRecord = authenticated,
    ) -> ExecutionSession:
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

    @app.get("/v1/runs")
    async def run_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[dict[str, object], ...]:
        return repository.runs(offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/tasks")
    async def task_list(
        run_id: str,
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[dict[str, object], ...]:
        return repository.tasks(run_id, offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/review-rejections")
    async def review_rejection_list(
        run_id: str,
        _principal: TokenRecord = authenticated,
    ) -> tuple[TaskReviewRejection, ...]:
        return run_repository.rejected_reviews(run_id)

    @app.get("/v1/mcp/connections")
    async def mcp_connection_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[dict[str, object], ...]:
        if mcp_repository is None:
            return ()
        return tuple(
            item.model_dump(mode="json")
            for item in mcp_repository.list_connections(offset=offset, limit=limit)
        )

    @app.get("/v1/mcp/connections/{connection_id}/primitives")
    async def mcp_primitive_list(
        connection_id: str,
        _principal: TokenRecord = authenticated,
    ) -> tuple[dict[str, object], ...]:
        if mcp_repository is None:
            return ()
        return tuple(
            item.model_dump(mode="json") for item in mcp_repository.list_primitives(connection_id)
        )

    @app.get("/v1/mcp/connections/{connection_id}/contracts")
    async def mcp_contract_list(
        connection_id: str,
        _principal: TokenRecord = authenticated,
    ) -> tuple[dict[str, object], ...]:
        if mcp_repository is None or mcp_config is None:
            return ()
        factory = McpContractFactory(mcp_config)
        return tuple(
            factory.build(connection_id, item).model_dump(mode="json")
            for item in mcp_repository.list_primitives(connection_id)
            if item.kind is McpPrimitiveKind.TOOL
        )

    @app.get("/v1/mcp/calls")
    async def mcp_call_list(
        _principal: TokenRecord = authenticated,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = 100,
    ) -> tuple[dict[str, object], ...]:
        if mcp_repository is None:
            return ()
        return mcp_repository.list_calls(offset=offset, limit=limit)

    @app.get("/v1/mcp/calls/{request_id}/progress")
    async def mcp_progress_list(
        request_id: UUID,
        _principal: TokenRecord = authenticated,
        cursor: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[dict[str, object], ...]:
        if mcp_repository is None:
            return ()
        return tuple(
            item.model_dump(mode="json")
            for item in mcp_repository.progress_after(request_id, cursor)
        )

    if mcp_http is not None:
        assert mcp_config is not None
        app.mount(mcp_config.facade.streamable_http_path, mcp_http.app)

    return app


def _event_projection(
    command: ApplicationCommand,
    result_payload: dict[str, object],
    authorized: AuthorizedApplicationCommand,
) -> dict[str, object]:
    """Describe a command settlement without copying effect inputs or result bodies."""

    return {
        "command_type": command.command_type,
        "request_schema_version": command.schema_version,
        "payload_fields": sorted(command.payload),
        "result_fields": sorted(result_payload),
        **_authorization_projection(authorized),
    }


def _authorization_projection(
    authorized: AuthorizedApplicationCommand,
) -> dict[str, object]:
    return {
        "authorization_request_fingerprint": authorized.request.fingerprint,
        "policy_fingerprint": authorized.decision.policy_fingerprint,
        "policy_revisions": list(authorized.decision.policy_revisions),
        "matched_rule_ids": list(authorized.decision.matched_rule_ids),
        "authorization_decision": authorized.decision.decision.value,
    }


def _dispatch(
    command: ApplicationCommand,
    authorized: AuthorizedApplicationCommand,
    repository: SQLiteApplicationRepository,
    event_retention_policy: EventRetentionPolicy,
    artifacts: DurableArtifactService,
    changes: ChangeSetService,
    git_effects: GovernedGitService,
    effective_policy: EffectivePolicy,
    supervisor: SessionSupervisor,
    runs: LocalRunRepository,
    mcp_runner: McpServiceRunner | None,
    mcp_config: McpConfig | None,
    resolved_credentials: dict[str, str],
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
    if command.command_type == "artifact.upload.abort" and command.target_id is not None:
        upload = artifacts.abort_upload(UUID(command.target_id))
        return "artifact.upload_aborted", upload.model_dump(mode="json")
    if command.command_type == "artifact.reference.update":
        reference = artifacts.update_reference(
            str(payload["scope"]),
            str(payload["name"]),
            str(payload["artifact_reference"]),
            expected_revision=int(payload["expected_reference_revision"]),
        )
        return "artifact.reference_updated", reference.model_dump(mode="json")
    if command.command_type == "artifact.collection.create":
        entries = payload["entries"]
        if not isinstance(entries, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in entries.items()
        ):
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "artifact collection entries must map logical paths to artifact references",
            )
        normalized_entries = {str(key): str(value) for key, value in entries.items()}
        collection = artifacts.create_collection(normalized_entries)
        return "artifact.collection_created", collection.model_dump(mode="json")
    if command.command_type == "artifact.hold.set" and command.target_id is not None:
        artifact_hold = artifacts.hold(f"artifact:{command.target_id}", str(payload["reason"]))
        return "artifact.hold_set", artifact_hold.model_dump(mode="json")
    if command.command_type == "artifact.hold.release" and command.target_id is not None:
        released_artifact_hold = artifacts.release_hold(f"artifact:{command.target_id}")
        return "artifact.hold_released", released_artifact_hold.model_dump(mode="json")
    if command.command_type == "artifact.pin.set" and command.target_id is not None:
        pin = artifacts.pin(f"artifact:{command.target_id}")
        return "artifact.pin_set", pin.model_dump(mode="json")
    if command.command_type == "artifact.pin.release" and command.target_id is not None:
        pin = artifacts.release_pin(f"artifact:{command.target_id}")
        return "artifact.pin_released", pin.model_dump(mode="json")
    if command.command_type == "artifact.gc.plan":
        plan = artifacts.plan_gc(watermark=datetime.fromisoformat(str(payload["watermark"])))
        return "artifact.gc_planned", plan.model_dump(mode="json")
    if command.command_type == "artifact.gc.apply" and command.target_id is not None:
        plan = artifacts.apply_gc(UUID(command.target_id))
        return "artifact.gc_applied", plan.model_dump(mode="json")
    if command.command_type == "artifact.reconcile.plan":
        reconciliation = artifacts.plan_reconciliation()
        return "artifact.reconciliation_planned", reconciliation.model_dump(mode="json")
    if command.command_type == "artifact.reconcile.apply" and command.target_id is not None:
        reconciliation = artifacts.apply_reconciliation(UUID(command.target_id))
        return "artifact.reconciliation_applied", reconciliation.model_dump(mode="json")
    if command.command_type == "event.hold.create":
        event_hold = repository.create_event_hold(
            scope=EventHoldScope(str(payload["scope"])),
            scope_id=(str(payload["scope_id"]) if payload.get("scope_id") is not None else None),
            reason=str(payload["reason"]),
            actor_id=command.actor_id,
        )
        return "event.hold_created", event_hold.model_dump(mode="json")
    if command.command_type == "event.hold.release" and command.target_id is not None:
        released_event_hold = repository.release_event_hold(UUID(command.target_id))
        return "event.hold_released", released_event_hold.model_dump(mode="json")
    if command.command_type == "event.retention.plan":
        event_plan = repository.plan_event_retention(event_retention_policy)
        return "event.retention_planned", event_plan.model_dump(mode="json")
    if command.command_type == "event.retention.apply" and command.target_id is not None:
        applied_event_plan = repository.apply_event_retention(UUID(command.target_id))
        return "event.retention_applied", applied_event_plan.model_dump(mode="json")
    if command.command_type == "change.plan":
        change_result = changes.plan(ChangeSet.model_validate(payload["change_set"]))
        return "change_set.planned", change_result.model_dump(mode="json")
    if command.command_type == "change.apply" and command.target_id is not None:
        change_result = changes.apply(UUID(command.target_id))
        return "change_set.settled", change_result.model_dump(mode="json")
    if command.command_type == "change.reconcile" and command.target_id is not None:
        change_result = changes.reconcile(UUID(command.target_id))
        return "change_set.reconciled", change_result.model_dump(mode="json")
    if command.command_type.startswith("git."):
        git_request = authorized.git_request
        if git_request is None:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "authorized Git request is absent")
        git_result = git_effects.execute(
            git_request,
            authorization=authorized.request,
            policy=effective_policy,
            credential_value=(
                resolved_credentials.get(git_request.credential_reference)
                if git_request.credential_reference is not None
                else None
            ),
        )
        return f"git.{git_request.mode.value}_settled", git_result.model_dump(mode="json")
    if command.command_type == "session.start":
        session_request = authorized.session_request
        if session_request is None:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "authorized session request is absent")
        effective_request = session_request.model_copy(
            update={"policy_fingerprint": authorized.decision.policy_fingerprint}
        )
        record = supervisor.start(effective_request, credential_values=resolved_credentials)
        return "session.started", record.model_dump(mode="json")
    if command.command_type == "session.write" and command.target_id is not None:
        content = base64.b64decode(str(payload["content_base64"]), validate=True)
        written = supervisor.write(
            UUID(command.target_id),
            content,
            declared_effects=tuple(str(value) for value in payload["declared_effects"]),
            network_destinations=tuple(str(value) for value in payload["network_destinations"]),
        )
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
    if command.command_type == "run.cancel" and command.target_id is not None:
        snapshot = runs.cancel_run(command.target_id)
        return "run.cancellation_requested", {"run_id": snapshot.run_id}
    if command.command_type == "run.recover" and command.target_id is not None:
        effects = tuple(str(value) for value in payload.get("uncertain_effects", []))
        released = runs.recover_interrupted(command.target_id, uncertain_effects=effects)
        return "run.recovered", {"run_id": command.target_id, "released_tasks": released}
    if command.command_type == "mcp.connection.connect" and command.target_id is not None:
        if command.payload:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "MCP connection command accepts no credential values or payload",
            )
        if mcp_runner is None or mcp_config is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        configured = mcp_config.connections.get(command.target_id)
        if configured is None or not configured.enabled:
            raise MishkanError(ErrorCode.MCP, "MCP connection is not enabled")
        mcp_record = mcp_runner.connect(
            command.target_id,
            principal=command.actor_id,
            policy_fingerprint=authorized.decision.policy_fingerprint,
            credentials=resolved_credentials,
        )
        return "mcp.connection_ready", mcp_record.model_dump(mode="json")
    if command.command_type == "mcp.call.cancel" and command.target_id is not None:
        if command.payload:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "MCP cancellation accepts no payload")
        if mcp_runner is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        request_id = UUID(command.target_id)
        if mcp_runner.cancel(request_id):
            return "mcp.call_cancellation_requested", {"request_id": command.target_id}
        if mcp_config is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        mcp_result = mcp_runner.cancel_remote_task(request_id, credentials=resolved_credentials)
        return "mcp.call_cancelled", mcp_result.model_dump(mode="json")
    if command.command_type == "mcp.call.reconcile" and command.target_id is not None:
        if command.payload:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "MCP reconciliation accepts no payload")
        if mcp_runner is None or mcp_config is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        request_id = UUID(command.target_id)
        mcp_result = mcp_runner.resume_remote_task(request_id, credentials=resolved_credentials)
        return "mcp.call_reconciled", mcp_result.model_dump(mode="json")
    raise MishkanError(
        ErrorCode.OUTPUT_CONTRACT,
        "application command type has no registered handler",
        details={"command_type": command.command_type},
    )


def _session_credential_references(request: ExecutionRequest) -> tuple[CredentialReference, ...]:
    references = (
        tuple(
            value
            for value in request.credential_environment.values()
            if isinstance(value, CredentialReference)
        )
        + request.credential_references
    )
    by_locator: dict[str, CredentialReference] = {}
    for reference in references:
        current = by_locator.get(reference.locator)
        if current is not None and current != reference:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "session credential locator maps to conflicting credential sources",
            )
        by_locator[reference.locator] = reference
    return tuple(by_locator[key] for key in sorted(by_locator))


def _resolve_command_credentials(
    authorized: AuthorizedApplicationCommand,
    resolver: CredentialPoolResolver,
    mcp_runner: McpServiceRunner | None,
    mcp_config: McpConfig | None,
    config: MishkanConfig,
) -> dict[str, str]:
    command = authorized.command
    references: tuple[CredentialReference, ...] = ()
    if authorized.session_request is not None:
        references = _session_credential_references(authorized.session_request)
    elif authorized.git_request is not None:
        binding_id = authorized.git_request.credential_reference
        if binding_id is None:
            return {}
        reference = config.credential_bindings.get(binding_id)
        if reference is None:
            raise MishkanError(ErrorCode.AUTHORIZATION_MISSING, "Git credential is not configured")
        resolved = resolver.resolve_exact((reference,))
        return {binding_id: resolved[reference.locator]}
    elif command.command_type == "mcp.connection.connect" and command.target_id is not None:
        if mcp_config is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        references = mcp_config.connections[command.target_id].credential_refs
    elif command.command_type in {"mcp.call.cancel", "mcp.call.reconcile"}:
        if mcp_config is None or mcp_runner is None or command.target_id is None:
            raise MishkanError(ErrorCode.MCP, "MCP mediation is not configured")
        connection_id = mcp_runner.call_connection_id(UUID(command.target_id))
        references = mcp_config.connections[connection_id].credential_refs
    return resolver.resolve_exact(references)
