from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from uuid import uuid4

import pytest

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig
from mishkan.config.presets import preset_text
from mishkan.conversations import EscalationOption
from mishkan.crewai.mission_governance import (
    CrewAIMissionGovernanceRunner,
    CTOMissionReview,
    PMMissionProposal,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions import (
    CrewAssignmentKind,
    CrewSelectionEvidence,
    MissionCrewMember,
    MissionEnvironmentIntent,
    MissionOrigin,
    MissionOriginKind,
    MissionRecord,
)
from mishkan.organization import load_canonical_organization


def _config(tmp_path: Path) -> MishkanConfig:
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    return ConfigLoader().load([source]).value


def _mission() -> MissionRecord:
    organization = load_canonical_organization()
    return MissionRecord(
        origin=MissionOrigin(
            kind=MissionOriginKind.CEO,
            actor_id="CEO",
            objective="Implement durable account recovery",
        ),
        organization_id=organization.organization_id,
        organization_version=organization.organization_version,
    )


def _pm() -> PMMissionProposal:
    return PMMissionProposal(
        problem="Users cannot recover access after losing a credential",
        desired_outcome="A verified recovery flow with explicit residual risk",
        scope=("repository:api",),
        exclusions=("production deployment",),
        acceptance_criteria=("independent recovery test passes",),
        constraints=("preserve existing accounts",),
        risks=("account takeover",),
        authority_scope=("repository:api",),
        proposed_identity_ids=(
            "Backend_Service_Engineer",
            "Product_Functional_Evaluator",
            "Technical_Change_Reporter",
        ),
        evidence_requirements=("test report", "security review"),
        escalation_conditions=("material security tradeoff",),
        environment_intent=MissionEnvironmentIntent(
            required_evidence=("environment readiness",), environment_dependent=True
        ),
        rationale="The proposal covers the product outcome and independent acceptance",
        evidence_references=("evidence:product-analysis",),
    )


def _member(identity_id: str, kind: CrewAssignmentKind) -> MissionCrewMember:
    return MissionCrewMember(
        identity_id=identity_id,
        assignment_kind=kind,
        responsibility=f"Perform attributable {kind.value} work",
        selection_evidence=CrewSelectionEvidence(
            project_references=("repository:api",),
            competence_references=(f"profile:{identity_id}:competence",),
            availability_references=(f"profile:{identity_id}:availability",),
            conflict_assessment="No production, evaluation, or reporting conflict",
            risk_coverage=("account recovery",),
            independence_references=(f"profile:{identity_id}:independence",),
        ),
    )


def _cto(
    disposition: Literal["confirmed", "rejected"] = "confirmed",
) -> CTOMissionReview:
    rejected = disposition == "rejected"
    return CTOMissionReview(
        disposition=disposition,
        rationale="Technical, security, quality, and reporting coverage is explicit",
        evidence_references=("evidence:technical-review",),
        coverage=("technical", "security", "quality", "operability"),
        mission_lead_id="Backend_Service_Engineer",
        approved_members=(
            _member("Backend_Service_Engineer", CrewAssignmentKind.PRODUCTION),
            _member("Product_Functional_Evaluator", CrewAssignmentKind.EVALUATION),
            _member("Technical_Change_Reporter", CrewAssignmentKind.REPORTING),
        ),
        unresolved_findings=() if not rejected else ("risk unresolved",),
        disputed_scope=() if not rejected else ("task:security-design",),
        alternatives=(
            ()
            if not rejected
            else (
                EscalationOption(
                    option_id="pm-proposal",
                    description="Proceed with the PM recovery design",
                    consequences=("delivery continues",),
                    risks=("security coverage remains disputed",),
                ),
                EscalationOption(
                    option_id="cto-remediation",
                    description="Add security remediation before implementation",
                    consequences=("delivery is delayed",),
                    risks=("product milestone may move",),
                ),
            )
        ),
        pm_recommended_option_id="pm-proposal" if rejected else None,
        cto_recommended_option_id="cto-remediation" if rejected else None,
        independent_work_continuing=("task:documentation",) if rejected else (),
    )


def test_pm_cto_outputs_compile_to_confirmed_brief_and_contextual_crew(tmp_path: Path) -> None:
    runner = CrewAIMissionGovernanceRunner(_config(tmp_path))
    result = runner.compile(_mission(), _pm(), _cto())

    assert result.brief.pm_confirmation is not None
    assert result.brief.cto_confirmation is not None
    assert result.crew.mission_lead_id == "Backend_Service_Engineer"
    assert {item.identity_id for item in result.crew.members} == set(result.brief.proposed_crew)
    assert any(
        ref.startswith("crewai-output:") for ref in result.brief.pm_confirmation.evidence_references
    )


def test_proposal_runs_bounded_pm_then_cto_crewai_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CrewAIMissionGovernanceRunner(_config(tmp_path))
    calls: list[tuple[str, str, type[object]]] = []
    monkeypatch.setattr(
        runner._models,
        "candidates_for",
        lambda route_name: (SimpleNamespace(route_name=route_name),),
    )

    def crew_result(  # type: ignore[no-untyped-def]
        role, llm, _description, _expected_output, output_model
    ):
        calls.append((role.name, llm.route_name, output_model))
        return SimpleNamespace(pydantic=_pm() if role.name == "PM" else _cto(), raw="")

    monkeypatch.setattr(runner, "_crew", crew_result)

    result = runner.propose(
        _mission(),
        ({"reference": "evidence:repository", "fact": "Account recovery is absent"},),
    )

    assert result.disposition == "agreed"
    assert [identity for identity, _route, _model in calls] == ["PM", "CTO"]
    assert calls[0][1] == runner._config.crewai.mission_pm_model_route
    assert calls[1][1] == runner._config.crewai.mission_cto_model_route
    assert calls[0][2] is PMMissionProposal
    assert calls[1][2] is CTOMissionReview


def test_cto_rejection_compiles_to_actionable_disagreement_without_a_crew(
    tmp_path: Path,
) -> None:
    runner = CrewAIMissionGovernanceRunner(_config(tmp_path))

    result = runner.compile(_mission(), _pm(), _cto("rejected"))

    assert result.disposition == "disagreement"
    assert result.brief.status.value == "rejected"
    assert result.crew is None
    assert result.disagreement is not None
    assert result.disagreement.blocked_scope == ("task:security-design",)
    assert result.disagreement.independent_work_continuing == ("task:documentation",)
    assert {item.identity_id for item in result.disagreement.recommendations} == {"PM", "CTO"}
    escalation = result.escalation(uuid4())
    assert escalation.blocked_scope == ("task:security-design",)
    assert escalation.independent_work_continuing == ("task:documentation",)
    assert escalation.raised_by == "PM+CTO"
    assert any(item.startswith("crewai-output:") for item in escalation.evidence_references)


def test_cto_cannot_silently_replace_the_pm_confirmed_composition(tmp_path: Path) -> None:
    runner = CrewAIMissionGovernanceRunner(_config(tmp_path))
    changed = _cto().model_copy(
        update={
            "approved_members": (
                _member("Backend_Service_Engineer", CrewAssignmentKind.PRODUCTION),
                _member("Software_Technical_Evaluator", CrewAssignmentKind.EVALUATION),
            )
        }
    )

    with pytest.raises(MishkanError) as error:
        runner.compile(_mission(), _pm(), changed)
    assert error.value.envelope.code is ErrorCode.MISSION
