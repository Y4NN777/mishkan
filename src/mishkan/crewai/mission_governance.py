"""CrewAI PM/CTO proposal boundary with deterministic MISHKAN compilation."""

from __future__ import annotations

import hashlib
import json
from typing import Literal, TypeVar, cast

from crewai import LLM, Agent, Crew, Process, Task
from crewai.crews.crew_output import CrewOutput
from pydantic import BaseModel, ConfigDict, Field

from mishkan.config.models import MishkanConfig
from mishkan.crewai.routing import CrewAIModelRouter
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.missions import (
    ExecutiveConfirmation,
    MissionBrief,
    MissionBriefStatus,
    MissionCrewMember,
    MissionCrewRevision,
    MissionEnvironmentIntent,
    MissionRecord,
)
from mishkan.organization import OrganizationRosterDefinition, load_canonical_organization
from mishkan.organization.models import RoleDefinition

OutputT = TypeVar("OutputT", bound=BaseModel)


class GovernanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PMMissionProposal(GovernanceOutput):
    problem: str = Field(min_length=3, max_length=8_192)
    desired_outcome: str = Field(min_length=3, max_length=8_192)
    scope: tuple[str, ...] = Field(min_length=1)
    exclusions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...] = Field(min_length=1)
    constraints: tuple[str, ...]
    risks: tuple[str, ...]
    authority_scope: tuple[str, ...] = Field(min_length=1)
    proposed_identity_ids: tuple[str, ...] = Field(min_length=2)
    evidence_requirements: tuple[str, ...] = Field(min_length=1)
    escalation_conditions: tuple[str, ...] = Field(min_length=1)
    environment_intent: MissionEnvironmentIntent
    rationale: str = Field(min_length=3, max_length=4_096)
    evidence_references: tuple[str, ...] = Field(min_length=1)


class CTOMissionReview(GovernanceOutput):
    disposition: Literal["confirmed", "rejected"]
    rationale: str = Field(min_length=3, max_length=4_096)
    evidence_references: tuple[str, ...] = Field(min_length=1)
    coverage: tuple[str, ...] = Field(min_length=1)
    mission_lead_id: str = Field(min_length=2, max_length=128)
    approved_members: tuple[MissionCrewMember, ...] = Field(min_length=2)
    unresolved_findings: tuple[str, ...]


