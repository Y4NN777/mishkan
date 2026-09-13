from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from typer.testing import CliRunner

import mishkan.cli.app as cli


@dataclass(frozen=True)
class _Payload:
    kind: str

    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "json"
        return {"kind": self.kind}


class _Client:
    principal_id = "operator:test"

    def __init__(self) -> None:
        self.command_types: list[str] = []

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def engineer_profile(self) -> _Payload:
        return _Payload("engineer-profile")

    def community_candidates(self) -> tuple[_Payload, ...]:
        return (_Payload("community-candidate"),)

    def telemetry_status(self) -> _Payload:
        return _Payload("telemetry-status")

    def skills(self, **kwargs: object) -> tuple[_Payload, ...]:
        assert kwargs == {"name": "review", "offset": 2, "limit": 3}
        return (_Payload("skill-version"),)

    def active_skill(self, name: str) -> _Payload | None:
        return None if name == "missing" else _Payload("active-skill")

    def skill_usage_summary(self, task_class: str, *, skill_name: str | None) -> _Payload:
        assert (task_class, skill_name) == ("software.review", "review")
        return _Payload("usage")

    def skill_updates(self) -> _Payload:
        return _Payload("updates")

    def skill_curation(self) -> tuple[_Payload, ...]:
        return (_Payload("curation"),)

    def invoke_skill(self, request: Any) -> _Payload:
        assert request.context.consuming_identity == self.principal_id
        return _Payload("invocation")

    def command(self, command: Any) -> _Payload:
        assert command.actor_id == self.principal_id
        self.command_types.append(command.command_type)
        return _Payload(command.command_type)

    def observe_environment(self, request: Any) -> _Payload:
        assert request.actor_identity == self.principal_id
        assert request.context_id == "mission:test"
        assert request.execution_location == "local:host"
        return _Payload("observation")

    def environment_observation(self, observation_id: str) -> _Payload:
        assert observation_id == "observation/id"
        return _Payload("observation-query")

    def environment_binding(self, binding_id: str) -> _Payload:
        assert binding_id == "binding/id"
        return _Payload("binding-query")

    def engineering_command_candidates(self, observation_id: str) -> tuple[_Payload, ...]:
        assert observation_id == "observation/id"
        return (_Payload("command-candidate"),)

    def environment_descriptor_set(self, descriptor_set_id: str) -> _Payload:
        assert descriptor_set_id == "descriptor/id"
        return _Payload("descriptor-set")

    def environment_attempt(self, attempt_id: str) -> _Payload:
        assert attempt_id == "attempt/id"
        return _Payload("attempt")

    def environment_verification(self, verification_id: str) -> _Payload:
        assert verification_id == "verification/id"
        return _Payload("verification")


