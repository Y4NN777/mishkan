from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from typer.testing import CliRunner

import mishkan.cli.app as cli
from mishkan.notifications import NotificationDelivery, NotificationSeverity
from mishkan.organization import ProfessionalEvidenceKind


@dataclass(frozen=True)
class _Payload:
    kind: str

    def model_dump(self, *, mode: str) -> dict[str, str]:
        assert mode == "json"
        return {"kind": self.kind}


class _Client:
    principal_id = "operator:test"

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def organization(self) -> _Payload:
        return _Payload("organization")

    def organization_inspection(self, *, limit: int) -> dict[str, str]:
        assert limit == 13
        return {"kind": "organization-inspection"}

    def organization_branch_inspection(
        self,
        branch_id: str,
        *,
        limit: int,
    ) -> dict[str, str]:
        assert (branch_id, limit) == ("software-engineering", 14)
        return {"kind": "branch-inspection"}

    def professional_competence(
        self,
        identity_id: str,
        *,
        kind: ProfessionalEvidenceKind,
        subject: str,
    ) -> _Payload:
        assert (identity_id, kind, subject) == (
            "backend-engineer",
            ProfessionalEvidenceKind.DEMONSTRATED_COMPETENCE,
            "postgresql",
        )
        return _Payload("competence")

    def professional_evidence(self, identity_id: str, **filters: Any) -> tuple[_Payload, ...]:
        assert identity_id == "backend-engineer"
        assert filters == {"kind": None, "subject": None, "offset": 0, "limit": 10}
        return (_Payload("professional-evidence"),)

    def professional_promotions(self, identity_id: str, **filters: Any) -> tuple[_Payload, ...]:
        assert identity_id == "backend-engineer"
        assert filters == {"kind": None, "subject": None, "offset": 0, "limit": 10}
        return (_Payload("professional-promotion"),)

    def missions(self, *, limit: int) -> tuple[_Payload, ...]:
        assert limit == 3
        return (_Payload("mission"),)

    def mission(self, mission_id: str) -> _Payload:
        assert mission_id == "mission/id"
        return _Payload("mission-detail")

    def mission_inspection(self, mission_id: str, *, limit: int) -> dict[str, str]:
        assert (mission_id, limit) == ("mission/id", 11)
        return {"kind": "mission-inspection"}

    def create_prospective_run(self, **request: str) -> dict[str, str]:
        assert request == {
            "workspace_id": "prospective:cli-fixture",
            "objective": "Build the prospective service",
            "outcome_id": "greenfield",
        }
        return {"kind": "prospective-run"}

    def establish_repository(self, run_id: str, **request: Any) -> dict[str, str]:
        assert run_id == "run-prospective"
        assert request == {
            "prospective_workspace_id": "prospective:cli-fixture",
            "discovery_revision": "a" * 64,
            "evidence_references": ("artifact:establishment",),
        }
        return {"kind": "repository-establishment"}

    def mission_templates(
        self,
        *,
        signals: tuple[str, ...] | None,
        organization_version: str,
    ) -> tuple[_Payload, ...]:
        assert signals == ("incident", "production")
        assert organization_version == "2"
        return (_Payload("mission-template"),)

    def mission_brief(self, mission_id: str, *, version: int | None) -> _Payload:
        assert (mission_id, version) == ("mission/id", 2)
        return _Payload("mission-brief")

    def mission_crew(self, mission_id: str, *, version: int | None) -> _Payload:
        assert (mission_id, version) == ("mission/id", 3)
        return _Payload("mission-crew")

    def mission_assignments(self, mission_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 4)
        return (_Payload("mission-assignment"),)

    def mission_run_bindings(self, mission_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 12)
        return (_Payload("mission-run-binding"),)

    def mission_transitions(self, mission_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 5)
        return (_Payload("mission-transition"),)

    def mission_decisions(self, mission_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 10)
        return (_Payload("mission-decision"),)

    def mission_environment_plan(self, mission_id: str, *, version: int | None) -> _Payload:
        assert (mission_id, version) == ("mission/id", 2)
        return _Payload("environment-plan")

    def mission_environment_readiness(self, mission_id: str) -> _Payload:
        assert mission_id == "mission/id"
        return _Payload("environment-readiness")

    def mission_task_eligibility(self, mission_id: str, task_id: str) -> _Payload:
        assert (mission_id, task_id) == ("mission/id", "task/id")
        return _Payload("task-eligibility")

    def mission_completion_readiness(self, mission_id: str) -> _Payload:
        assert mission_id == "mission/id"
        return _Payload("completion-readiness")

    def conversations(
        self,
        *,
        mission_id: str | None,
        limit: int,
    ) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 6)
        return (_Payload("conversation"),)

    def conversation(self, conversation_id: str) -> _Payload:
        assert conversation_id == "conversation/id"
        return _Payload("conversation-detail")

    def conversation_messages(self, conversation_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (conversation_id, limit) == ("conversation/id", 7)
        return (_Payload("message"),)

    def mission_escalations(
        self,
        mission_id: str,
        *,
        state: Any,
        limit: int,
    ) -> tuple[_Payload, ...]:
        assert mission_id == "mission/id"
        assert state.value == "open"
        assert limit == 8
        return (_Payload("escalation"),)

    def mission_interventions(self, mission_id: str, *, limit: int) -> tuple[_Payload, ...]:
        assert (mission_id, limit) == ("mission/id", 9)
        return (_Payload("intervention"),)

    def community_candidates(self) -> tuple[_Payload, ...]:
        return (_Payload("candidate"),)

    def notifications(
        self,
        *,
        after: int,
        limit: int | None,
        severities: tuple[NotificationSeverity, ...],
        deliveries: tuple[NotificationDelivery, ...],
    ) -> _Payload:
        assert (after, limit, severities, deliveries) == (
            2,
            3,
            (NotificationSeverity.URGENT,),
            (NotificationDelivery.FEED,),
        )
        return _Payload("notifications")

    def post_message(self, message: Any) -> _Payload:
        assert str(message.conversation_id) == "00000000-0000-4000-8000-000000000001"
        assert message.author_identity == "CEO"
        assert message.body == "Ship the verified scope"
        assert message.evidence_references == ("evidence:42",)
        return _Payload("chat-message")


runner = CliRunner()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> _Client:
    value = _Client()
    monkeypatch.setattr(cli, "_daemon_client", lambda _ctx: value)
    return value


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (("org", "show"), {"kind": "organization"}),
        (("org", "inspect", "--limit", "13"), {"kind": "organization-inspection"}),
        (
            ("org", "branch", "software-engineering", "--limit", "14"),
            {"kind": "branch-inspection"},
        ),
        (
            (
                "org",
                "competence",
                "backend-engineer",
                "--kind",
                "demonstrated_competence",
                "--subject",
                "postgresql",
            ),
            {"kind": "competence"},
        ),
        (
            ("org", "evidence", "backend-engineer", "--limit", "10"),
            [{"kind": "professional-evidence"}],
        ),
        (
            ("org", "promotions", "backend-engineer", "--limit", "10"),
            [{"kind": "professional-promotion"}],
        ),
        (("mission", "list", "--limit", "3"), [{"kind": "mission"}]),
        (("mission", "show", "mission/id"), {"kind": "mission-detail"}),
        (
            ("mission", "inspect", "mission/id", "--limit", "11"),
            {"kind": "mission-inspection"},
        ),
        (
            (
                "run",
                "prospective-create",
                "--workspace-id",
                "prospective:cli-fixture",
                "--objective",
                "Build the prospective service",
                "--outcome-id",
                "greenfield",
            ),
            {"kind": "prospective-run"},
        ),
        (
            (
                "run",
                "repository-establish",
                "run-prospective",
                "--workspace-id",
                "prospective:cli-fixture",
                "--discovery-revision",
                "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "--evidence",
                "artifact:establishment",
            ),
            {"kind": "repository-establishment"},
        ),
        (
            (
                "mission",
                "templates",
                "--signal",
                "incident",
                "--signal",
                "production",
                "--organization-version",
                "2",
            ),
            [{"kind": "mission-template"}],
        ),
        (("mission", "brief", "mission/id", "--version", "2"), {"kind": "mission-brief"}),
        (("mission", "crew", "mission/id", "--version", "3"), {"kind": "mission-crew"}),
        (
            ("mission", "assignments", "mission/id", "--limit", "4"),
            [{"kind": "mission-assignment"}],
        ),
        (
            ("mission", "run-bindings", "mission/id", "--limit", "12"),
            [{"kind": "mission-run-binding"}],
        ),
        (
            ("mission", "transitions", "mission/id", "--limit", "5"),
            [{"kind": "mission-transition"}],
        ),
        (
            ("mission", "decisions", "mission/id", "--limit", "10"),
            [{"kind": "mission-decision"}],
        ),
        (
            ("mission", "environment-plan", "mission/id", "--version", "2"),
            {"kind": "environment-plan"},
        ),
        (
            ("mission", "readiness", "mission/id"),
            {"kind": "environment-readiness"},
        ),
        (
            ("mission", "task-eligibility", "mission/id", "task/id"),
            {"kind": "task-eligibility"},
        ),
        (
            ("mission", "completion-readiness", "mission/id"),
            {"kind": "completion-readiness"},
        ),
        (
            ("conversation", "list", "--mission", "mission/id", "--limit", "6"),
            [{"kind": "conversation"}],
        ),
        (
            ("conversation", "show", "conversation/id"),
            {"kind": "conversation-detail"},
        ),
        (
            ("conversation", "messages", "conversation/id", "--limit", "7"),
            [{"kind": "message"}],
        ),
        (
            ("intervention", "escalations", "mission/id", "--state", "open", "--limit", "8"),
            [{"kind": "escalation"}],
        ),
        (
            ("intervention", "interventions", "mission/id", "--limit", "9"),
            [{"kind": "intervention"}],
        ),
        (
            ("advisory", "candidates"),
            {
                "candidates": [{"kind": "candidate"}],
                "count": 1,
                "activation_authorized": False,
            },
        ),
        (
            (
                "events",
                "notifications",
                "--after",
                "2",
                "--limit",
                "3",
                "--severity",
                "urgent",
                "--delivery",
                "feed",
            ),
            {"kind": "notifications"},
        ),
    ],
)
def test_i06_read_surfaces_use_shared_daemon_client(
    client: _Client,
    arguments: tuple[str, ...],
    expected: object,
) -> None:
    del client
    result = runner.invoke(cli.app, ["--json", *arguments])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == expected


