"""CrewAI-only research boundary for attributable skill proposals and review."""

from __future__ import annotations

import json
from collections.abc import Iterable
from functools import lru_cache
from importlib.resources import files
from typing import TypeVar, cast

import yaml
from crewai import LLM, Agent, Crew, Process, Task
from crewai.crews.crew_output import CrewOutput
from pydantic import BaseModel

from mishkan.config.models import MishkanConfig
from mishkan.crewai.routing import CrewAIModelRouter
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization.models import OrganizationDefinition, RoleDefinition
from mishkan.skills.models import (
    SkillLearningRequest,
    SkillLearningReview,
    SkillPackageDraft,
    SkillVersionRecord,
)

OutputT = TypeVar("OutputT", bound=BaseModel)


@lru_cache(maxsize=1)
def _research_roles() -> OrganizationDefinition:
    resource = files("mishkan.resources.organization").joinpath("skill-learning-research.yaml")
    document = yaml.safe_load(resource.read_text(encoding="utf-8"))
    return OrganizationDefinition.model_validate(document)


class CrewAISkillLearningRunner:
    """Use canonical Research identities without introducing another agent runtime."""

    def __init__(self, config: MishkanConfig) -> None:
        self._config = config
        self._models = CrewAIModelRouter(config)
        self._organization = _research_roles()

    def propose(
        self,
        request: SkillLearningRequest,
        *,
        desired_name: str,
        base: SkillVersionRecord | None,
        source_packet: tuple[dict[str, object], ...],
        source_fingerprints: tuple[str, ...],
    ) -> SkillPackageDraft:
        role = self._role("Research_Synthesizer")
        base_payload = None if base is None else base.metadata.model_dump(mode="json")
        description = f"""
Create one bounded procedural skill draft from only the attributed source packet below.

Exact skill identity: {desired_name}
Task class that MUST remain applicable: {request.task_class}
Available tool identities: {json.dumps(sorted(request.available_tools))}
Existing applicable package metadata, if this is an update:
{json.dumps(base_payload, sort_keys=True)}
Exact source fingerprints:
{json.dumps(source_fingerprints)}
Attributed source packet:
{json.dumps(source_packet, sort_keys=True)}

Rules:
- Return skill_name exactly {desired_name!r}.
- Include task class {request.task_class!r}.
- source_fingerprints must exactly equal the supplied ordered fingerprints.
- Write concise operational Markdown instructions, not a workflow or an authority grant.
- Name required tools only when the evidence actually requires them and only from available tools.
- Put unacquired URLs in retrieval_references and do not invent claims from their contents.
- Prefer a coherent update of the supplied base metadata over a duplicate capability.
- Never include secrets, credentials, policy grants, activation claims, or destructive effects.
""".strip()
        return self._kickoff_structured(
            role,
            description,
            "One complete SkillPackageDraft grounded in the supplied evidence.",
            SkillPackageDraft,
        )

    def review(
        self,
        request: SkillLearningRequest,
        draft: SkillPackageDraft,
        *,
        source_fingerprints: tuple[str, ...],
    ) -> SkillLearningReview:
        role = self._role("Research_Evaluator")
        description = f"""
Independently evaluate the proposed procedural skill against its exact learning request.

Task class: {request.task_class}
Available tools: {json.dumps(sorted(request.available_tools))}
Exact source fingerprints: {json.dumps(source_fingerprints)}
Draft fingerprint: {draft.fingerprint}
Draft: {draft.model_dump_json()}

Accept only if the draft is attributable, coherent, non-duplicative with its requested identity,
does not claim authority, names only available required tools, includes the task class, and does
not invent content for unacquired references. Return the exact draft fingerprint. Findings and
reason must be non-secret and specific.
""".strip()
        return self._kickoff_structured(
            role,
            description,
            "One complete independent SkillLearningReview.",
            SkillLearningReview,
        )

    def _kickoff_structured(
        self,
        role: RoleDefinition,
        description: str,
        expected_output: str,
        output_model: type[OutputT],
    ) -> OutputT:
        failures: list[tuple[str, ...]] = []
        for llm in self._models.candidates_for(role.model_route):
            retry_description = description
            for _attempt in range(self._config.crewai.structured_output_retries + 1):
                try:
                    output = self._crew(role, llm, retry_description, expected_output, output_model)
                    if isinstance(output.pydantic, output_model):
                        return output.pydantic
                    return output_model.model_validate_json(output.raw)
                except Exception as exc:
                    failures.append(self._exception_type_chain(exc))
                    retry_description = (
                        description
                        + f"\n\nThe prior result failed {output_model.__name__} validation. "
                        "Return one complete value matching the declared schema."
                    )
        last_failure = " <- ".join(failures[-1]) if failures else "unknown"
        raise MishkanError(
            ErrorCode.REQUIRED_DEPENDENCY,
            f"all configured CrewAI skill-learning candidates failed: {last_failure}",
            details={"role": role.name, "failure_type_chains": [list(item) for item in failures]},
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
        values = list(matches)
        if len(values) != 1:
            raise MishkanError(
                ErrorCode.ROLE_CONFLICT,
                "skill-learning organization does not define exactly one required role",
                details={"role": name},
            )
        return values[0]

    @staticmethod
    def _exception_type_chain(exc: BaseException) -> tuple[str, ...]:
        chain: list[str] = []
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        return tuple(chain)