runner = CliRunner()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    value = _Client()
    monkeypatch.setattr(cli, "_daemon_client", lambda _ctx: value)
    return value


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("context", "engineer-profile"), {"kind": "engineer-profile"}),
        (
            ("context", "community-candidates"),
            {
                "candidates": [{"kind": "community-candidate"}],
                "count": 1,
                "activation_authorized": False,
            },
        ),
        (("telemetry", "status"), {"kind": "telemetry-status"}),
        (
            ("skill", "list", "--name", "review", "--offset", "2", "--limit", "3"),
            [{"kind": "skill-version"}],
        ),
        (("skill", "active", "review"), {"kind": "active-skill"}),
        (("skill", "active", "missing"), None),
        (
            ("skill", "usage", "software.review", "--name", "review"),
            {"kind": "usage"},
        ),
        (("skill", "updates"), {"kind": "updates"}),
        (("skill", "curation"), [{"kind": "curation"}]),
        (
            (
                "environment",
                "observe",
                "--context-id",
                "mission:test",
                "--execution-location",
                "local:host",
            ),
            {"kind": "observation"},
        ),
        (
            ("environment", "observation", "observation/id"),
            {"kind": "observation-query"},
        ),
        (("environment", "binding", "binding/id"), {"kind": "binding-query"}),
        (
            ("environment", "command-candidates", "observation/id"),
            [{"kind": "command-candidate"}],
        ),
        (
            ("environment", "descriptor-set", "descriptor/id"),
            {"kind": "descriptor-set"},
        ),
        (("environment", "attempt", "attempt/id"), {"kind": "attempt"}),
        (
            ("environment", "verification", "verification/id"),
            {"kind": "verification"},
        ),
    ],
)
def test_i05_read_surfaces_use_the_shared_daemon_client(
    client: _Client,
    arguments: tuple[str, ...],
    expected: object,
) -> None:
    del client
    result = runner.invoke(cli.app, ["--json", *arguments])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        ("context", "recommend", "--request", "missing.json"),
        ("telemetry", "import-langsmith-feedback", "--request", "missing.json"),
        ("environment", "resolve", "--request", "missing.json"),
        ("environment", "plan-command", "--request", "missing.json"),
        ("environment", "start-command", "--request", "missing.json"),
        ("environment", "validate-descriptors", "--set", "missing.json"),
        ("environment", "plan-descriptor-change", "--request", "missing.json"),
        ("environment", "plan-operation", "--request", "missing.json"),
        ("environment", "start-operation", "--request", "missing.json"),
        ("environment", "settle-attempt", "--plan", "missing.json", "--session-id", "x"),
        ("environment", "verify", "--request", "missing.json"),
        ("environment", "invalidate", "--request", "missing.json"),
    ],
)
def test_i05_json_input_commands_refuse_missing_or_invalid_contracts(
    client: _Client,
    arguments: tuple[str, ...],
) -> None:
    del client
    result = runner.invoke(cli.app, ["--json", *arguments])

    assert result.exit_code == 2
    assert "must contain a valid" in result.output


def test_skill_invocation_builds_an_identity_bound_request(client: _Client) -> None:
    result = runner.invoke(
        cli.app,
        [
            "--json",
            "skill",
            "invoke",
            "task:review",
            "software.review",
            "/review",
            "--available-tool",
            "file.read",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"kind": "invocation"}


@pytest.mark.parametrize(
    ("arguments", "command_type"),
    [
        (
            (
                "skill",
                "decide",
                "00000000-0000-4000-8000-000000000001",
                "--disposition",
                "allow",
                "--reason",
                "reviewed",
            ),
            "skill.version.decide",
        ),
        (
            (
                "skill",
                "archive",
                "00000000-0000-4000-8000-000000000001",
                "--version-revision",
                "1",
                "--reason",
                "stale",
            ),
            "skill.version.archive",
        ),
        (
            (
                "skill",
                "delete",
                "00000000-0000-4000-8000-000000000001",
                "--version-revision",
                "1",
                "--reason",
                "withdrawn",
            ),
            "skill.version.delete",
        ),
        (
            (
                "skill",
                "restore",
                "00000000-0000-4000-8000-000000000001",
                "--version-revision",
                "2",
                "--reason",
                "needed",
            ),
            "skill.version.restore",
        ),
        (
            (
                "skill",
                "reset",
                "00000000-0000-4000-8000-000000000001",
                "--version-revision",
                "2",
                "--expected-active",
                "00000000-0000-4000-8000-000000000002",
                "--reason",
                "regression",
            ),
            "skill.version.reset",
        ),
        (
            ("skill", "pin", "00000000-0000-4000-8000-000000000001", "--version-revision", "2"),
            "skill.version.pin",
        ),
        (
            ("skill", "unpin", "00000000-0000-4000-8000-000000000001", "--version-revision", "2"),
            "skill.version.unpin",
        ),
    ],
)
def test_skill_lifecycle_cli_emits_governed_application_commands(
    client: _Client,
    arguments: tuple[str, ...],
    command_type: str,
) -> None:
    result = runner.invoke(cli.app, ["--json", *arguments])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"kind": command_type}
    assert client.command_types[-1] == command_type
