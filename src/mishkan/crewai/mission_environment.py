"""Bounded CrewAI planning for mission execution environments."""

from __future__ import annotations

import hashlib
import json
from typing import Protocol, TypeVar, cast

from crewai import LLM, Agent, Crew, Process, Task
from crewai.crews.crew_output import CrewOutput
from pydantic import BaseModel, ConfigDict, Field

from mishkan.config.models import MishkanConfig
from mishkan.crewai.routing import CrewAIModelRouter
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.environment import EnvironmentObservation
from mishkan.missions import MissionBrief, MissionCrewRevision, MissionRecord
from mishkan.missions.environment import (
    CrewAIPlanningLineage,
    MissionEnvironmentDecision,
    MissionEnvironmentPlan,
    MissionEnvironmentPlanningRequest,
)
from mishkan.organization import OrganizationRosterDefinition, load_canonical_organization
from mishkan.organization.models import RoleDefinition

OutputT = TypeVar("OutputT", bound=BaseModel)


class PlanningOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionEnvironmentDecisionOutput(PlanningOutput):
    context_id: str = Field(min_length=1, max_length=256)
    selected_alternative_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    requested_outcome: str = Field(min_length=1, max_length=64)
    rationale: str = Field(min_length=3, max_length=8_192)
    constraints: tuple[str, ...]
    declared_effects: tuple[str, ...]
    verification_checks: tuple[str, ...] = Field(min_length=1)
    cleanup_criteria: tuple[str, ...] = Field(min_length=1)
    alternatives_considered: tuple[str, ...] = Field(min_length=1)
    unknowns: tuple[str, ...]
    evidence_references: tuple[str, ...] = Field(min_length=1)


class MissionEnvironmentPlanningOutput(PlanningOutput):
    decisions: tuple[MissionEnvironmentDecisionOutput, ...] = Field(min_length=1, max_length=64)


class MissionEnvironmentPlanningRunner(Protocol):
    def propose(
        self,
        request: MissionEnvironmentPlanningRequest,
        *,
        mission: MissionRecord,
        brief: MissionBrief,
        crew: MissionCrewRevision,
        observations: tuple[EnvironmentObservation, ...],
        plan_version: int,
    ) -> MissionEnvironmentPlan: ...


