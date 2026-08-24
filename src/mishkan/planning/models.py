"""Structured planning and initialization result envelopes."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlanTask(PlanModel):
    task_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    title: str = Field(min_length=3, max_length=160)
    purpose: str = Field(min_length=3, max_length=2_000)
    assigned_role: str = Field(min_length=1)
    tools: tuple[str, ...] = ()
    evidence_paths: tuple[str, ...] = Field(min_length=1)
    depends_on: tuple[str, ...] = ()

    @field_validator("tools", "evidence_paths", "depends_on")
    @classmethod
    def values_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("values must be unique")
        return value


class PlanCandidate(PlanModel):
    schema_version: str = "1.0"
    objective: str = Field(min_length=3, max_length=2_000)
    outcome_id: str = Field(min_length=1)
    repository_revision: str = Field(min_length=7)
    tasks: tuple[PlanTask, ...] = Field(min_length=1, max_length=12)


class AcceptedPlan(PlanCandidate):
    fingerprint: str = Field(min_length=64, max_length=64)
    discovery_fingerprint: str = Field(min_length=64, max_length=64)


class InitializationResult(PlanModel):
    schema_version: str = "1.0"
    repository_revision: str = Field(min_length=7)
    task_id: str = Field(min_length=2)
    summary: str = Field(min_length=3, max_length=4_000)
    cited_paths: tuple[str, ...] = Field(min_length=1)
    findings: tuple[str, ...] = Field(min_length=1)


class InitializationReport(PlanModel):
    run_id: str
    repository_id: str
    repository_revision: str
    discovery_fingerprint: str
    plan_fingerprint: str
    resumed: bool
    completed_task_ids: tuple[str, ...]
    results: tuple[InitializationResult, ...]
