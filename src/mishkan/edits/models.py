"""Versioned recoverable change-set contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

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
    precondition_value: str | None = None
    inline_content: str | None = None
    artifact_reference: str | None = None
    match: str | None = None
    replacement: str | None = None
    expected_occurrences: int | None = Field(default=None, ge=0)
    expected_digest: str | None = None
    rewrite_engine: str | None = None
    rewrite_version: str | None = None
    rewrite_rule: str | None = None
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
        if self.kind in {ChangeOperationKind.REPLACE, ChangeOperationKind.PATCH} and (
            self.match is None or self.replacement is None or self.expected_occurrences is None
        ):
            raise ValueError("replace and patch require exact match, replacement, and count")
        if self.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            if self.destination is None:
                raise ValueError("move and copy require a destination")
        elif self.destination is not None:
            raise ValueError("destination is accepted only by move and copy")
        if self.kind is ChangeOperationKind.REWRITE and not all(
            (self.rewrite_engine, self.rewrite_version, self.rewrite_rule)
        ):
            raise ValueError("rewrite requires engine, version, and rule evidence")
        return self


class ChangeSet(DomainRecord):
    schema_version: str = "1.0"
    workspace: str = "."
    scope: str = Field(min_length=1)
    operations: tuple[ChangeOperation, ...] = Field(min_length=1, max_length=1_000)
    declared_effects: tuple[str, ...] = Field(min_length=1)


class ChangeSetResult(DomainRecord):
    change_set_id: str
    state: ChangeSetState
    completed_operations: int = Field(ge=0)
    revision: int = Field(ge=0)
    preimage_references: tuple[str, ...] = ()
    diff_reference: str | None = None
    reason: str | None = None