class CrewAIMissionEnvironmentPlanningRunner:
    """Let the assigned professional choose; compile exact resolver constraints."""

    def __init__(
        self,
        config: MishkanConfig,
        organization: OrganizationRosterDefinition | None = None,
    ) -> None:
        self._config = config
        self._models = CrewAIModelRouter(config)
        self._organization = organization or load_canonical_organization()

    def propose(
        self,
        request: MissionEnvironmentPlanningRequest,
        *,
        mission: MissionRecord,
        brief: MissionBrief,
        crew: MissionCrewRevision,
        observations: tuple[EnvironmentObservation, ...],
        plan_version: int,
    ) -> MissionEnvironmentPlan:
        route = self._config.crewai.mission_environment_model_route
        output = self._kickoff_structured(
            role=self._role(request.owner_identity, route),
            route_name=route,
            description=self._prompt(request, mission, brief, crew, observations),
        )
        return self.compile(request, output, plan_version=plan_version, model_route=route)

    @staticmethod
    def compile(
        request: MissionEnvironmentPlanningRequest,
        output: MissionEnvironmentPlanningOutput,
        *,
        plan_version: int,
        model_route: str,
    ) -> MissionEnvironmentPlan:
        by_context = {item.context_id: item for item in request.contexts}
        outputs = {item.context_id: item for item in output.decisions}
        if len(outputs) != len(output.decisions) or set(outputs) != set(by_context):
            raise MishkanError(
                ErrorCode.PLAN,
                "CrewAI environment output must decide every requested context exactly once",
            )
        decisions: list[MissionEnvironmentDecision] = []
        for context in request.contexts:
            decision = outputs[context.context_id]
            alternatives = {item.alternative_id: item for item in context.alternatives}
            selected = alternatives.get(decision.selected_alternative_id)
            if selected is None or decision.requested_outcome != selected.requested_outcome.value:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "CrewAI environment output selected an unavailable alternative",
                )
            if set(decision.alternatives_considered) != set(alternatives):
                raise MishkanError(
                    ErrorCode.PLAN,
                    "CrewAI environment output did not compare every exposed alternative",
                )
            allowed_evidence = {
                *request.evidence_references,
                *context.evidence_references,
                *selected.evidence_references,
            }
            if set(decision.evidence_references) - allowed_evidence:
                raise MishkanError(
                    ErrorCode.PLAN,
                    "CrewAI environment output invented an evidence reference",
                )
            decisions.append(
                MissionEnvironmentDecision(
                    context_id=context.context_id,
                    selected_alternative_id=selected.alternative_id,
                    requested_outcome=selected.requested_outcome,
                    rationale=decision.rationale,
                    constraints=(*selected.constraints, *decision.constraints),
                    declared_effects=decision.declared_effects,
                    verification_checks=decision.verification_checks,
                    cleanup_criteria=decision.cleanup_criteria,
                    alternatives_considered=decision.alternatives_considered,
                    unknowns=decision.unknowns,
                    evidence_references=decision.evidence_references,
                    required_semantics=selected.required_semantics,
                    allowed_descriptor_formats=selected.allowed_descriptor_formats,
                    required_engine_ids=selected.required_engine_ids,
                    eligible_engine_ids=selected.eligible_engine_ids,
                    requires_consequential_decision=(selected.requires_consequential_decision),
                    consequential_decision_id=selected.consequential_decision_id,
                    consequential_option_id=selected.consequential_option_id,
                )
            )
        fingerprint = hashlib.sha256(output.model_dump_json().encode()).hexdigest()
        return MissionEnvironmentPlan(
            mission_id=request.mission_id,
            version=plan_version,
            mission_revision=request.mission_revision,
            brief_version=request.brief_version,
            crew_version=request.crew_version,
            source_request_id=request.request_id,
            planning_task_id=request.planning_task_id,
            owner_identity=request.owner_identity,
            evidence_references=request.evidence_references,
            contexts=request.contexts,
            decisions=tuple(decisions),
            lineage=CrewAIPlanningLineage(
                model_route=model_route,
                output_fingerprint=fingerprint,
            ),
        )

    def _kickoff_structured(
        self,
        *,
        role: RoleDefinition,
        route_name: str,
        description: str,
    ) -> MissionEnvironmentPlanningOutput:
        failures: list[str] = []
        for llm in self._models.candidates_for(route_name):
            prompt = description
            for _attempt in range(self._config.crewai.structured_output_retries + 1):
                try:
                    output = self._crew(role, llm, prompt)
                    if isinstance(output.pydantic, MissionEnvironmentPlanningOutput):
                        return output.pydantic
                    return MissionEnvironmentPlanningOutput.model_validate_json(output.raw)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    prompt = description + "\nReturn one complete value matching the JSON Schema."
        raise MishkanError(
            ErrorCode.REQUIRED_DEPENDENCY,
            "all configured CrewAI environment-planning candidates failed",
            details={"owner_identity": role.name, "failure_types": failures},
            retryable=True,
        )

    def _crew(self, role: RoleDefinition, llm: LLM, description: str) -> CrewOutput:
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
            expected_output=(
                "One decision per supplied context selecting exactly one exposed alternative, "
                "with attributable rationale, effects, verification, cleanup, and unknowns."
            ),
            agent=agent,
            tools=[],
            output_pydantic=MissionEnvironmentPlanningOutput,
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
            raise MishkanError(ErrorCode.ROLE_CONFLICT, "environment plan owner is unknown")
        return RoleDefinition(
            name=identity.identity_id,
            goal=identity.responsibility,
            backstory=(
                f"Persistent {identity.identity_id} professional identity in organization "
                f"{self._organization.organization_id}@{self._organization.organization_version}."
            ),
            model_route=route_name,
        )

    @staticmethod
    def _prompt(
        request: MissionEnvironmentPlanningRequest,
        mission: MissionRecord,
        brief: MissionBrief,
        crew: MissionCrewRevision,
        observations: tuple[EnvironmentObservation, ...],
    ) -> str:
        observation_json = json.dumps(
            [item.model_dump(mode="json") for item in observations], sort_keys=True
        )
        return (
            "Choose one supplied environment alternative for every context. Compare all exposed "
            "alternatives. Do not invent evidence, engines, formats, authority, or readiness. "
            "This is a proposal only: do not execute tools or mutate the project.\n"
            f"Mission: {mission.model_dump_json()}\nBrief: {brief.model_dump_json()}\n"
            f"Crew: {crew.model_dump_json()}\nRequest: {request.model_dump_json()}\n"
            f"Observations: {observation_json}"
        )
