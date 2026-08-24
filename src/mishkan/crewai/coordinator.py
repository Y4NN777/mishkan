"""CrewAI agents, tasks, crews, and processes for the I01 outcome."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TypeVar, cast

from crewai import LLM, Agent, Crew, Process, Task
from crewai.crews.crew_output import CrewOutput
from crewai.tools import BaseTool
from pydantic import BaseModel

from mishkan.config.models import MishkanConfig
from mishkan.crewai.routing import CrewAIModelRouter
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition, RoleDefinition
from mishkan.planning.models import (
    AcceptedPlan,
    InitializationResult,
    PlanCandidate,
    PlanTask,
    ReviewDecision,
)
from mishkan.policy import EffectivePolicy
from mishkan.repository.models import DiscoverySnapshot
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.gateway import CapabilityGateway
from mishkan.tools.gateway_models import InvocationContext

OutputT = TypeVar("OutputT", bound=BaseModel)


class CrewAIInitializationCoordinator:
    """Production coordinator; there is intentionally no runtime selector."""

    def __init__(
        self,
        config: MishkanConfig,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
        gateway: CapabilityGateway | None = None,
        policy: EffectivePolicy | None = None,
    ) -> None:
        self._config = config
        self._organization = organization
        self._outcome = outcome
        self._gateway = gateway
        self._policy = policy
        self._models = CrewAIModelRouter(config)

    @property
    def review_retries(self) -> int:
        return self._config.crewai.review_retries

    @property
    def plan_validation_retries(self) -> int:
        return self._config.crewai.plan_validation_retries

    def propose_plan(
        self,
        discovery: DiscoverySnapshot,
        objective: str,
        validation_feedback: tuple[str, ...] = (),
    ) -> PlanCandidate:
        role = self._role("Repository_Planner")
        evidence = [fact.model_dump(mode="json") for fact in discovery.facts]
        cited_paths = sorted(path.as_posix() for path in discovery.cited_paths)
        feedback = ""
        if validation_feedback:
            feedback = f"""
A previous candidate was deterministically refused for these violations:
{json.dumps(validation_feedback)}
Correct every violation. Do not repeat the refused role, tool, path, or dependency values.
""".strip()
        description = f"""
Propose exactly one read-only task for this repository initialization outcome.

Objective: {objective}
Outcome ID: {self._outcome.outcome_id}
Repository revision: {discovery.binding.base_revision}
Outcome intent: {self._outcome.intent}
Discovery fingerprint: {discovery.fingerprint}
Discovery facts: {json.dumps(evidence, sort_keys=True)}
Unknowns: {json.dumps(discovery.unknowns)}

Rules:
- Return the exact objective, outcome ID, and repository revision above.
- Create exactly one task whose ID, title, and purpose are specific to the evidence.
- assigned_role must be Repository_Investigator.
- tools must be ["repository.read_file"].
- evidence_paths must contain one or more values from: {json.dumps(cited_paths)}.
- depends_on must be empty.
- Never invent a path or use a universal task title.

{feedback}
""".strip()
        return self._kickoff_structured(
            route_name=role.model_route,
            role=role,
            description=description,
            expected_output=(
                "One valid PlanCandidate grounded only in the supplied discovery facts."
            ),
            output_model=PlanCandidate,
            tools=[],
        )

    def execute_task(
        self,
        run_id: str,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task_contract: PlanTask,
        review_feedback: ReviewDecision | None = None,
    ) -> InitializationResult:
        role = self._role(task_contract.assigned_role)
        tools = self._governed_tools(
            run_id,
            plan,
            discovery,
            task_contract.task_id,
            role.name,
            task_contract.tools,
        )
        feedback = ""
        if review_feedback is not None:
            feedback = f"""
A previous independent review rejected an earlier attempt.
Review summary: {review_feedback.summary}
Issues to correct: {json.dumps(review_feedback.issues)}
Re-read the evidence and do not repeat unsupported claims.
""".strip()
        description = f"""
Execute this accepted read-only repository task:
{task_contract.model_dump_json()}

Repository revision: {discovery.binding.base_revision}
You MUST call repository_read_file for every evidence path before answering.
Return task_id exactly as {task_contract.task_id!r} and repository_revision exactly as
{discovery.binding.base_revision!r}. Cite only the bound evidence paths. Report at least one
concrete finding grounded in the file content. Do not modify anything.

