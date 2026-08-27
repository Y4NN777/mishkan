"""Thin CLI over I00 configuration application functions."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml

from mishkan.config.editor import set_value
from mishkan.config.loader import ConfigLoader, EffectiveConfig
from mishkan.config.presets import PRESET_NAMES, write_preset
from mishkan.config.probe import probe_connections
from mishkan.domain.errors import MishkanError
from mishkan.domain.export import export_schemas

app = typer.Typer(help="MISHKAN engineering control plane.", no_args_is_help=True)
config_app = typer.Typer(help="Create, inspect, edit, and validate effective configuration.")
schema_app = typer.Typer(help="Export versioned public schemas.")
daemon_app = typer.Typer(help="Bootstrap and administer the local mishkand instance.")
daemon_token_app = typer.Typer(help="Administer the local daemon bearer credential.")
database_app = typer.Typer(help="Inspect and explicitly migrate authoritative metadata.")
events_app = typer.Typer(help="Query and export the durable event stream.")
artifact_app = typer.Typer(help="Inspect and reconcile immutable artifacts.")
change_app = typer.Typer(help="Plan, apply, and inspect recoverable change sets.")
terminal_app = typer.Typer(help="Open and control daemon-owned PTY sessions.")
job_app = typer.Typer(help="Start and control daemon-owned managed jobs.")
run_app = typer.Typer(help="Inspect, cancel, and recover durable runs.")
mcp_app = typer.Typer(help="Connect and inspect governed MCP peers through mishkand.")
app.add_typer(config_app, name="config")
app.add_typer(schema_app, name="schema")
app.add_typer(daemon_app, name="daemon")
daemon_app.add_typer(daemon_token_app, name="token")
app.add_typer(database_app, name="db")
app.add_typer(events_app, name="events")
app.add_typer(artifact_app, name="artifact")
app.add_typer(change_app, name="change")
app.add_typer(terminal_app, name="terminal")
app.add_typer(job_app, name="job")
app.add_typer(run_app, name="run")
app.add_typer(mcp_app, name="mcp")


@dataclass(frozen=True, slots=True)
class CliState:
    sources: tuple[Path, ...]
    json_output: bool


def _environment_sources() -> tuple[Path, ...]:
    encoded = os.environ.get("MISHKAN_CONFIG", "")
    return tuple(Path(item) for item in encoded.split(os.pathsep) if item)


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        list[Path] | None,
        typer.Option("--config", "-c", help="YAML layer; repeat from low to high precedence."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Configure shared options for every command."""

    ctx.obj = CliState(sources=tuple(config or _environment_sources()), json_output=json_output)


def _state(ctx: typer.Context) -> CliState:
    state = ctx.find_root().obj
    if not isinstance(state, CliState):
        return CliState(sources=(), json_output=False)
    return state


def _emit(value: Any, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(yaml.safe_dump(value, sort_keys=False, allow_unicode=True).rstrip())


def _emit_error(error: MishkanError, *, as_json: bool) -> None:
    payload = error.envelope.model_dump(mode="json")
    if as_json:
        _emit(payload, as_json=True)
    else:
        typer.echo(f"{payload['code']}: {payload['message']}", err=True)
        if payload["details"]:
            typer.echo(yaml.safe_dump(payload["details"], sort_keys=False).rstrip(), err=True)


def _load_or_exit(ctx: typer.Context) -> EffectiveConfig:
    state = _state(ctx)
    try:
        return ConfigLoader().load(state.sources)
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error


@config_app.command("validate")
def validate_config(ctx: typer.Context) -> None:
    """Validate and fingerprint the explicit effective configuration."""

    effective = _load_or_exit(ctx)
    _emit(
        {
            "valid": True,
            "schema_version": effective.value.schema_version,
            "fingerprint": effective.fingerprint,
            "layers": [layer.as_dict() for layer in effective.layers],
        },
        as_json=_state(ctx).json_output,
    )


@config_app.command("show")
def show_config(ctx: typer.Context) -> None:
    """Show the effective non-secret configuration and field provenance."""

    effective = _load_or_exit(ctx)
    _emit(effective.public_view(), as_json=_state(ctx).json_output)


@config_app.command("setup")
def setup_config(
    ctx: typer.Context,
    preset: Annotated[str, typer.Option(help=f"One of: {', '.join(PRESET_NAMES)}")] = "local",
    output: Annotated[Path, typer.Option(help="Destination YAML file.")] = Path(
        ".mishkan/config.yaml"
    ),
    force: Annotated[bool, typer.Option(help="Replace an existing destination.")] = False,
    test_connections: Annotated[
        bool,
        typer.Option(help="Probe configured endpoints; failures are warnings."),
    ] = False,
) -> None:
    """Write an inspectable local, cloud, or hybrid configuration preset."""

    state = _state(ctx)
    try:
        target = write_preset(preset, output, overwrite=force)
        effective = ConfigLoader().load([target])
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error

    result: dict[str, Any] = {
        "created": str(target),
        "preset": preset,
        "fingerprint": effective.fingerprint,
    }
    if test_connections:
        result["connection_probes"] = [
            probe.model_dump(mode="json") for probe in probe_connections(effective.value)
        ]
    _emit(result, as_json=state.json_output)


@config_app.command("set")
def set_config(
    ctx: typer.Context,
    path: Annotated[str, typer.Argument(help="Dot-separated configuration field.")],
    value: Annotated[str, typer.Argument(help="YAML-encoded value.")],
    source: Annotated[Path, typer.Option("--file", help="Explicit YAML source to edit.")],
) -> None:
    """Atomically update one explicit source after validating the complete result."""

    state = _state(ctx)
    try:
        target = set_value(source, path, value)
        effective = ConfigLoader().load([target])
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {"updated": str(target), "field": path, "fingerprint": effective.fingerprint},
        as_json=state.json_output,
    )


