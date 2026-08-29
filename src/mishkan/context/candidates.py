"""Configured community candidates and evidence-based contextual recommendations."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.sources import resolve_source_path
from mishkan.domain.time import require_aware, utc_now


class CandidateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateKind(StrEnum):
    TOOL = "tool"
    MCP_SERVER = "mcp_server"
    PLUGIN = "plugin"
    SKILL = "skill"
    TECHNICAL_PACK = "technical_pack"


class CandidateSourceKind(StrEnum):
    CONFIGURED_CATALOGUE = "configured_catalogue"
    OFFICIAL_DOCUMENTATION = "official_documentation"
    REGISTRY = "registry"
    REPOSITORY = "repository"
    HUB = "hub"


class ConstraintState(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


class CandidateConstraints(CandidateModel):
    compatibility: ConstraintState
    policy: ConstraintState
    trust: ConstraintState
    execution: ConstraintState


class CommunityCandidate(CandidateModel):
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    kind: CandidateKind
    name: str = Field(min_length=1, max_length=256)
    version: str | None = Field(default=None, max_length=256)
    revision: str = Field(min_length=1, max_length=512)
    source_kind: CandidateSourceKind
    source_locator: str = Field(min_length=1, max_length=2_048)
    observed_at: datetime
    evidence_references: tuple[str, ...] = Field(min_length=1, max_length=64)
    constraints: CandidateConstraints
    criterion_values: dict[str, float] = Field(default_factory=dict, max_length=64)
    maintenance_evidence: str | None = Field(default=None, max_length=2_048)
    trust_evidence: str | None = Field(default=None, max_length=2_048)
    overlap: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("observed_at")
    @classmethod
    def observed_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @field_validator("evidence_references", "overlap")
    @classmethod
    def bounded_unique_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item or len(item) > 2_048 for item in value):
            raise ValueError("candidate references must be bounded and unique")
        return value


class CommunityCandidateCatalogue(CandidateModel):
    schema_version: Literal["1.0"] = "1.0"
    catalogue_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    candidates: tuple[CommunityCandidate, ...] = Field(max_length=10_000)

    @model_validator(mode="after")
    def candidate_identities_are_unique(self) -> CommunityCandidateCatalogue:
        identities = [candidate.candidate_id for candidate in self.candidates]
        if len(identities) != len(set(identities)):
            raise ValueError("community candidate identities must be unique")
        return self


class RecommendationCriterion(CandidateModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    weight: float = Field(gt=0, le=1_000)


class ContextualRecommendationRequest(CandidateModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=new_id)
    owner_identity: str = Field(min_length=1, max_length=256)
    context_id: str = Field(min_length=1, max_length=256)
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=1_000)
    criteria: tuple[RecommendationCriterion, ...] = Field(min_length=1, max_length=64)
    project_evidence: tuple[str, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def inputs_are_unique(self) -> ContextualRecommendationRequest:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("recommendation candidate identities must be unique")
        names = [criterion.name for criterion in self.criteria]
        if len(names) != len(set(names)):
            raise ValueError("recommendation criterion names must be unique")
        if len(self.project_evidence) != len(set(self.project_evidence)):
            raise ValueError("project evidence references must be unique")
        return self


class CandidateAssessment(CandidateModel):
    candidate_id: str
    eligible: bool
    score: float | None = None
    rejected_by: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    evidence_references: tuple[str, ...]


class ContextualRecommendation(CandidateModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    context_id: str
    generated_at: datetime = Field(default_factory=utc_now)
    ranked: tuple[CandidateAssessment, ...]
    excluded: tuple[CandidateAssessment, ...]
    project_evidence: tuple[str, ...]
    catalogue_revisions: tuple[str, ...]
    activation_authorized: Literal[False] = False

    @field_validator("generated_at")
    @classmethod
    def generated_time_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class CommunityCandidateLoader:
    def load(
        self, sources: tuple[str, ...], project_root: Path
    ) -> tuple[CommunityCandidateCatalogue, ...]:
        catalogues: list[CommunityCandidateCatalogue] = []
        identities: set[str] = set()
        try:
            for source in sources:
                if source.startswith("package://"):
                    location = source.removeprefix("package://")
                    package, separator, resource = location.rpartition("/")
                    if not separator:
                        raise ValueError("package community-candidate source is invalid")
                    text = files(package).joinpath(resource).read_text(encoding="utf-8")
                else:
                    path = resolve_source_path(
                        source, project_root, "community candidate catalogue"
                    )
                    text = path.read_text(encoding="utf-8")
                catalogue = CommunityCandidateCatalogue.model_validate(yaml.safe_load(text))
                duplicates = identities.intersection(
                    candidate.candidate_id for candidate in catalogue.candidates
                )
                if duplicates:
                    raise ValueError(
                        f"duplicate community candidate identities: {sorted(duplicates)}"
                    )
                identities.update(candidate.candidate_id for candidate in catalogue.candidates)
                catalogues.append(catalogue)
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "community candidate catalogues cannot be loaded",
                details={"sources": list(sources)},
            ) from exc
        return tuple(catalogues)


class ContextualRecommendationService:
    def __init__(self, catalogues: tuple[CommunityCandidateCatalogue, ...]) -> None:
        self._catalogues = catalogues
        self._candidates = {
            candidate.candidate_id: candidate
            for catalogue in catalogues
            for candidate in catalogue.candidates
        }

    def candidates(self) -> tuple[CommunityCandidate, ...]:
        return tuple(self._candidates[key] for key in sorted(self._candidates))

    def recommend(self, request: ContextualRecommendationRequest) -> ContextualRecommendation:
        criteria = {criterion.name: criterion.weight for criterion in request.criteria}
        weight_total = sum(criteria.values())
        ranked: list[CandidateAssessment] = []
        excluded: list[CandidateAssessment] = []
        for candidate_id in request.candidate_ids:
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                excluded.append(
                    CandidateAssessment(
                        candidate_id=candidate_id,
                        eligible=False,
                        rejected_by=("candidate.not_configured",),
                        evidence_references=(),
                    )
                )
                continue
            states = candidate.constraints.model_dump(mode="json")
            failed = tuple(f"{name}.failed" for name, state in states.items() if state == "fail")
            unknown = tuple(
                f"{name}.unproven" for name, state in states.items() if state == "unknown"
            )
            missing = tuple(
                f"criterion.{name}.missing"
                for name in criteria
                if name not in candidate.criterion_values
            )
            if failed or unknown or missing:
                excluded.append(
                    CandidateAssessment(
                        candidate_id=candidate_id,
                        eligible=False,
                        rejected_by=failed + missing,
                        uncertainties=unknown,
                        evidence_references=candidate.evidence_references,
                    )
                )
                continue
            score = sum(
                candidate.criterion_values[name] * weight for name, weight in criteria.items()
            )
            ranked.append(
                CandidateAssessment(
                    candidate_id=candidate_id,
                    eligible=True,
                    score=round(score / weight_total, 12),
                    evidence_references=candidate.evidence_references,
                )
            )
        ranked.sort(key=lambda item: (-(item.score or 0), item.candidate_id))
        excluded.sort(key=lambda item: item.candidate_id)
        return ContextualRecommendation(
            request_id=request.request_id,
            context_id=request.context_id,
            ranked=tuple(ranked),
            excluded=tuple(excluded),
            project_evidence=request.project_evidence,
            catalogue_revisions=tuple(
                f"{catalogue.catalogue_id}@{catalogue.revision}" for catalogue in self._catalogues
            ),
        )