def test_chat_posts_through_the_same_durable_message_contract(client: _Client) -> None:
    del client
    result = runner.invoke(
        cli.app,
        [
            "--json",
            "chat",
            "--conversation",
            "00000000-0000-4000-8000-000000000001",
            "--author",
            "CEO",
            "--message",
            "Ship the verified scope",
            "--evidence",
            "evidence:42",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"kind": "chat-message"}


@pytest.mark.parametrize(
    "arguments",
    [
        ("org", "evidence-record", "--evidence", "missing.json"),
        (
            "org",
            "promotion-decide",
            "--request",
            "missing.json",
            "--disposition",
            "accepted",
            "--reason",
            "proved",
        ),
        ("mission", "create", "--record", "missing.json"),
        ("mission", "task-claim", "--request", "missing.json"),
        ("mission", "governance-propose", "--request", "missing.json"),
        (
            "mission",
            "governance-escalate",
            "--proposal",
            "missing.json",
            "--conversation",
            "00000000-0000-4000-8000-000000000001",
        ),
        (
            "mission",
            "brief-record",
            "--brief",
            "missing.json",
            "--expected-revision",
            "0",
        ),
        (
            "mission",
            "crew-record",
            "--crew",
            "missing.json",
            "--expected-revision",
            "0",
        ),
        ("mission", "assignment-record", "--assignment", "missing.json"),
        (
            "mission",
            "transition",
            "--transition",
            "missing.json",
            "--expected-revision",
            "0",
        ),
        ("mission", "environment-propose", "--request", "missing.json"),
        (
            "mission",
            "environment-accept",
            "--plan",
            "missing.json",
            "--expected-revision",
            "0",
        ),
        ("conversation", "create", "--channel", "missing.json"),
        ("conversation", "post", "--message", "missing.json"),
        ("intervention", "decision-record", "--decision", "missing.json"),
        ("intervention", "escalation-open", "--escalation", "missing.json"),
        (
            "intervention",
            "apply",
            "--intervention",
            "missing.json",
            "--expected-revision",
            "0",
        ),
        ("advisory", "recommend", "--request", "missing.json"),
    ],
)
def test_i06_contract_commands_reject_missing_inputs(
    client: _Client,
    arguments: tuple[str, ...],
) -> None:
    del client
    result = runner.invoke(cli.app, ["--json", *arguments])

    assert result.exit_code == 2
    assert "must contain a valid" in result.output