@config_app.command("migrate")
def migrate_config(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option("--file", help="Explicit schema 1.1 or 1.2 YAML source.")],
) -> None:
    """Explicitly migrate one configuration source to the current schema."""
    from mishkan.config.migration import migrate_to_latest

    state = _state(ctx)
    try:
        target = migrate_to_latest(source)
        effective = ConfigLoader().load([target])
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {"migrated": str(target), "schema_version": effective.value.schema_version},
        as_json=state.json_output,
    )


@daemon_app.command("setup")
def setup_daemon(
    ctx: typer.Context,
    principal: Annotated[str, typer.Option(help="Authenticated local operator identity.")] = (
        "local-operator"
    ),
) -> None:
    """Explicitly initialize an empty daemon database and local credential."""
    from mishkan.daemon import DaemonBootstrap
    from mishkan.daemon.auth import TokenFile

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    try:
        paths = DaemonBootstrap().setup(effective.value, principal_id=principal)
        token = TokenFile(paths.token_file).public_status()
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {"database": str(paths.database), "artifacts": str(paths.artifacts), "token": token},
        as_json=state.json_output,
    )


@daemon_token_app.command("rotate")
def rotate_daemon_token(ctx: typer.Context) -> None:
    """Atomically rotate the configured local bearer credential."""
    from mishkan.daemon.auth import TokenFile
    from mishkan.daemon.bootstrap import DaemonPaths

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    try:
        paths = DaemonPaths.from_config(effective.value)
        token_file = TokenFile(paths.token_file)
        record = token_file.rotate()
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {"rotated": True, "path": str(paths.token_file), "principal_id": record.principal_id},
        as_json=state.json_output,
    )


@database_app.command("status")
def database_status(ctx: typer.Context) -> None:
    """Report the observed schema without changing it."""
    from mishkan.daemon.bootstrap import DaemonPaths
    from mishkan.persistence import SchemaManager

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    try:
        paths = DaemonPaths.from_config(effective.value)
        observed = SchemaManager(paths.database).status()
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {
            "database": str(paths.database),
            "state": observed.state.value,
            "current_revision": observed.current_revision,
            "head_revision": observed.head_revision,
        },
        as_json=state.json_output,
    )


@database_app.command("upgrade")
def database_upgrade(ctx: typer.Context) -> None:
    """Back up and explicitly upgrade recognized metadata to the current head."""
    from mishkan.daemon.bootstrap import DaemonPaths
    from mishkan.persistence import SchemaManager

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    try:
        paths = DaemonPaths.from_config(effective.value)
        observed = SchemaManager(paths.database).upgrade()
        artifact_config = effective.value.artifacts
        assert artifact_config is not None
        from mishkan.artifacts.service import DurableArtifactService

        imported_artifacts = DurableArtifactService(
            paths.database,
            paths.artifacts,
            max_artifact_bytes=artifact_config.max_artifact_bytes,
            max_chunk_bytes=artifact_config.chunk_bytes,
        ).import_legacy_manifests()
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(
        {
            "database": str(paths.database),
            "state": observed.state.value,
            "revision": observed.current_revision,
            "backup": str(observed.backup_path) if observed.backup_path else None,
            "imported_artifacts": imported_artifacts,
        },
        as_json=state.json_output,
    )


