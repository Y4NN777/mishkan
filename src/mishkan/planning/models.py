"""Structured planning, execution, and review envelopes."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.policy.models import ApprovalEvidence, AuthorizationDecision
from mishkan.repository.models import (
    ProspectiveWorkspaceBinding,
    RepositoryBinding,
)
from mishkan.tools.models import RegistrySnapshot, ToolBinding, argument_fingerprint


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanExecutionContext(PlanModel):
    kind: Literal["repository", "prospective_workspace"]
    context_id: str = Field(min_length=12, max_length=256)
    revision: str = Field(min_length=7, max_length=128)
    repository_id: str | None = Field(default=None, min_length=12, max_length=256)
    repository_revision: str | None = Field(default=None, min_length=7, max_length=128)
    prospective_workspace_id: str | None = Field(default=None, min_length=12, max_length=256)
    discovery_revision: str | None = Field(default=None, min_length=7, max_length=128)

    @model_validator(mode="after")
    def identity_matches_kind(self) -> "PlanExecutionContext":
        if self.kind == "repository":
            if (
                self.repository_id != self.context_id
                or self.repository_revision != self.revision
                or self.prospective_workspace_id is not None
                or self.discovery_revision is not None
            ):
                raise ValueError("repository plan context has inconsistent identity")
        elif (
            self.prospective_workspace_id != self.context_id
            or self.discovery_revision != self.revision
            or self.repository_id is not None
            or self.repository_revision is not None
        ):
            raise ValueError("prospective plan context has inconsistent identity")
        return self

    @classmethod
    def from_binding(
        cls,
        binding: RepositoryBinding | ProspectiveWorkspaceBinding,
    ) -> "PlanExecutionContext":
        if isinstance(binding, RepositoryBinding):
            return cls(
                kind="repository",
                context_id=binding.repository_id,
                revision=binding.base_revision,
                repository_id=binding.repository_id,
                repository_revision=binding.base_revision,
            )
        return cls(
            kind="prospective_workspace",
            context_id=binding.workspace_id,
            revision=binding.discovery_revision,
            prospective_workspace_id=binding.workspace_id,
            discovery_revision=binding.discovery_revision,
        )


class PlanOrganizationBinding(PlanModel):
    organization_id: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=64)
    organization_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    mission_id: UUID | None = None


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
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    objective: str = Field(min_length=3, max_length=2_000)
    outcome_id: str = Field(min_length=1)
    repository_revision: str | None = Field(default=None, min_length=7)
    execution_context: PlanExecutionContext | None = None
    tasks: tuple[PlanTask, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def versioned_call_contract_is_complete(self) -> "PlanCandidate":
        if self.schema_version in {"1.1", "1.2"}:
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
        if self.schema_version in {"1.0", "1.1"} and self.repository_revision is None:
            raise ValueError("legacy repository plan requires a repository revision")
        if self.schema_version == "1.2":
            if self.execution_context is None:
                raise ValueError("plan 1.2 requires one explicit execution context")
            if self.repository_revision != self.execution_context.repository_revision:
                raise ValueError("plan repository revision contradicts its execution context")
        return self


class AcceptedPlan(PlanCandidate):
    schema_version: Literal["1.0", "1.1", "1.2"] = "1.0"
    fingerprint: str = Field(min_length=64, max_length=64)
    discovery_fingerprint: str = Field(min_length=64, max_length=64)
    registry: RegistrySnapshot | None = None
    tool_bindings: tuple[ToolBinding, ...] = ()
    policy_fingerprint: str | None = None
    approvals: tuple[ApprovalEvidence, ...] = ()
    authorizations: tuple[AuthorizationDecision, ...] = ()
    organization_binding: PlanOrganizationBinding | None = None

    @model_validator(mode="after")
    def governed_plan_has_complete_lineage(self) -> "AcceptedPlan":
        if self.schema_version in {"1.1", "1.2"} and (
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
    schema_version: Literal["1.0", "1.1"] = "1.0"
    repository_revision: str | None = Field(default=None, min_length=7)
    execution_context: PlanExecutionContext | None = None
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

    @model_validator(mode="after")
    def result_context_is_explicit(self) -> "InitializationResult":
        if self.schema_version == "1.0" and self.repository_revision is None:
            raise ValueError("legacy result requires a repository revision")
        if self.schema_version == "1.1":
            if self.execution_context is None:
                raise ValueError("result 1.1 requires one explicit execution context")
            if self.repository_revision != self.execution_context.repository_revision:
                raise ValueError("result repository revision contradicts its execution context")
        return self


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
