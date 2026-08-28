"""CrewAI agents, tasks, crews, and processes for repository initialization."""

from __future__ import annotations

import json
from collections.abc import Iterable
from fnmatch import fnmatchcase
from functools import lru_cache
from typing import TypeVar, cast

from crewai import LLM, Agent, Crew, Process, Task
from crewai.crews.crew_output import CrewOutput
from crewai.tools import BaseTool
from pydantic import BaseModel, Field, ValidationError, create_model

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
from mishkan.policy import Decision, EffectivePolicy
from mishkan.repository.models import DiscoverySnapshot
from mishkan.tools.crewai_gateway import GatewayCrewAITool
from mishkan.tools.gateway import CapabilityGateway
from mishkan.tools.gateway_models import InvocationContext
from mishkan.tools.models import EffectClass, ToolContract
from mishkan.tools.native import ExecutableObservation

OutputT = TypeVar("OutputT", bound=BaseModel)


@lru_cache(maxsize=12)
def _bounded_plan_candidate_model(max_tasks: int) -> type[PlanCandidate]:
    """Derive the provider-visible output schema from the public outcome bound."""

    return create_model(
        f"PlanCandidateMax{max_tasks}",
        __base__=PlanCandidate,
        tasks=(
            tuple[PlanTask, ...],
            Field(min_length=1, max_length=max_tasks),
        ),
    )