def _daemon_client(ctx: typer.Context):  # type: ignore[no-untyped-def]
    from mishkan.client import Mishkan, daemon_url
    from mishkan.daemon.bootstrap import DaemonPaths

    effective = _load_or_exit(ctx)
    paths = DaemonPaths.from_config(effective.value)
    daemon = effective.value.daemon
    assert daemon is not None
    return Mishkan(
        daemon_url(daemon.host, daemon.port),
        token_file=paths.token_file,
        timeout_seconds=daemon.request_timeout_seconds,
    )


@events_app.command("list")
def list_events(
    ctx: typer.Context,
    after: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int | None, typer.Option(min=1, max=1_000)] = None,
    event_type: Annotated[list[str] | None, typer.Option("--type")] = None,
    run_id: Annotated[str | None, typer.Option("--run")] = None,
    task_id: Annotated[str | None, typer.Option("--task")] = None,
    identity_id: Annotated[str | None, typer.Option("--identity")] = None,
    team_id: Annotated[str | None, typer.Option("--team")] = None,
    since: Annotated[str | None, typer.Option(help="Inclusive ISO-8601 timestamp.")] = None,
    until: Annotated[str | None, typer.Option(help="Inclusive ISO-8601 timestamp.")] = None,
    security: Annotated[bool, typer.Option("--security", help="Only security events.")] = False,
) -> None:
    """Query a bounded page from the durable daemon stream."""
    state = _state(ctx)
    try:
        with _daemon_client(ctx) as client:
            page = client.events(
                after=after,
                limit=limit,
                event_types=tuple(event_type or ()),
                run_id=run_id,
                task_id=task_id,
                identity_id=identity_id,
                team_id=team_id,
                occurred_after=_event_time(since),
                occurred_before=_event_time(until),
                security_relevant=True if security else None,
            )
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(page.model_dump(mode="json"), as_json=state.json_output)


@events_app.command("tail")
def tail_events(
    ctx: typer.Context,
    after: Annotated[int, typer.Option(min=0)] = 0,
    count: Annotated[
        int,
        typer.Option(min=0, help="Stop after this many events; zero follows continuously."),
    ] = 0,
    event_type: Annotated[list[str] | None, typer.Option("--type")] = None,
    run_id: Annotated[str | None, typer.Option("--run")] = None,
    task_id: Annotated[str | None, typer.Option("--task")] = None,
    identity_id: Annotated[str | None, typer.Option("--identity")] = None,
    team_id: Annotated[str | None, typer.Option("--team")] = None,
    since: Annotated[str | None, typer.Option(help="Inclusive ISO-8601 timestamp.")] = None,
    security: Annotated[bool, typer.Option("--security", help="Only security events.")] = False,
) -> None:
    """Follow the resumable SSE stream from an explicit durable cursor."""
    emitted = 0
    with _daemon_client(ctx) as client:
        for event in client.stream_events(
            after=after,
            event_types=tuple(event_type or ()),
            run_id=run_id,
            task_id=task_id,
            identity_id=identity_id,
            team_id=team_id,
            occurred_after=_event_time(since),
            security_relevant=True if security else None,
        ):
            _emit(event.model_dump(mode="json"), as_json=_state(ctx).json_output)
            emitted += 1
            if count and emitted >= count:
                return


def _event_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        observed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("event time must be an ISO-8601 timestamp") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise typer.BadParameter("event time must include a timezone offset")
    return observed


