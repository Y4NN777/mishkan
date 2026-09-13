from pathlib import Path
from typing import Literal

import pytest

from mishkan.config.loader import ConfigLoader
from mishkan.config.models import MishkanConfig
from mishkan.config.presets import preset_text
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
        unresolved_findings=() if disposition == "confirmed" else ("risk unresolved",),
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


def test_cto_rejection_cannot_be_compiled_as_executive_agreement(tmp_path: Path) -> None:
    runner = CrewAIMissionGovernanceRunner(_config(tmp_path))

    with pytest.raises(MishkanError) as error:
        runner.compile(_mission(), _pm(), _cto("rejected"))
    assert error.value.envelope.code is ErrorCode.MISSION


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
