import json
from pathlib import Path

from typer.testing import CliRunner

from mishkan.cli.app import app

runner = CliRunner()
FIXTURES = Path(__file__).parents[2] / "fixtures" / "config"


def test_setup_validate_show_and_set_round_trip(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    setup = runner.invoke(
        app,
        ["--json", "config", "setup", "--preset", "local", "--output", str(config)],
    )
    assert setup.exit_code == 0, setup.output
    assert config.exists()

    validate = runner.invoke(app, ["--json", "--config", str(config), "config", "validate"])
    assert validate.exit_code == 0, validate.output
    assert json.loads(validate.stdout)["valid"] is True

    update = runner.invoke(
        app,
        ["--json", "config", "set", "timezone", "Africa/Ouagadougou", "--file", str(config)],
    )
    assert update.exit_code == 0, update.output

    show = runner.invoke(app, ["--json", "--config", str(config), "config", "show"])
    payload = json.loads(show.stdout)
    assert show.exit_code == 0, show.output
    assert payload["configuration"]["timezone"] == "Africa/Ouagadougou"
    assert payload["provenance"]["field_sources"]["timezone"] == str(config.resolve())


def test_starting_without_configuration_fails_and_creates_no_state() -> None:
    result = runner.invoke(app, ["--json", "config", "validate"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "ERR-CFG-001"


def test_unsupported_schema_fails_with_version_error() -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "--config",
            str(FIXTURES / "unsupported-schema.yaml"),
            "config",
            "validate",
        ],
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["code"] == "ERR-VER-001"
    assert payload["details"]["automatic_migration"] is False


def test_show_exposes_only_credential_reference_not_resolved_secret(
    tmp_path: Path, monkeypatch: object
) -> None:
    canary = "sk-live-secret-canary"
    monkeypatch.setenv("OPENAI_API_KEY", canary)  # type: ignore[attr-defined]
    config = tmp_path / "cloud.yaml"
    setup = runner.invoke(
        app,
        ["config", "setup", "--preset", "cloud", "--output", str(config)],
    )
    assert setup.exit_code == 0, setup.output

    show = runner.invoke(app, ["--json", "--config", str(config), "config", "show"])
    assert show.exit_code == 0, show.output
    assert "OPENAI_API_KEY" in show.stdout
    assert canary not in show.stdout


def test_schema_export_is_deterministic(tmp_path: Path) -> None:
    output = tmp_path / "schemas"
    first = runner.invoke(app, ["--json", "schema", "export", "--output", str(output)])
    assert first.exit_code == 0, first.output
    first_contents = {path.name: path.read_bytes() for path in output.iterdir()}

    second = runner.invoke(app, ["--json", "schema", "export", "--output", str(output)])
    assert second.exit_code == 0, second.output
    assert {path.name: path.read_bytes() for path in output.iterdir()} == first_contents