{feedback}
""".strip()
        return self._kickoff_structured(
            route_name=role.model_route,
            role=role,
            description=description,
            expected_output="One valid InitializationResult with cited, file-grounded findings.",
            output_model=InitializationResult,
            tools=tools,
        )

    def review_task(
        self,
        run_id: str,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task_contract: PlanTask,
        result: InitializationResult,
    ) -> ReviewDecision:
        role_name = self._outcome.review_roles[0]
        role = self._role(role_name)
        tools = self._governed_tools(
            run_id,
            plan,
            discovery,
            f"review-{task_contract.task_id}",
            role.name,
            task_contract.tools,
        )
        description = f"""
Independently review this result against its accepted task and repository evidence.

Accepted task: {task_contract.model_dump_json()}
Proposed result: {result.model_dump_json()}

You MUST call repository_read_file for every cited path. Return task_id exactly as
{task_contract.task_id!r}. checked_citations must include every result citation. Set verdict to
accepted only when the findings are supported by the file content and the result obeys the task;
otherwise set verdict to rejected and list concrete issues. You are reviewing another role's work.
""".strip()
        return self._kickoff_structured(
            route_name=role.model_route,
            role=role,
            description=description,
            expected_output="One independent ReviewDecision grounded in the bound files.",
            output_model=ReviewDecision,
            tools=tools,
        )

    def _governed_tools(
        self,
        run_id: str,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        binding_task_id: str,
        role: str,
        tool_ids: tuple[str, ...],
    ) -> list[BaseTool]:
        registry = plan.registry
        if (
            self._gateway is None
            or self._policy is None
            or registry is None
            or plan.policy_fingerprint != self._policy.fingerprint
        ):
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "accepted plan does not match the active policy and registry lineage",
            )
        tools: list[BaseTool] = []
        for tool_id in tool_ids:
            contract = registry.require(tool_id)
            binding = plan.binding_for(binding_task_id, role, tool_id)
            context = InvocationContext(
                run_id=run_id,
                task_attempt_id=f"{binding_task_id}:1",
                identity=f"role:{role}",
                objective_class=self._outcome.objective_class,
                repository=discovery.binding.repository_id,
                outcome=self._outcome.outcome_id,
                role=role,
                plan_fingerprint=plan.fingerprint,
                registry=registry,
                binding=binding,
                policy=self._policy,
                resources=contract.resources,
            )
            tools.append(
                GatewayCrewAITool(
                    contract,
                    self._gateway,
                    context,
                    approval=plan.approvals,
                )
            )
        return tools

    def _kickoff_structured(
        self,
        *,
        route_name: str,
        role: RoleDefinition,
        description: str,
        expected_output: str,
        output_model: type[OutputT],
        tools: list[BaseTool],
    ) -> OutputT:
        failures: list[str] = []
        for llm in self._models.candidates_for(route_name):
            retry_description = description
            attempts = self._config.crewai.structured_output_retries + 1
            for _attempt in range(attempts):
                try:
                    output = self._crew(
                        role,
                        llm,
                        retry_description,
                        expected_output,
                        output_model,
                        tools,
                    )
                    if isinstance(output.pydantic, output_model):
                        return output.pydantic
                    return output_model.model_validate_json(output.raw)
                except Exception as exc:
                    failures.append(type(exc).__name__)
                    retry_description = f"""
{description}

A previous attempt did not satisfy the required {output_model.__name__} JSON Schema.
Return only a complete value matching the declared structured output. Do not omit required fields.
""".strip()
        raise MishkanError(
            ErrorCode.REQUIRED_DEPENDENCY,
            "all configured CrewAI model candidates failed",
            details={"route": route_name, "failures": failures},
            retryable=True,
        )

    def _crew(
        self,
        role: RoleDefinition,
        llm: LLM,
        description: str,
        expected_output: str,
        output_model: type[OutputT],
        tools: list[BaseTool],
    ) -> CrewOutput:
        agent = Agent(
            role=role.name,
            goal=role.goal,
            backstory=role.backstory,
            llm=llm,
            tools=tools,
            allow_delegation=False,
            allow_code_execution=False,
            max_iter=self._config.crewai.max_agent_iterations,
            verbose=False,
        )
        task = Task(
            description=description,
            expected_output=expected_output,
            agent=agent,
            tools=tools,
            output_pydantic=output_model,
        )
        crew = Crew(
            agents=[agent],
            tasks=[task],
            process=Process.sequential,
            tracing=self._config.crewai.tracing,
            verbose=False,
        )
        return cast(CrewOutput, crew.kickoff())

    def _role(self, name: str) -> RoleDefinition:
        matches: Iterable[RoleDefinition] = (
            role for role in self._organization.roles if role.name == name
        )
        role_list = list(matches)
        if len(role_list) != 1:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "organization does not define exactly one required role",
                details={"role": name},
            )
        return role_list[0]
