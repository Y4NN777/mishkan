"""Public contracts for portable procedural skills."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.domain.identity import new_id
from mishkan.domain.time import require_aware, utc_now


class SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillSourceKind(StrEnum):
    BUNDLED = "bundled"
    PROJECT = "project"
    OPERATOR = "operator"
    COMMUNITY = "community"
    SOURCE_CONTROL = "source_control"
    URL = "url"
    HUB = "hub"


class SkillTrustState(StrEnum):
    UNTRUSTED = "untrusted"
    TRUSTED = "trusted"
    QUARANTINED = "quarantined"


class SkillActivationState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    INACTIVE = "inactive"
    QUARANTINED = "quarantined"
    ARCHIVED = "archived"


class SkillUseOutcome(StrEnum):
    HIT = "hit"
    PARTIAL = "partial"
    MISS = "miss"


class SkillVersionState(StrEnum):
    CANDIDATE = "candidate"
    VALIDATING = "validating"
    ELIGIBLE = "eligible"
    STAGED = "staged"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    SUPERSEDED = "superseded"


class SkillFindingCategory(StrEnum):
    SECURITY = "security"
    PRIVACY = "privacy"
    UNICODE = "unicode"
    CREDENTIAL = "credential"
    PROMPT_INJECTION = "prompt_injection"
    DESTRUCTIVE_ACTION = "destructive_action"


class SkillFindingSeverity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SkillMutationAction(StrEnum):
    CREATE = "create"
    PATCH = "patch"
    EDIT = "edit"
    DELETE = "delete"
    ARCHIVE = "archive"
    RESTORE = "restore"
    INSTALL = "install"
    UPDATE = "update"
    RESET = "reset"


class SkillMutationDisposition(StrEnum):
    ALLOW = "allow"
    REQUIRE_REVIEW = "require_review"
    DENY = "deny"


class SkillBundleMode(StrEnum):
    ALL = "all"
    SELECT = "select"


class SkillLearningSourceKind(StrEnum):
    TEXT = "text"
    ARTIFACT = "artifact"
    URL = "url"
    REPOSITORY_EVIDENCE = "repository_evidence"
    EXECUTION_EVIDENCE = "execution_evidence"


class SkillLearningState(StrEnum):
    REQUESTED = "requested"
    RESEARCHING = "researching"
    REVIEWING = "reviewing"
    PROPOSED = "proposed"
    REFUSED = "refused"
    FAILED = "failed"


class SkillBounds(SkillModel):
    max_frontmatter_bytes: int = Field(ge=128, le=1_048_576)
    max_manifest_bytes: int = Field(ge=256, le=16_777_216)
    max_resource_bytes: int = Field(ge=1, le=1_073_741_824)
    max_package_bytes: int = Field(ge=256, le=4_294_967_296)
    max_package_files: int = Field(ge=1, le=100_000)
    max_resource_depth: int = Field(ge=1, le=64)
    allow_frontmatter_extensions: bool


class SkillSourceDefinition(SkillModel):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    kind: SkillSourceKind
    uri: str = Field(min_length=1, max_length=4_096)
    revision: str = Field(min_length=1, max_length=512)
    precedence: int = Field(ge=-1_000_000, le=1_000_000)
    trust: SkillTrustState
    default_activation: SkillActivationState
    package_activation: dict[str, SkillActivationState] = Field(default_factory=dict)
    enabled: bool = True


class SkillMetadata(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1_024)
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    source_id: str = Field(min_length=1, max_length=128)
    source_kind: SkillSourceKind
    source_revision: str = Field(min_length=1, max_length=512)
    package_uri: str = Field(min_length=1, max_length=4_096)
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    trust: SkillTrustState
    activation: SkillActivationState
    license: str | None = Field(default=None, min_length=1, max_length=512)
    compatibility_summary: str | None = Field(default=None, min_length=1, max_length=500)
    allowed_tools_hint: str | None = Field(default=None, min_length=1, max_length=2_048)
    author_claim: str | None = Field(default=None, min_length=1, max_length=512)
    platforms: tuple[str, ...] = ("*",)
    required_tools: tuple[str, ...] = ()
    fallback_tools: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    organization_versions: tuple[str, ...] = ("*",)
    task_classes: tuple[str, ...] = ()
    resource_paths: tuple[str, ...] = ()

    @model_validator(mode="after")
    def fallbacks_belong_to_requirements(self) -> SkillMetadata:
        if not set(self.fallback_tools).issubset(self.required_tools):
            raise ValueError("skill fallbacks must correspond to required tools")
        return self


class SkillSelectionContext(SkillModel):
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    platform: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=512)
    available_tools: frozenset[str] = frozenset()


class SkillSelection(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    requested_name: str = Field(min_length=1, max_length=64)
    context: SkillSelectionContext
    outcome: SkillUseOutcome
    selected: SkillMetadata | None = None
    reason: str = Field(min_length=1, max_length=2_048)
    missing_conditions: tuple[str, ...] = ()
    fallback_bindings: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def result_matches_outcome(self) -> SkillSelection:
        if self.outcome is SkillUseOutcome.MISS and self.selected is not None:
            raise ValueError("skill miss cannot contain a selection")
        if self.outcome is not SkillUseOutcome.MISS and self.selected is None:
            raise ValueError("skill hit or partial outcome requires a selection")
        return self


class SkillLoadedResource(SkillModel):
    path: str = Field(min_length=1, max_length=512)
    fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)


class SkillLoadEvidence(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    skill_name: str = Field(min_length=1, max_length=64)
    skill_version: str = Field(min_length=1, max_length=128)
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    outcome: SkillUseOutcome
    reason: str = Field(min_length=1, max_length=2_048)
    instruction_fingerprint: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    loaded_resources: tuple[SkillLoadedResource, ...] = ()


class SkillBundleDefinition(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,127}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    summary: str = Field(min_length=3, max_length=1_024)
    mode: SkillBundleMode
    skills: tuple[str, ...] = Field(min_length=1)
    max_selected: int | None = Field(default=None, ge=1, le=1_000)

    @model_validator(mode="after")
    def selection_bound_matches_mode(self) -> SkillBundleDefinition:
        if len(self.skills) != len(set(self.skills)):
            raise ValueError("skill bundle identities must be unique")
        if self.mode is SkillBundleMode.ALL and self.max_selected is not None:
            raise ValueError("all-mode skill bundle cannot declare max_selected")
        if self.mode is SkillBundleMode.SELECT and self.max_selected is None:
            raise ValueError("select-mode skill bundle requires max_selected")
        return self


class SkillBundleResolution(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    bundle_id: str = Field(min_length=1, max_length=128)
    bundle_version: str = Field(min_length=1, max_length=128)
    context: SkillSelectionContext
    outcome: SkillUseOutcome
    selections: tuple[SkillSelection, ...]
    selected_skill_names: tuple[str, ...]
    reason: str = Field(min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def selected_names_are_proven(self) -> SkillBundleResolution:
        proven = {
            selection.selected.name
            for selection in self.selections
            if selection.selected is not None
        }
        if len(self.selected_skill_names) != len(set(self.selected_skill_names)):
            raise ValueError("selected bundle skill identities must be unique")
        if not set(self.selected_skill_names).issubset(proven):
            raise ValueError("selected bundle skills must have eligible selection evidence")
        if self.outcome is SkillUseOutcome.MISS and self.selected_skill_names:
            raise ValueError("missed bundle cannot select skills")
        if self.outcome is not SkillUseOutcome.MISS and not self.selected_skill_names:
            raise ValueError("resolved bundle must select at least one skill")
        return self


class SkillUsageRecord(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID = Field(default_factory=new_id)
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    requested_skill: str = Field(min_length=1, max_length=64)
    skill_version: str | None = Field(default=None, min_length=1, max_length=128)
    package_fingerprint: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    outcome: SkillUseOutcome
    reason: str = Field(min_length=1, max_length=2_048)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence: SkillLoadEvidence | SkillSelection
    recorded_at: datetime = Field(default_factory=utc_now)

    @field_validator("recorded_at")
    @classmethod
    def recorded_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def evidence_matches_record(self) -> SkillUsageRecord:
        if self.evidence.outcome is not self.outcome:
            raise ValueError("skill usage evidence outcome differs from its record")
        if isinstance(self.evidence, SkillSelection):
            if self.evidence.requested_name != self.requested_skill:
                raise ValueError("skill usage selection names another skill")
            if (
                self.evidence.context.task_id != self.task_id
                or self.evidence.context.task_class != self.task_class
                or self.evidence.context.consuming_identity != self.consuming_identity
            ):
                raise ValueError("skill usage selection belongs to another task context")
            if self.evidence.selected is not None:
                if self.skill_version != self.evidence.selected.version:
                    raise ValueError("skill usage version differs from its selection")
                if self.package_fingerprint != self.evidence.selected.package_fingerprint:
                    raise ValueError("skill usage fingerprint differs from its selection")
        else:
            if self.evidence.skill_name != self.requested_skill:
                raise ValueError("skill load evidence names another skill")
            if (
                self.evidence.task_id != self.task_id
                or self.evidence.task_class != self.task_class
                or self.evidence.consuming_identity != self.consuming_identity
            ):
                raise ValueError("skill load evidence belongs to another task context")
            if self.skill_version != self.evidence.skill_version:
                raise ValueError("skill usage version differs from its load evidence")
            if self.package_fingerprint != self.evidence.package_fingerprint:
                raise ValueError("skill usage fingerprint differs from its load evidence")
        return self


class SkillUsageSummary(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    task_class: str = Field(min_length=1, max_length=256)
    requested_skill: str | None = Field(default=None, min_length=1, max_length=64)
    hits: int = Field(ge=0)
    partials: int = Field(ge=0)
    misses: int = Field(ge=0)
    last_recorded_at: datetime | None = None

    @field_validator("last_recorded_at")
    @classmethod
    def last_recorded_at_is_unambiguous(cls, value: datetime | None) -> datetime | None:
        return None if value is None else require_aware(value)


class SkillProvenanceLock(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    source_id: str = Field(min_length=1, max_length=128)
    source_kind: SkillSourceKind
    source_uri: str = Field(min_length=1, max_length=4_096)
    resolved_revision: str = Field(min_length=1, max_length=512)
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    dependency_fingerprints: tuple[str, ...] = ()
    author_claim: str | None = Field(default=None, min_length=1, max_length=512)
    acquired_at: datetime = Field(default_factory=utc_now)

    @field_validator("acquired_at")
    @classmethod
    def acquired_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class SkillInspectionRule(SkillModel):
    rule_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    category: SkillFindingCategory
    severity: SkillFindingSeverity
    pattern: str = Field(min_length=1, max_length=8_192)
    path_patterns: tuple[str, ...] = ("*",)
    summary: str = Field(min_length=1, max_length=1_024)


class SkillInspectionProfile(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=512)
    adoption_authority: str = Field(min_length=1, max_length=256)
    quarantine_threshold: SkillFindingSeverity
    rules: tuple[SkillInspectionRule, ...] = Field(min_length=1)
    max_scanned_files: int = Field(ge=1, le=100_000)
    max_scanned_file_bytes: int = Field(ge=1, le=1_073_741_824)
    max_total_bytes: int = Field(ge=1, le=4_294_967_296)

    @model_validator(mode="after")
    def rules_cover_required_categories(self) -> SkillInspectionProfile:
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("skill inspection rule identities must be unique")
        missing = set(SkillFindingCategory) - {rule.category for rule in self.rules}
        if missing:
            raise ValueError(
                "skill inspection profile must cover every mandatory category: "
                + ", ".join(sorted(item.value for item in missing))
            )
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class SkillInspectionFinding(SkillModel):
    rule_id: str = Field(min_length=1, max_length=128)
    category: SkillFindingCategory
    severity: SkillFindingSeverity
    logical_path: str = Field(min_length=1, max_length=1_024)
    line: int | None = Field(default=None, ge=1)
    summary: str = Field(min_length=1, max_length=1_024)
    evidence_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class SkillInspectionResult(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    profile_id: str = Field(min_length=1, max_length=128)
    profile_revision: str = Field(min_length=1, max_length=512)
    profile_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    package_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    findings: tuple[SkillInspectionFinding, ...]
    scanned_files: int = Field(ge=0)
    scanned_bytes: int = Field(ge=0)
    quarantined: bool
    inspected_at: datetime = Field(default_factory=utc_now)

    @field_validator("inspected_at")
    @classmethod
    def inspected_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class SkillVersionRecord(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    id: UUID = Field(default_factory=new_id)
    skill_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    skill_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    state: SkillVersionState
    package_collection_id: UUID
    metadata: SkillMetadata
    provenance: SkillProvenanceLock
    inspection: SkillInspectionResult | None = None
    base_version_id: UUID | None = None
    mutation_action: SkillMutationAction
    mutation_artifact_id: UUID | None = None
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    activation_decision_id: UUID | None = None
    revision: int = Field(default=1, ge=1)
    pinned: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamp_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def package_matches_provenance(self) -> SkillVersionRecord:
        if (
            self.metadata.name != self.skill_name
            or self.metadata.version != self.skill_version
            or self.metadata.package_fingerprint != self.provenance.package_fingerprint
            or self.metadata.source_id != self.provenance.source_id
            or self.metadata.source_kind is not self.provenance.source_kind
            or self.metadata.source_revision != self.provenance.resolved_revision
        ):
            raise ValueError("skill metadata differs from its version or provenance lock")
        if self.inspection is not None and (
            self.inspection.package_fingerprint != self.provenance.package_fingerprint
        ):
            raise ValueError("skill inspection belongs to another package")
        return self


class SkillInvocationRequest(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    requested_name: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=64,
    )
    bundle_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{2,127}$",
    )
    context: SkillSelectionContext

    @model_validator(mode="after")
    def explicit_target_is_unambiguous(self) -> SkillInvocationRequest:
        if self.requested_name is not None and self.bundle_id is not None:
            raise ValueError("skill invocation cannot request a skill and bundle together")
        return self


class SkillInvocationEvidence(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    request: SkillInvocationRequest
    outcome: SkillUseOutcome
    selections: tuple[SkillSelection, ...]
    load_evidence: tuple[SkillLoadEvidence, ...]
    package_collections: dict[str, UUID]
    instruction_artifacts: dict[str, str]
    reason: str = Field(min_length=1, max_length=2_048)

    @model_validator(mode="after")
    def loaded_skills_have_exact_artifact_evidence(self) -> SkillInvocationEvidence:
        names = {item.skill_name for item in self.load_evidence}
        if set(self.package_collections) != names or set(self.instruction_artifacts) != names:
            raise ValueError("loaded skills require exact collection and instruction artifacts")
        if self.outcome is SkillUseOutcome.MISS and names:
            raise ValueError("missed skill invocation cannot contain loaded skills")
        if self.outcome is not SkillUseOutcome.MISS and not names:
            raise ValueError("successful skill invocation requires load evidence")
        return self


class SkillLearningSource(SkillModel):
    kind: SkillLearningSourceKind
    locator: str = Field(min_length=1, max_length=4_096)
    content: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def content_matches_source_kind(self) -> SkillLearningSource:
        if self.kind is SkillLearningSourceKind.TEXT:
            if self.content is None:
                raise ValueError("text learning source requires inline content")
        elif self.content is not None:
            raise ValueError("non-text learning source cannot carry inline content")
        if self.kind in {
            SkillLearningSourceKind.ARTIFACT,
            SkillLearningSourceKind.REPOSITORY_EVIDENCE,
            SkillLearningSourceKind.EXECUTION_EVIDENCE,
        }:
            if not self.locator.startswith("artifact:"):
                raise ValueError("evidence learning source must use an artifact reference")
            try:
                UUID(self.locator.removeprefix("artifact:"))
            except ValueError as exc:
                raise ValueError("learning source artifact reference is invalid") from exc
        if self.kind is SkillLearningSourceKind.URL:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.locator)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("URL learning source must use an absolute HTTP(S) URL")
            if parsed.username is not None or parsed.password is not None:
                raise ValueError("URL learning source cannot contain credentials")
        return self


class SkillLearningRequest(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID = Field(default_factory=new_id)
    task_id: str = Field(min_length=1, max_length=256)
    task_class: str = Field(min_length=1, max_length=256)
    consuming_identity: str = Field(min_length=1, max_length=256)
    suggested_name: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
        max_length=64,
    )
    sources: tuple[SkillLearningSource, ...] = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=128)
    organization_version: str = Field(min_length=1, max_length=512)
    available_tools: frozenset[str] = frozenset()
    reason: str = Field(min_length=1, max_length=2_048)
    requested_at: datetime = Field(default_factory=utc_now)

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)


class SkillPackageDraft(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    skill_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=1_024)
    instructions_markdown: str = Field(min_length=1, max_length=262_144)
    required_tools: tuple[str, ...] = Field(max_length=256)
    fallback_tools: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    task_classes: tuple[str, ...] = Field(min_length=1, max_length=256)
    retrieval_references: tuple[str, ...] = Field(max_length=128)
    source_fingerprints: tuple[str, ...] = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=4_096)

    @model_validator(mode="after")
    def fallbacks_and_task_classes_are_coherent(self) -> SkillPackageDraft:
        if not set(self.fallback_tools).issubset(self.required_tools):
            raise ValueError("draft fallback tools must belong to its required tools")
        if len(self.task_classes) != len(set(self.task_classes)):
            raise ValueError("draft task classes must be unique")
        return self

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()


class SkillLearningReview(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    draft_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    evaluator_identity: Literal["Research_Evaluator"] = "Research_Evaluator"
    accepted: bool
    findings: tuple[str, ...] = Field(max_length=128)
    reason: str = Field(min_length=1, max_length=4_096)


class SkillLearningRecord(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    request: SkillLearningRequest
    state: SkillLearningState
    source_fingerprints: tuple[str, ...] = Field(min_length=1, max_length=128)
    base_version_id: UUID | None = None
    draft: SkillPackageDraft | None = None
    review: SkillLearningReview | None = None
    package_collection_id: UUID | None = None
    candidate_version_id: UUID | None = None
    author_identity: Literal["Research_Synthesizer"] = "Research_Synthesizer"
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    refusal_code: str | None = Field(default=None, min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("created_at", "updated_at")
    @classmethod
    def learning_timestamp_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)

    @model_validator(mode="after")
    def settlement_has_required_evidence(self) -> SkillLearningRecord:
        if (
            self.review is not None
            and self.draft is not None
            and self.review.draft_fingerprint != self.draft.fingerprint
        ):
            raise ValueError("learning review belongs to another draft")
        if self.state is SkillLearningState.PROPOSED and (
            self.draft is None
            or self.review is None
            or not self.review.accepted
            or self.package_collection_id is None
            or self.candidate_version_id is None
        ):
            raise ValueError("proposed learning record requires accepted immutable evidence")
        if self.state is SkillLearningState.REFUSED and self.refusal_code is None:
            raise ValueError("refused learning record requires a stable refusal code")
        return self


class SkillLifecycleDecision(SkillModel):
    schema_version: Literal["1.0"] = "1.0"
    decision_id: UUID = Field(default_factory=new_id)
    version_id: UUID
    disposition: SkillMutationDisposition
    actor_id: str = Field(min_length=1, max_length=256)
    policy_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_active_version_id: UUID | None = None
    quarantine_override: bool = False
    reason: str = Field(min_length=1, max_length=2_048)
    decided_at: datetime = Field(default_factory=utc_now)

    @field_validator("decided_at")
    @classmethod
    def decided_at_is_unambiguous(cls, value: datetime) -> datetime:
        return require_aware(value)