class MissionGovernanceResult(GovernanceOutput):
    mission: MissionRecord
    brief: MissionBrief
    crew: MissionCrewRevision
    pm_output_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cto_output_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class CrewAIMissionGovernanceRunner:
    """Run attributable PM then CTO work through CrewAI's production runtime."""

    def __init__(
        self,
        config: MishkanConfig,
        organization: OrganizationRosterDefinition | None = None,
    ) -> None:
        self._config = config
        self._models = CrewAIModelRouter(config)
        self._organization = organization or load_canonical_organization()

    def propose(
        self, mission: MissionRecord, evidence: tuple[dict[str, object], ...]
    ) -> MissionGovernanceResult:
        pm = self._kickoff_structured(
            identity_id="PM",
            route_name=self._config.crewai.mission_pm_model_route,
            description=self._pm_prompt(mission, evidence),
            expected_output="One complete product Mission Brief proposal grounded in the evidence.",
            output_model=PMMissionProposal,
        )
        cto = self._kickoff_structured(
            identity_id="CTO",
            route_name=self._config.crewai.mission_cto_model_route,
            description=self._cto_prompt(mission, pm, evidence),
            expected_output="One technical, security, and quality coverage decision.",
            output_model=CTOMissionReview,
        )
        return self.compile(mission, pm, cto)

    def compile(
        self,
        mission: MissionRecord,
        pm: PMMissionProposal,
        cto: CTOMissionReview,
    ) -> MissionGovernanceResult:
        if cto.disposition != "confirmed":
            raise MishkanError(
                ErrorCode.MISSION,
                "CTO rejected the proposed mission coverage",
                details={"unresolved_findings": list(cto.unresolved_findings)},
            )
        proposed = tuple(pm.proposed_identity_ids)
        approved = tuple(member.identity_id for member in cto.approved_members)
        if len(proposed) != len(set(proposed)) or set(proposed) != set(approved):
            raise MishkanError(
                ErrorCode.MISSION,
                "PM and CTO did not confirm the same Mission Crew composition",
            )
        known = {item.identity_id for item in self._organization.identities}
        if unknown := set(proposed) - known:
            raise MishkanError(
                ErrorCode.MISSION,
                "mission proposal references unknown professional identities",
                details={"unknown": sorted(unknown)},
            )
        pm_fingerprint = self._fingerprint(pm)
        cto_fingerprint = self._fingerprint(cto)
        pm_confirmation = ExecutiveConfirmation(
            identity_id="PM",
            disposition="confirmed",
            rationale=pm.rationale,
            evidence_references=(*pm.evidence_references, f"crewai-output:{pm_fingerprint}"),
            coverage=("product", "developer-experience", "composition"),
        )
        cto_confirmation = ExecutiveConfirmation(
            identity_id="CTO",
            disposition="confirmed",
            rationale=cto.rationale,
            evidence_references=(*cto.evidence_references, f"crewai-output:{cto_fingerprint}"),
            coverage=cto.coverage,
        )
        brief = MissionBrief(
            mission_id=mission.mission_id,
            version=(mission.current_brief_version or 0) + 1,
            organization_id=mission.organization_id,
            organization_version=mission.organization_version,
            status=MissionBriefStatus.CONFIRMED,
            objective=mission.origin.objective,
            problem=pm.problem,
            desired_outcome=pm.desired_outcome,
            scope=pm.scope,
            exclusions=pm.exclusions,
            acceptance_criteria=pm.acceptance_criteria,
            constraints=pm.constraints,
            risks=pm.risks,
            authority_scope=pm.authority_scope,
            proposed_crew=proposed,
            evidence_requirements=pm.evidence_requirements,
            escalation_conditions=pm.escalation_conditions,
            environment_intent=pm.environment_intent,
            pm_confirmation=pm_confirmation,
            cto_confirmation=cto_confirmation,
        )
        crew = MissionCrewRevision(
            mission_id=mission.mission_id,
            version=(mission.current_crew_version or 0) + 1,
            organization_id=mission.organization_id,
            organization_version=mission.organization_version,
            brief_version=brief.version,
            mission_lead_id=cto.mission_lead_id,
            members=cto.approved_members,
            pm_composition_confirmation_id=pm_confirmation.confirmation_id,
            cto_coverage_confirmation_id=cto_confirmation.confirmation_id,
            revision_reason="PM/CTO CrewAI proposal compiled from attributable evidence",
        )
        return MissionGovernanceResult(
            mission=mission,
            brief=brief,
            crew=crew,
            pm_output_fingerprint=pm_fingerprint,
            cto_output_fingerprint=cto_fingerprint,
        )

    def _kickoff_structured(
        self,
        *,
        identity_id: str,
        route_name: str,
        description: str,
        expected_output: str,
        output_model: type[OutputT],
    ) -> OutputT:
        failures: list[str] = []
        role = self._role(identity_id, route_name)
        for llm in self._models.candidates_for(route_name):
            prompt = description
            for _attempt in range(self._config.crewai.structured_output_retries + 1):
                try:
                    output = self._crew(role, llm, prompt, expected_output, output_model)
                    if isinstance(output.pydantic, output_model):
                        return output.pydantic
                    return output_model.model_validate_json(output.raw)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    prompt = description + "\nReturn one complete value matching the JSON Schema."
        raise MishkanError(
            ErrorCode.REQUIRED_DEPENDENCY,
            "all configured CrewAI mission-governance candidates failed",
            details={"identity_id": identity_id, "failure_types": failures},
            retryable=True,
        )

    def _crew(
        self,
        role: RoleDefinition,
        llm: LLM,
        description: str,
        expected_output: str,
        output_model: type[OutputT],
    ) -> CrewOutput:
        agent = Agent(
            role=role.name,
            goal=role.goal,
            backstory=role.backstory,
            llm=llm,
            tools=[],
            allow_delegation=False,
            allow_code_execution=False,
            max_iter=self._config.crewai.max_agent_iterations,
            max_retry_limit=0,
            verbose=False,
        )
        task = Task(
            description=description,
            expected_output=expected_output,
            agent=agent,
            tools=[],
            output_pydantic=output_model,
        )
        return cast(
            CrewOutput,
            Crew(
                agents=[agent],
                tasks=[task],
                process=Process.sequential,
                tracing=self._config.crewai.tracing,
                verbose=False,
            ).kickoff(),
        )

    def _role(self, identity_id: str, route_name: str) -> RoleDefinition:
        identity = next(
            (item for item in self._organization.identities if item.identity_id == identity_id),
            None,
        )
        if identity is None:
            raise MishkanError(ErrorCode.ROLE_CONFLICT, "required executive identity is absent")
        return RoleDefinition(
            name=identity.identity_id,
            goal=identity.responsibility,
            backstory=(
                f"Persistent {identity.identity_id} professional identity in organization "
                f"{self._organization.organization_id}@{self._organization.organization_version}."
            ),
            model_route=route_name,
        )

    def _pm_prompt(self, mission: MissionRecord, evidence: tuple[dict[str, object], ...]) -> str:
        return (
            "Produce the product Mission Brief proposal for this mission. Do not invent evidence, "
            "authority, tools, or a fixed workflow. Select identities only from the supplied "
            "roster.\n"
            f"Mission: {mission.model_dump_json()}\n"
            f"Roster: {self._roster_projection()}\nEvidence: {json.dumps(evidence, sort_keys=True)}"
        )

    def _cto_prompt(
        self,
        mission: MissionRecord,
        pm: PMMissionProposal,
        evidence: tuple[dict[str, object], ...],
    ) -> str:
        return (
            "Independently confirm or reject technical, security, quality, operability, and "
            "independence coverage. Keep exactly the PM-proposed identities when confirming; "
            "otherwise reject with findings. Do not grant tools or authority.\n"
            f"Mission: {mission.model_dump_json()}\nPM proposal: {pm.model_dump_json()}\n"
            f"Roster: {self._roster_projection()}\nEvidence: {json.dumps(evidence, sort_keys=True)}"
        )

    def _roster_projection(self) -> str:
        return json.dumps(
            [
                {
                    "identity_id": item.identity_id,
                    "responsibility": item.responsibility,
                    "independence_class": item.independence_class.value,
                    "mission_lead_eligible": item.mission_lead_eligible,
                }
                for item in self._organization.identities
            ],
            sort_keys=True,
        )

    @staticmethod
    def _fingerprint(value: BaseModel) -> str:
        return hashlib.sha256(value.model_dump_json().encode()).hexdigest()