@events_app.command("export")
def export_events(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="Atomic JSONL destination.")],
    after: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Export all currently retained events after a cursor as inspectable JSONL."""
    with _daemon_client(ctx) as client:
        count, cursor = client.export_events_jsonl(output, after=after)
    _emit(
        {"output": str(output), "events": count, "cursor": cursor},
        as_json=_state(ctx).json_output,
    )


@change_app.command("list")
def list_change_sets(
    ctx: typer.Context,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """Query bounded change-set state from mishkand."""
    with _daemon_client(ctx) as client:
        values = client.change_sets(offset=offset, limit=limit)
    _emit(
        [value.model_dump(mode="json") for value in values],
        as_json=_state(ctx).json_output,
    )


@artifact_app.command("list")
def list_artifacts(
    ctx: typer.Context,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """List bounded artifact manifests from mishkand."""

    with _daemon_client(ctx) as client:
        manifests = client.artifacts(offset=offset, limit=limit)
    _emit(
        [manifest.model_dump(mode="json") for manifest in manifests],
        as_json=_state(ctx).json_output,
    )


@artifact_app.command("show")
def show_artifact(ctx: typer.Context, reference: Annotated[str, typer.Argument()]) -> None:
    """Show one immutable artifact manifest."""

    with _daemon_client(ctx) as client:
        manifest = client.artifact(reference)
    _emit(manifest.model_dump(mode="json"), as_json=_state(ctx).json_output)


@artifact_app.command("reconcile-plan")
def plan_artifact_reconciliation(ctx: typer.Context) -> None:
    """Observe inconsistencies and persist a non-mutating reconciliation plan."""

    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="artifact.reconcile.plan",
                actor_id=client.principal_id,
                target_type="artifact_service",
                payload={},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@artifact_app.command("reconcile-apply")
def apply_artifact_reconciliation(
    ctx: typer.Context,
    plan_id: Annotated[str, typer.Argument()],
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Apply one previously persisted reconciliation plan exactly once."""

    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="artifact.reconcile.apply",
                actor_id=client.principal_id,
                target_type="artifact_reconciliation_plan",
                target_id=plan_id,
                expected_revision=expected_revision,
                payload={},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@change_app.command("plan")
def plan_change_set(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option("--file", help="Versioned change-set YAML or JSON.")],
) -> None:
    """Submit an immutable change-set plan to mishkand."""
    from mishkan.application import ApplicationCommand
    from mishkan.edits import ChangeSet

    change_set = ChangeSet.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
    with _daemon_client(ctx) as client:
        command = ApplicationCommand(
            command_type="change.plan",
            actor_id=client.principal_id,
            target_type="change_set",
            target_id=str(change_set.id),
            expected_revision=0,
            payload={"change_set": change_set.model_dump(mode="json")},
        )
        result = client.command(command)
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@change_app.command("apply")
def apply_change_set(
    ctx: typer.Context,
    change_set_id: Annotated[str, typer.Argument()],
    expected_revision: Annotated[int, typer.Option(min=0)] = 1,
) -> None:
    """Apply a previously planned change set through mishkand."""
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        command = ApplicationCommand(
            command_type="change.apply",
            actor_id=client.principal_id,
            target_type="change_set",
            target_id=change_set_id,
            expected_revision=expected_revision,
            payload={},
        )
        result = client.command(command)
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


def _start_session(ctx: typer.Context, source: Path, mode: str) -> None:
    from mishkan.application import ApplicationCommand
    from mishkan.execution import SessionMode, SessionRequest

    request = SessionRequest.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
    expected_mode = SessionMode(mode)
    if request.mode is not expected_mode:
        raise typer.BadParameter(f"request mode must be {mode}")
    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="session.start",
                actor_id=client.principal_id,
                target_type="session_service",
                payload={"request": request.model_dump(mode="json")},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


def _session_effect(
    ctx: typer.Context,
    session_id: str,
    command_type: str,
    payload: dict[str, object],
    expected_revision: int | None,
) -> None:
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type=command_type,
                actor_id=client.principal_id,
                target_type="session",
                target_id=session_id,
                expected_revision=expected_revision,
                payload=payload,
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@terminal_app.command("open")
def open_terminal(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option("--file", help="Versioned PTY request YAML.")],
) -> None:
    """Open a governed PTY session."""
    _start_session(ctx, source, "pty")


@job_app.command("start")
def start_job(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option("--file", help="Versioned job request YAML.")],
) -> None:
    """Start a governed managed job."""
    _start_session(ctx, source, "job")


