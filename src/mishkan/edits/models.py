"""Versioned recoverable change-set contracts."""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Self

from pydantic import Field, field_validator, model_validator

from mishkan.domain.identity import DomainRecord


class ChangeOperationKind(StrEnum):
    MKDIR = "mkdir"
    CREATE = "create"
    WRITE = "write"
    REPLACE = "replace"
    PATCH = "patch"
    REWRITE = "rewrite"
    MOVE = "move"
    COPY = "copy"
    DELETE = "delete"


class PreconditionKind(StrEnum):
    ABSENT = "absent"
    DIGEST = "digest"
    REVISION = "revision"
    GIT_BLOB = "git_blob"


class RollbackPolicy(StrEnum):
    RESTORE = "restore"
    RETAIN = "retain"


class ChangeValidationKind(StrEnum):
    EXISTS = "exists"
    ABSENT = "absent"
    DIGEST = "digest"
    MODE = "mode"


class ChangeSetState(StrEnum):
    PLANNED = "planned"
    APPLYING = "applying"
    APPLIED = "applied"
    VERIFIED = "verified"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"


class ChangeOperation(DomainRecord):
    kind: ChangeOperationKind
    path: str = Field(min_length=1, max_length=4096)
    destination: str | None = Field(default=None, min_length=1, max_length=4096)
    precondition: PreconditionKind
    precondition_value: str | None = Field(default=None, max_length=4_096)
    destination_precondition: PreconditionKind | None = None
    destination_precondition_value: str | None = Field(default=None, max_length=4_096)
    inline_content: str | None = Field(default=None, max_length=1_048_576)
    artifact_reference: str | None = None
    match: str | None = Field(default=None, max_length=262_144)
    replacement: str | None = Field(default=None, max_length=1_048_576)
    expected_occurrences: int | None = Field(default=None, ge=0)
    patch: str | None = Field(default=None, max_length=1_048_576)
    expected_digest: str | None = None
    result_mode: int | None = Field(default=None, ge=0, le=0o7777)
    rewrite_engine: str | None = None
    rewrite_version: str | None = None
    rewrite_rule: str | None = Field(default=None, max_length=262_144)
    rewrite_language: str | None = None
    rewrite_scope: str | None = None
    rewrite_matches: int | None = Field(default=None, ge=0)
    rewrite_parse_failures: tuple[str, ...] = ()
    rewrite_ignored_files: tuple[str, ...] = ()
    rewrite_formatting: str | None = None
    rewrite_limits: dict[str, int] = Field(default_factory=dict)
    semantic_preservation_evidence: str | None = Field(default=None, max_length=262_144)
    rollback: RollbackPolicy = RollbackPolicy.RESTORE

    @model_validator(mode="after")
    def fields_match_operation(self) -> Self:
        if self.precondition is PreconditionKind.ABSENT and self.precondition_value is not None:
            raise ValueError("absent precondition accepts no value")
        if self.precondition is not PreconditionKind.ABSENT and not self.precondition_value:
            raise ValueError("selected precondition requires an exact value")
        sources = sum(value is not None for value in (self.inline_content, self.artifact_reference))
        content_kinds = {
            ChangeOperationKind.CREATE,
            ChangeOperationKind.WRITE,
            ChangeOperationKind.REWRITE,
        }
        if self.kind in content_kinds and sources != 1:
            raise ValueError("content operation requires exactly one content source")
        if self.kind is ChangeOperationKind.REPLACE and (
            self.match is None or self.replacement is None or self.expected_occurrences is None
        ):
            raise ValueError("replace requires exact match, replacement, and count")
        if self.kind is ChangeOperationKind.PATCH and not self.patch:
            raise ValueError("patch requires one non-empty unified diff")
        if self.kind is ChangeOperationKind.PATCH and any(
            value is not None for value in (self.match, self.replacement, self.expected_occurrences)
        ):
            raise ValueError("patch cannot also declare replacement fields")
        if self.kind is not ChangeOperationKind.PATCH and self.patch is not None:
            raise ValueError("unified diff content is accepted only by patch operations")
        if (
            self.precondition is PreconditionKind.ABSENT
            and self.kind
            in {
                ChangeOperationKind.MKDIR,
                ChangeOperationKind.CREATE,
                ChangeOperationKind.WRITE,
                ChangeOperationKind.REWRITE,
            }
            and self.result_mode is None
        ):
            raise ValueError("a newly created path requires an explicit result mode")
        if self.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            if self.destination is None:
                raise ValueError("move and copy require a destination")
            if self.destination_precondition is None:
                raise ValueError("move and copy require an explicit destination precondition")
        elif self.destination is not None:
            raise ValueError("destination is accepted only by move and copy")
        elif self.destination_precondition is not None or self.destination_precondition_value:
            raise ValueError("destination preconditions are accepted only by move and copy")
        if self.kind is ChangeOperationKind.MOVE and self.result_mode is not None:
            raise ValueError("move preserves the source mode and accepts no result mode")
        if self.destination_precondition is PreconditionKind.ABSENT:
            if self.destination_precondition_value is not None:
                raise ValueError("absent destination precondition accepts no value")
        elif self.destination_precondition is not None and not self.destination_precondition_value:
            raise ValueError("selected destination precondition requires an exact value")
        if self.kind is ChangeOperationKind.REWRITE:
            required = (
                self.rewrite_engine,
                self.rewrite_version,
                self.rewrite_rule,
                self.rewrite_language,
                self.rewrite_scope,
                self.rewrite_formatting,
            )
            if not all(required) or self.rewrite_matches is None or not self.rewrite_limits:
                raise ValueError(
                    "rewrite requires engine, version, rule, language, scope, matches, "
                    "formatting, and limits evidence"
                )
        elif (
            any(
                value is not None
                for value in (
                    self.rewrite_engine,
                    self.rewrite_version,
                    self.rewrite_rule,
                    self.rewrite_language,
                    self.rewrite_scope,
                    self.rewrite_matches,
                    self.rewrite_formatting,
                    self.semantic_preservation_evidence,
                )
            )
            or self.rewrite_parse_failures
            or self.rewrite_ignored_files
            or self.rewrite_limits
        ):
            raise ValueError("rewrite evidence is accepted only by rewrite operations")
        return self


