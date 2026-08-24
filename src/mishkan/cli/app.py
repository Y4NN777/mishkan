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
app.add_typer(config_app, name="config")
app.add_typer(schema_app, name="schema")


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