@terminal_app.command("write")
def write_terminal(
    ctx: typer.Context,
    session_id: str,
    data: str,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Write UTF-8 input to an owned PTY."""
    import base64

    _session_effect(
        ctx,
        session_id,
        "session.write",
        {"content_base64": base64.b64encode(data.encode()).decode()},
        expected_revision,
    )


@terminal_app.command("resize")
def resize_terminal(
    ctx: typer.Context,
    session_id: str,
    rows: Annotated[int, typer.Option(min=1, max=1000)],
    columns: Annotated[int, typer.Option(min=1, max=4000)],
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Resize an owned PTY."""
    _session_effect(
        ctx,
        session_id,
        "session.resize",
        {"rows": rows, "columns": columns},
        expected_revision,
    )


def _read_session(
    ctx: typer.Context,
    session_id: str,
    channel: str,
    offset: int,
    limit: int,
    binary: bool,
) -> None:
    with _daemon_client(ctx) as client:
        result = client.session_output(
            session_id, channel=channel, offset=offset, limit=limit, binary=binary
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@terminal_app.command("read")
def read_terminal(
    ctx: typer.Context,
    session_id: str,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=16_777_216)] = 65_536,
    binary: bool = False,
) -> None:
    """Read PTY output from a durable cursor."""
    _read_session(ctx, session_id, "stdout", offset, limit, binary)


@job_app.command("read")
def read_job(
    ctx: typer.Context,
    session_id: str,
    channel: Annotated[str, typer.Option()] = "stdout",
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=16_777_216)] = 65_536,
    binary: bool = False,
) -> None:
    """Read managed-job output from a durable cursor."""
    _read_session(ctx, session_id, channel, offset, limit, binary)


