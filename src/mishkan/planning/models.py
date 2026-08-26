"""Structured planning, execution, and review envelopes."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.policy.models import ApprovalEvidence, AuthorizationDecision
from mishkan.tools.models import RegistrySnapshot, ToolBinding, argument_fingerprint


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedToolCall(PlanModel):
    """One exact model-authored call accepted before task execution."""

    call_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    arguments: dict[str, Any]

    @property
    def argument_fingerprint(self) -> str:
        return argument_fingerprint(self.arguments)


class PlanTask(PlanModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    title: str = Field(min_length=3, max_length=160)
    purpose: str = Field(min_length=3, max_length=2_000)
    assigned_role: str = Field(min_length=1)
    tools: tuple[str, ...] = Field(min_length=1)
    tool_calls: tuple[PlannedToolCall, ...] = ()
    evidence_paths: tuple[str, ...] = Field(min_length=1)
    depends_on: tuple[str, ...] = ()

    @field_validator("tools", "evidence_paths", "depends_on")
    @classmethod
    def values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value

    @model_validator(mode="after")
    def calls_reference_selected_tools(self) -> "PlanTask":
        call_ids = [call.call_id for call in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("planned tool call identifiers must be unique")
        unknown = sorted({call.tool_id for call in self.tool_calls} - set(self.tools))
        if unknown:
            raise ValueError(f"planned calls reference unselected tools: {unknown}")
        return self


class PlanCandidate(PlanModel):
    schema_version: str = "1.0"
    objective: str = Field(min_length=3, max_length=2_000)
    outcome_id: str = Field(min_length=1)
    repository_revision: str = Field(min_length=7)
    tasks: tuple[PlanTask, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def versioned_call_contract_is_complete(self) -> "PlanCandidate":
        if self.schema_version == "1.1":
            incomplete = [
                task.task_id
                for task in self.tasks
                if not task.tool_calls
                or {call.tool_id for call in task.tool_calls} != set(task.tools)
            ]
            if incomplete:
                raise ValueError(
                    "plan 1.1 requires one or more exact calls for every selected tool: "
                    f"{incomplete}"
                )
        return self


class AcceptedPlan(PlanCandidate):
    schema_version: str = "1.0"
    fingerprint: str = Field(min_length=64, max_length=64)
    discovery_fingerprint: str = Field(min_length=64, max_length=64)
    registry: RegistrySnapshot | None = None
    tool_bindings: tuple[ToolBinding, ...] = ()
    policy_fingerprint: str | None = None
    approvals: tuple[ApprovalEvidence, ...] = ()
    authorizations: tuple[AuthorizationDecision, ...] = ()

    @model_validator(mode="after")
    def governed_plan_has_complete_lineage(self) -> "AcceptedPlan":
        if self.schema_version == "1.1" and (
            self.registry is None
            or not self.tool_bindings
            or self.policy_fingerprint is None
            or not self.authorizations
        ):
            raise ValueError("accepted plan 1.1 requires complete policy and tool lineage")
        return self

    def binding_for(self, task_id: str, role: str, tool_id: str) -> ToolBinding:
        matches = [
            binding
            for binding in self.tool_bindings
            if binding.task_id == task_id and binding.role == role and binding.tool_id == tool_id
        ]
        if len(matches) != 1:
            raise ValueError("accepted plan does not contain one exact tool binding")
        return matches[0]


class InitializationResult(PlanModel):
    schema_version: str = "1.0"
    repository_revision: str = Field(min_length=7)
    task_id: str = Field(min_length=2)
    summary: str = Field(min_length=3, max_length=2_000)
    cited_paths: tuple[str, ...] = Field(
        min_length=1,
        description="Exact repository-relative evidence paths from the accepted task.",
    )
    findings: tuple[str, ...] = Field(
        min_length=1,
        description="Factual findings directly supported by the cited repository paths.",
    )


class ReviewDecision(PlanModel):
    schema_version: str = "1.0"
    task_id: str = Field(min_length=2)
    verdict: Literal["accepted", "rejected"]
    summary: str = Field(min_length=3, max_length=2_000)
    checked_citations: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Exact repository-relative path strings copied from the proposed result's "
            "cited_paths; never findings or explanations."
        ),
    )
    issues: tuple[str, ...] = Field(
        default=(),
        description="Concrete evidence or contract defects; empty when the verdict is accepted.",
    )


class InitializationReport(PlanModel):
    run_id: str
    repository_id: str
    repository_revision: str
    discovery_fingerprint: str
    plan_fingerprint: str
    resumed: bool
    completed_task_ids: tuple[str, ...]
    results: tuple[InitializationResult, ...]
    reviews: tuple[ReviewDecision, ...]