class ChangeValidation(DomainRecord):
    kind: ChangeValidationKind
    path: str = Field(min_length=1, max_length=4096)
    expected_value: str | int | None = None

    @model_validator(mode="after")
    def expected_value_matches_kind(self) -> Self:
        if self.kind in {ChangeValidationKind.EXISTS, ChangeValidationKind.ABSENT}:
            if self.expected_value is not None:
                raise ValueError("existence validation accepts no expected value")
        elif self.kind is ChangeValidationKind.DIGEST:
            if not isinstance(self.expected_value, str) or not self.expected_value:
                raise ValueError("digest validation requires an exact digest")
        elif not isinstance(self.expected_value, int) or not 0 <= self.expected_value <= 0o7777:
            raise ValueError("mode validation requires a POSIX mode")
        return self


class ChangeValidationResult(DomainRecord):
    validation_id: str
    kind: ChangeValidationKind
    path: str
    passed: bool
    expected: str | int | None = None
    observed: str | int | None = None


class ChangeSet(DomainRecord):
    schema_version: str = "1.0"
    workspace: str = "."
    scope: str = Field(min_length=1)
    path_scopes: tuple[str, ...] = (".",)
    operations: tuple[ChangeOperation, ...] = Field(min_length=1, max_length=1_000)
    declared_effects: tuple[str, ...] = Field(min_length=1)
    validations: tuple[ChangeValidation, ...] = ()

    @field_validator("path_scopes")
    @classmethod
    def scopes_are_workspace_relative(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("change-set path scopes must be non-empty and unique")
        for item in value:
            path = PurePosixPath(item)
            if not item or path.is_absolute() or ".." in path.parts:
                raise ValueError("change-set path scopes must remain workspace-relative")
        return value


class ChangeSetResult(DomainRecord):
    change_set_id: str
    state: ChangeSetState
    completed_operations: int = Field(ge=0)
    revision: int = Field(ge=0)
    preimage_references: tuple[str, ...] = ()
    diff_reference: str | None = None
    changed_paths: tuple[str, ...] = ()
    scope_deviations: tuple[str, ...] = ()
    validation_results: tuple[ChangeValidationResult, ...] = ()
    atomicity: str = "per_operation_atomic_recoverable_set"
    reason: str | None = None