@job_app.command("status")
@terminal_app.command("status")
def session_status(ctx: typer.Context, session_id: str) -> None:
    """Inspect one execution session."""
    with _daemon_client(ctx) as client:
        result = client.session(session_id)
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@job_app.command("signal")
@terminal_app.command("signal")
def signal_session(
    ctx: typer.Context,
    session_id: str,
    signal_name: str,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Send a profile-authorized signal to a proven process identity."""
    _session_effect(
        ctx,
        session_id,
        "session.signal",
        {"signal": signal_name},
        expected_revision,
    )


@job_app.command("stop")
@terminal_app.command("close")
def cancel_session(
    ctx: typer.Context,
    session_id: str,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Request durable cancellation and settle the session."""
    _session_effect(ctx, session_id, "session.cancel", {}, expected_revision)


@job_app.command("settle")
def settle_job(
    ctx: typer.Context,
    session_id: str,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Finalize completed job spools as immutable Artifacts."""
    _session_effect(ctx, session_id, "session.settle", {}, expected_revision)


@run_app.command("list")
def list_runs(
    ctx: typer.Context,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """List bounded durable run projections."""
    with _daemon_client(ctx) as client:
        values = client.runs(offset=offset, limit=limit)
    _emit(values, as_json=_state(ctx).json_output)


@run_app.command("tasks")
def list_run_tasks(
    ctx: typer.Context,
    run_id: str,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """List bounded task projections for one run."""
    with _daemon_client(ctx) as client:
        values = client.tasks(run_id, offset=offset, limit=limit)
    _emit(values, as_json=_state(ctx).json_output)


@run_app.command("cancel")
def cancel_run(
    ctx: typer.Context,
    run_id: str,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Persist monotone cancellation before stopping new eligibility."""
    _run_effect(ctx, run_id, "run.cancel", {}, expected_revision)


@run_app.command("recover")
def recover_run(
    ctx: typer.Context,
    run_id: str,
    uncertain_effect: Annotated[
        list[str] | None,
        typer.Option("--uncertain-effect", help="Unreconciled effect; repeat as needed."),
    ] = None,
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Release interrupted tasks only after effect reconciliation."""
    _run_effect(
        ctx,
        run_id,
        "run.recover",
        {"uncertain_effects": uncertain_effect or []},
        expected_revision,
    )


def _run_effect(
    ctx: typer.Context,
    run_id: str,
    command_type: str,
    payload: dict[str, object],
    expected_revision: int | None,
) -> None:
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type=command_type,
                actor_id=client.principal_id,
                target_type="run",
                target_id=run_id,
                expected_revision=expected_revision,
                payload=payload,
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@mcp_app.command("connect")
def connect_mcp(
    ctx: typer.Context,
    connection_id: Annotated[str, typer.Argument(help="Configured MCP connection identity.")],
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Explicitly connect or reconnect one configured MCP peer and discover its claims."""
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="mcp.connection.connect",
                actor_id=client.principal_id,
                target_type="mcp_connection",
                target_id=connection_id,
                expected_revision=expected_revision,
                payload={},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@mcp_app.command("connections")
def list_mcp_connections(
    ctx: typer.Context,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """List bounded durable MCP connection states."""
    with _daemon_client(ctx) as client:
        values = client.mcp_connections(offset=offset, limit=limit)
    _emit(list(values), as_json=_state(ctx).json_output)


@mcp_app.command("primitives")
def list_mcp_primitives(
    ctx: typer.Context,
    connection_id: Annotated[str, typer.Argument(help="Configured MCP connection identity.")],
) -> None:
    """List normalized claims from the accepted discovery snapshot."""
    with _daemon_client(ctx) as client:
        values = client.mcp_primitives(connection_id)
    _emit(list(values), as_json=_state(ctx).json_output)


@mcp_app.command("calls")
def list_mcp_calls(
    ctx: typer.Context,
    offset: Annotated[int, typer.Option(min=0)] = 0,
    limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
) -> None:
    """List bounded durable outbound MCP call journals."""
    with _daemon_client(ctx) as client:
        values = client.mcp_calls(offset=offset, limit=limit)
    _emit(list(values), as_json=_state(ctx).json_output)


@mcp_app.command("contracts")
def list_mcp_contracts(
    ctx: typer.Context,
    connection_id: Annotated[str, typer.Argument(help="Configured MCP connection identity.")],
) -> None:
    """List candidate Gateway contracts derived from one accepted discovery snapshot."""
    with _daemon_client(ctx) as client:
        values = client.mcp_contracts(connection_id)
    _emit(list(values), as_json=_state(ctx).json_output)


@mcp_app.command("progress")
def list_mcp_progress(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="MCP call request UUID.")],
    cursor: Annotated[int, typer.Option(min=0)] = 0,
) -> None:
    """Read durable progress from an exact monotone cursor."""
    with _daemon_client(ctx) as client:
        values = client.mcp_progress(request_id, cursor=cursor)
    _emit(list(values), as_json=_state(ctx).json_output)


@mcp_app.command("cancel")
def cancel_mcp_call(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="MCP call request UUID.")],
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Request cancellation without claiming that a remote effect was stopped."""
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="mcp.call.cancel",
                actor_id=client.principal_id,
                target_type="mcp_call",
                target_id=request_id,
                expected_revision=expected_revision,
                payload={},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@mcp_app.command("reconcile")
def reconcile_mcp_call(
    ctx: typer.Context,
    request_id: Annotated[str, typer.Argument(help="Recoverable MCP call request UUID.")],
    expected_revision: Annotated[int | None, typer.Option(min=0)] = None,
) -> None:
    """Reconnect to a durable remote task and accept only its proven terminal result."""
    from mishkan.application import ApplicationCommand

    with _daemon_client(ctx) as client:
        result = client.command(
            ApplicationCommand(
                command_type="mcp.call.reconcile",
                actor_id=client.principal_id,
                target_type="mcp_call",
                target_id=request_id,
                expected_revision=expected_revision,
                payload={},
            )
        )
    _emit(result.model_dump(mode="json"), as_json=_state(ctx).json_output)


@schema_app.command("export")
def export_contract_schemas(
    ctx: typer.Context,
    output: Annotated[Path, typer.Option(help="Schema output directory.")] = Path(
        "definitions/schemas"
    ),
) -> None:
    """Export deterministic JSON Schemas for public I00 contracts."""

    paths = export_schemas(output)
    _emit({"exported": [str(path) for path in paths]}, as_json=_state(ctx).json_output)


@app.command("init")
def initialize_repository(
    ctx: typer.Context,
    objective: Annotated[
        str,
        typer.Argument(help="Repository-specific initialization objective."),
    ],
    repository: Annotated[
        Path | None,
        typer.Option("--repository", "-r", help="Git repository to initialize."),
    ] = None,
) -> None:
    """Submit repository initialization to the authoritative local daemon."""

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    workspace = effective.value.project.workspace.resolve()
    if repository is not None and repository.resolve() != workspace:
        raise typer.BadParameter(
            "--repository must match the workspace configured for this mishkand instance",
            param_hint="--repository",
        )
    try:
        from mishkan.application import ApplicationCommand, RunInitializationRequest

        with _daemon_client(ctx) as client:
            result = client.command(
                ApplicationCommand(
                    command_type="run.initialize",
                    actor_id=client.principal_id,
                    target_type="run",
                    payload=RunInitializationRequest(objective=objective).model_dump(mode="json"),
                )
            )
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(result.model_dump(mode="json"), as_json=state.json_output)


if __name__ == "__main__":
    app()
