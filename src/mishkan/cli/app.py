"""Thin CLI over I00 configuration application functions."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
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
change_app = typer.Typer(help="Plan, apply, and inspect recoverable change sets.")
app.add_typer(config_app, name="config")
app.add_typer(schema_app, name="schema")
app.add_typer(daemon_app, name="daemon")
daemon_app.add_typer(daemon_token_app, name="token")
app.add_typer(database_app, name="db")
app.add_typer(events_app, name="events")
app.add_typer(change_app, name="change")


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
    source: Annotated[Path, typer.Option("--file", help="Explicit schema 1.1 YAML source.")],
) -> None:
    """Explicitly migrate one configuration source to schema 1.2."""
    from mishkan.config.migration import migrate_to_1_2

    state = _state(ctx)
    try:
        target = migrate_to_1_2(source)
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
) -> None:
    """Query a bounded page from the durable daemon stream."""
    state = _state(ctx)
    try:
        with _daemon_client(ctx) as client:
            page = client.events(after=after, limit=limit)
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(page.model_dump(mode="json"), as_json=state.json_output)


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


@change_app.command("plan")
def plan_change_set(
    ctx: typer.Context,
    source: Annotated[Path, typer.Option("--file", help="Versioned change-set YAML or JSON.")],
) -> None:
    """Submit an immutable change-set plan to mishkand."""
    from mishkan.application import ApplicationCommand
    from mishkan.edits import ChangeSet

    change_set = ChangeSet.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))
    command = ApplicationCommand(
        command_type="change.plan",
        actor_id="local-operator",
        target_type="change_set",
        target_id=str(change_set.id),
        expected_revision=0,
        payload={"change_set": change_set.model_dump(mode="json")},
    )
    with _daemon_client(ctx) as client:
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

    command = ApplicationCommand(
        command_type="change.apply",
        actor_id="local-operator",
        target_type="change_set",
        target_id=change_set_id,
        expected_revision=expected_revision,
        payload={},
    )
    with _daemon_client(ctx) as client:
        result = client.command(command)
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
    """Run the read-only CrewAI initialization flow and durably resume it."""

    state = _state(ctx)
    effective = _load_or_exit(ctx)
    target = repository or effective.value.project.workspace
    try:
        from mishkan.application.initialize import MishkanInitializer

        report = MishkanInitializer().run(effective.value, target, objective)
    except MishkanError as error:
        _emit_error(error, as_json=state.json_output)
        raise typer.Exit(code=2) from error
    _emit(report.model_dump(mode="json"), as_json=state.json_output)


if __name__ == "__main__":
    app()
