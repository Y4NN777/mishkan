import json
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner

import mishkan.cli.app as cli_module
from mishkan.application import CommandResult, CommandStatus
from mishkan.cli.app import app
from mishkan.domain.time import utc_now


def test_init_cli_submits_the_objective_through_mishkand(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class FakeClient:
        principal_id = "local-operator"

        def command(self, command):  # type: ignore[no-untyped-def]
            assert command.command_type == "run.initialize"
            assert command.target_type == "run"
            assert command.payload == {
                "schema_version": "1.0",
                "objective": "Inspect repository evidence",
            }
            return CommandResult(
                command_id=UUID("00000000-0000-4000-8000-000000000001"),
                status=CommandStatus.ACCEPTED,
                target_type="run",
                target_id="local-instance",
                revision=1,
                event_cursor=1,
                payload={"run_id": "run-id"},
                completed_at=utc_now(),
            )

    @contextmanager
    def fake_daemon_client(_ctx):  # type: ignore[no-untyped-def]
        yield FakeClient()

    monkeypatch.setattr(cli_module, "_daemon_client", fake_daemon_client)

    cli_result = CliRunner().invoke(
        app,
        [
            "--json",
            "--config",
            str(Path("tests/fixtures/config/local-valid.yaml")),
            "init",
            "Inspect repository evidence",
            "--repository",
            ".",
        ],
    )

    assert cli_result.exit_code == 0, cli_result.output
    payload = json.loads(cli_result.stdout)
    assert payload["status"] == "accepted"
    assert payload["payload"]["run_id"] == "run-id"