class CrewAIInitializationCoordinator:
    """Production coordinator; there is intentionally no runtime selector."""

    def __init__(
        self,
        config: MishkanConfig,
        organization: OrganizationDefinition,
        outcome: OutcomeDefinition,
        gateway: CapabilityGateway | None = None,
        policy: EffectivePolicy | None = None,
        *,
        available_tools: tuple[ToolContract, ...] = (),
        available_executables: tuple[ExecutableObservation, ...] = (),
    ) -> None:
        self._config = config
        self._organization = organization
        self._outcome = outcome
        self._gateway = gateway
        self._policy = policy
        self._available_tools = available_tools
        self._available_executables = available_executables
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
        tool_contracts = [
            {
                "tool_id": contract.tool_id,
                "crewai_name": contract.crewai_name,
                "summary": contract.summary,
                "effect_class": contract.effect_class.value,
                "input_schema": contract.input_schema,
                "resources": contract.resources.model_dump(mode="json"),
            }
            for contract in self._available_tools
        ]
        task_role = self._required_task_role()
        command_policy_hints, executable_selectors = self._command_policy_context(task_role.name)
        executables = [
            {"name": executable.name, "path": executable.path}
            for executable in self._available_executables
            if any(fnmatchcase(executable.path, selector) for selector in executable_selectors)
        ]
        allowed_command_tools = tuple(
            dict.fromkeys(
                capability
                for hint in command_policy_hints
                for capability in cast(tuple[str, ...], hint["capabilities"])
            )
        )
        command_rule = (
            "- Include exactly one unattended command tool from: "
            f"{json.dumps(allowed_command_tools)}."
            if allowed_command_tools
            else "- Do not select a command tool because public policy permits none unattended."
        )
        description = f"""
Propose a bounded repository-specific plan for this initialization outcome.

Objective: {objective}
Outcome ID: {self._outcome.outcome_id}
Repository revision: {discovery.binding.base_revision}
Outcome intent: {self._outcome.intent}
Discovery fingerprint: {discovery.fingerprint}
Discovery facts: {json.dumps(evidence, sort_keys=True)}
Unknowns: {json.dumps(discovery.unknowns)}
Runnable tool contracts at this location: {json.dumps(tool_contracts, sort_keys=True)}
Observed executables eligible under unattended public policy:
{json.dumps(executables, sort_keys=True)}
Unattended public command-policy allow rules:
{json.dumps(command_policy_hints, sort_keys=True)}

Rules:
- Return schema_version "1.1".
- Return the exact objective, outcome ID, and repository revision above.
- Create at most {self._outcome.max_tasks} task(s); use the smallest sufficient plan and make every
  task ID, title, and purpose specific to the evidence.
- assigned_role must be {task_role.name}.
- Select exact tools only from the runnable contracts above.
- Include repository.read_file.
{command_rule}
- tool_calls must contain at least one exact call for every selected tool.
- Give every tool call a unique repository-specific call_id.
- Arguments must match the selected tool input schema exactly, including required fields.
- A task may contain at most {self._config.crewai.max_agent_iterations - 1} exact calls so the
  configured CrewAI iteration bound still permits a final structured result.
- Read every cited evidence path through repository.read_file.
- For process mode, use an absolute executable from the observed inventory and literal arguments
  useful for the detected project rather than a universal probe.
- For shell mode, use exact observed paths for the interpreter and declared executables.
- Select command executable/script and arguments only when they match an unattended public
  command-policy allow rule above. Availability alone does not grant authority.
- Keep the probe non-mutating: use empty declared effects, credentials, environment additions,
  network destinations, and stdin unless the objective explicitly requires them.
- Use cwd ".", honor the selected contract's resource limits, choose appropriate expected exit
  codes and bounded previews, and request no complete-output artifact unless it is necessary.
- evidence_paths must contain one or more values from: {json.dumps(cited_paths)}.
- depends_on must be empty.
- Never invent a path, executable, tool, or universal task title.

{feedback}
""".strip()
        return self._kickoff_structured(
            route_name=role.model_route,
            role=role,
            description=description,
            expected_output=(
                "One valid PlanCandidate grounded only in the supplied discovery facts."
            ),
            output_model=_bounded_plan_candidate_model(self._outcome.max_tasks),
            tools=[],
        )

    def _required_task_role(self) -> RoleDefinition:
        if len(self._outcome.task_roles) != 1:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "initialization outcome does not define exactly one task role",
            )
        return self._role(self._outcome.task_roles[0])

    def execute_task_evidence(
        self,
        run_id: str,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task_contract: PlanTask,
    ) -> str:
        role = self._role(task_contract.assigned_role)
        tools = self._governed_tools(
            run_id,
            plan,
            discovery,
            task_contract.task_id,
            role.name,
            task_contract.tools,
        )
        return json.dumps(
            self._execute_planned_calls(
                plan,
                task_contract,
                tools,
                task_contract.tools,
            ),
            sort_keys=True,
        )

    def execute_task(
        self,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task_contract: PlanTask,
        call_evidence: str,
        review_feedback: ReviewDecision | None = None,
    ) -> InitializationResult:
        role = self._role(task_contract.assigned_role)
        feedback = ""
        if review_feedback is not None:
            feedback = f"""
A previous independent review rejected an earlier attempt.
Review summary: {review_feedback.summary}
Issues to correct: {json.dumps(review_feedback.issues)}
Use the supplied immutable evidence and do not repeat unsupported claims.
""".strip()
        description = f"""
Execute this accepted bounded repository task:
{task_contract.model_dump_json()}

Repository revision: {discovery.binding.base_revision}
MISHKAN has already executed every accepted call exactly once through the governed capability
gateway. The immutable call evidence is: {call_evidence}
No capability is exposed during synthesis. Use only that evidence; never invent another observation
or claim that you executed a call yourself.
Return task_id exactly as {task_contract.task_id!r} and repository_revision exactly as
{discovery.binding.base_revision!r}. Cite only the bound evidence paths. Report at least one
concrete finding grounded in the file content and execution output. Do not modify anything.

{feedback}
""".strip()
        attempts = self._config.crewai.task_execution_retries + 1
        for attempt in range(attempts):
            try:
                return self._kickoff_structured(
                    route_name=role.model_route,
                    role=role,
                    description=description,
                    expected_output=(
                        "One valid InitializationResult with cited, file-grounded findings."
                    ),
                    output_model=InitializationResult,
                    tools=[],
                    retry_limit=0,
                )
            except MishkanError:
                if attempt + 1 >= attempts:
                    raise
        raise RuntimeError("task execution retry loop produced no result")

    def execute_review_evidence(
        self,
        run_id: str,
        plan: AcceptedPlan,
        discovery: DiscoverySnapshot,
        task_contract: PlanTask,
    ) -> str:
        role_name = self._outcome.review_roles[0]
        role = self._role(role_name)
        review_tool_ids = self._review_tool_ids(
            plan,
            task_contract.tools,
            role.allowed_tools,
        )
        tools = self._governed_tools(
            run_id,
            plan,
            discovery,
            f"review-{task_contract.task_id}",
            role.name,
            review_tool_ids,
        )
        return json.dumps(
            self._execute_planned_calls(
                plan,
                task_contract,
                tools,
                review_tool_ids,
            ),
            sort_keys=True,
        )

    def review_task(
        self,
        task_contract: PlanTask,
        result: InitializationResult,
        review_evidence: str,
        contract_feedback: tuple[str, ...] = (),
    ) -> ReviewDecision:
        role_name = self._outcome.review_roles[0]
        role = self._role(role_name)
        deterministic_feedback = (
            "\nA prior review envelope failed deterministic acceptance:\n"
            + "\n".join(f"- {item}" for item in contract_feedback)
            + "\nCorrect the review envelope without changing the evidence.\n"
            if contract_feedback
            else ""
        )
        description = f"""
Independently review this result against its accepted task and repository evidence.

Accepted task: {task_contract.model_dump_json()}
Proposed result: {result.model_dump_json()}

MISHKAN independently executed the review role's accepted read calls through a separate governed
binding. The immutable review evidence is: {review_evidence}
No capability is exposed during review synthesis. Do not claim another read or run Process or Bash.
Return task_id exactly as {task_contract.task_id!r}. checked_citations must include every result
citation. Set verdict to accepted only when the findings are supported by the file content and the
result obeys the task; otherwise set verdict to rejected and list concrete issues. You are reviewing
another role's work.

{deterministic_feedback}
""".strip()
        attempts = self._config.crewai.task_execution_retries + 1
        for attempt in range(attempts):
            try:
                return self._kickoff_structured(
                    route_name=role.model_route,
                    role=role,
                    description=description,
                    expected_output="One independent ReviewDecision grounded in the bound files.",
                    output_model=ReviewDecision,
                    tools=[],
                    retry_limit=0,
                )
            except MishkanError:
                if attempt + 1 >= attempts:
                    raise
        raise RuntimeError("review execution retry loop produced no result")

    def _execute_planned_calls(
        self,
        plan: AcceptedPlan,
        task: PlanTask,
        tools: list[BaseTool],
        tool_ids: tuple[str, ...],
    ) -> list[dict[str, object]]:
        if plan.registry is None:
            raise MishkanError(
                ErrorCode.TOOL_DRIFT,
                "accepted plan has no registry for deterministic call execution",
            )
        governed = {tool.name: tool for tool in tools if isinstance(tool, GatewayCrewAITool)}
        evidence: list[dict[str, object]] = []
        for call in task.tool_calls:
            if call.tool_id not in tool_ids:
                continue
            contract = plan.registry.require(call.tool_id)
            tool = governed.get(contract.crewai_name)
            if tool is None:
                raise MishkanError(
                    ErrorCode.TOOL_DRIFT,
                    "accepted call has no governed executable binding",
                    details={"tool_id": call.tool_id},
                )
            raw_output = tool.run(**call.arguments)
            if not isinstance(raw_output, str):
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "governed capability returned a non-JSON CrewAI envelope",
                    details={"tool_id": call.tool_id},
                )
            try:
                output = json.loads(raw_output)
            except json.JSONDecodeError as exc:
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "governed capability returned an invalid JSON CrewAI envelope",
                    details={"tool_id": call.tool_id},
                ) from exc
            evidence.append(
                {
                    "call_id": call.call_id,
                    "tool_id": call.tool_id,
                    "argument_fingerprint": call.argument_fingerprint,
                    "output": output,
                }
            )
        self._require_completed_calls(task, tools, tool_ids)
        return evidence

    @staticmethod
    def _review_tool_ids(
        plan: AcceptedPlan,
        tool_ids: tuple[str, ...],
        role_tools: tuple[str, ...],
    ) -> tuple[str, ...]:
        if plan.registry is None:
            return ()
        eligible = set(role_tools)
        return tuple(
            tool_id
            for tool_id in tool_ids
            if tool_id in eligible
            and plan.registry.require(tool_id).effect_class is EffectClass.READ
        )

    @staticmethod
    def _require_completed_calls(
        task: PlanTask,
        tools: list[BaseTool],
        tool_ids: tuple[str, ...],
    ) -> None:
        expected = {
            call.argument_fingerprint for call in task.tool_calls if call.tool_id in tool_ids
        }
        completed = {
            fingerprint
            for tool in tools
            if isinstance(tool, GatewayCrewAITool)
            for fingerprint in tool.completed_call_fingerprints
        }
        if completed != expected:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "governed execution did not complete every exact tool call in the accepted plan",
                details={
                    "missing_call_fingerprints": sorted(expected - completed),
                    "unexpected_call_fingerprints": sorted(completed - expected),
                },
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
        retry_limit: int | None = None,
    ) -> OutputT:
        failures: list[tuple[str, ...]] = []
        for llm in self._models.candidates_for(route_name):
            retry_description = description
            attempts = (
                self._config.crewai.structured_output_retries
                if retry_limit is None
                else retry_limit
            ) + 1
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
                    failures.append(self._exception_type_chain(exc))
                    validation_locations = self._validation_failure_locations(exc)
                    location_feedback = (
                        "\nValidation failed at these schema locations:\n"
                        + "\n".join(f"- {location}" for location in validation_locations)
                        if validation_locations
                        else ""
                    )
                    retry_description = f"""
{description}

A previous attempt did not satisfy the required {output_model.__name__} JSON Schema.
{location_feedback}
Return only a complete value matching the declared structured output. Do not omit required fields.
""".strip()
        last_failure = " <- ".join(failures[-1]) if failures else "unknown"
        raise MishkanError(
            ErrorCode.REQUIRED_DEPENDENCY,
            f"all configured CrewAI model candidates failed: {last_failure}",
            details={
                "route": route_name,
                "failure_type_chains": [list(chain) for chain in failures],
            },
            retryable=True,
        )

    @staticmethod
    def _exception_type_chain(exc: BaseException) -> tuple[str, ...]:
        """Retain actionable failure classes without leaking provider messages."""

        chain: list[str] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        return tuple(chain)

    def _command_policy_context(
        self,
        role: str,
    ) -> tuple[list[dict[str, object]], tuple[str, ...]]:
        if self._policy is None:
            return [], ()
        identity = f"role:{role}"
        command_tools = {"core.process.exec", "core.shell.run"}
        hints: list[dict[str, object]] = []
        selectors: list[str] = []
        for document in self._policy.documents:
            for rule in document.rules:
                scope = rule.scope
                capabilities = tuple(tool for tool in scope.capabilities if tool in command_tools)
                if (
                    rule.decision is not Decision.ALLOW
                    or not capabilities
                    or not any(fnmatchcase(identity, item) for item in scope.identities)
                    or not any(
                        fnmatchcase(self._outcome.objective_class, item)
                        for item in scope.objective_classes
                    )
                    or not any(
                        fnmatchcase(self._outcome.outcome_id, item) for item in scope.outcomes
                    )
                    or not any(fnmatchcase(role, item) for item in scope.roles)
                ):
                    continue
                hints.append(
                    {
                        "rule_id": rule.rule_id,
                        "capabilities": capabilities,
                        "executables": scope.executables,
                        "arguments": scope.arguments,
                    }
                )
                selectors.extend(scope.executables)
        return hints, tuple(dict.fromkeys(selectors))

    @staticmethod
    def _validation_failure_locations(exc: BaseException) -> tuple[str, ...]:
        """Expose only schema locations and error kinds, never rejected values."""

        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, ValidationError):
                locations: list[str] = []
                for error in current.errors(include_url=False, include_input=False):
                    path = ".".join(str(item) for item in error["loc"]) or "<root>"
                    locations.append(f"{path} [{error['type']}]")
                return tuple(locations)
            current = current.__cause__ or current.__context__
        return ()

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
            # MISHKAN owns the visible, finite structured-output retry policy.
            # CrewAI's additional implicit exception retry would multiply that
            # policy and make a configured timeout cease to be a useful bound.
            max_retry_limit=0,
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
