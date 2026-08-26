"""Crash-recoverable, exact filesystem change-set application."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.edits.models import (
    ChangeOperation,
    ChangeOperationKind,
    ChangeSet,
    ChangeSetResult,
    ChangeSetState,
    PreconditionKind,
)
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import ChangeOperationRow, ChangeSetRow, LocalRunRepository


class ChangeSetService:
    def __init__(
        self,
        database: Path,
        workspace: Path,
        artifacts: DurableArtifactService,
        *,
        after_effect_hook: Callable[[int], None] | None = None,
    ) -> None:
        SchemaManager(database).require_current()
        self._workspace = workspace.resolve(strict=True)
        self._artifacts = artifacts
        self._after_effect_hook = after_effect_hook or (lambda _position: None)
        self._engine = create_engine(f"sqlite:///{database.resolve()}")
        event.listen(self._engine, "connect", LocalRunRepository._configure_connection)

    def plan(self, change_set: ChangeSet) -> ChangeSetResult:
        now = utc_now()
        with Session(self._engine) as session, session.begin():
            if session.get(ChangeSetRow, str(change_set.id)) is not None:
                return self._result(session, str(change_set.id))
            session.add(
                ChangeSetRow(
                    id=str(change_set.id),
                    workspace=change_set.workspace,
                    scope=change_set.scope,
                    state=ChangeSetState.PLANNED.value,
                    revision=0,
                    operation_index=0,
                    payload=change_set.model_dump_json(),
                    diff_reference=None,
                    reason=None,
                    created_at=now.isoformat(),
                    updated_at=now.isoformat(),
                )
            )
            session.flush()
            for position, operation in enumerate(change_set.operations):
                session.add(
                    ChangeOperationRow(
                        change_set_id=str(change_set.id),
                        position=position,
                        state="pending",
                        before_token=None,
                        preimage_reference=None,
                        expected_after_token=None,
                        actual_after_token=None,
                        payload=operation.model_dump_json(),
                        updated_at=now.isoformat(),
                    )
                )
        return ChangeSetResult(
            change_set_id=str(change_set.id),
            state=ChangeSetState.PLANNED,
            completed_operations=0,
            revision=0,
        )

    def apply(self, change_set_id: UUID) -> ChangeSetResult:
        change_set = self._load(change_set_id)
        self._set_state(change_set_id, ChangeSetState.APPLYING)
        diffs: list[str] = []
        try:
            for position, operation in enumerate(change_set.operations):
                journal = self._journal(change_set_id, position)
                if journal.state == "applied":
                    continue
                path = self._safe_path(operation.path)
                if journal.state == "prepared":
                    recovery = self._recover_prepared(journal, operation, path)
                    if recovery == "applied":
                        continue
                    if recovery != "retry":
                        return self._finish(
                            change_set_id,
                            ChangeSetState.CONFLICT,
                            "real state differs from both preimage and expected after-state",
                        )
                before = self._read_file(path)
                self._check_precondition(operation, path)
                after = self._expected_content(operation, path, before)
                preimage = self._preimage(change_set_id, position, path, before)
                expected_after = self._expected_token(operation, path, after)
                if (
                    operation.expected_digest is not None
                    and expected_after != operation.expected_digest
                ):
                    raise MishkanError(
                        ErrorCode.REVISION_MISMATCH,
                        "declared expected result differs from computed exact result",
                    )
                self._prepare(
                    change_set_id,
                    position,
                    before_token=self._token(path),
                    preimage_reference=preimage,
                    expected_after_token=expected_after,
                )
                self._apply_operation(operation, path, after)
                self._after_effect_hook(position)
                actual = self._operation_token(operation, path)
                if actual != expected_after:
                    return self._rollback_one(
                        change_set_id,
                        position,
                        operation,
                        path,
                        reason="operation after-state failed exact verification",
                    )
                self._mark_applied(change_set_id, position, actual)
                if before is not None and after is not None:
                    diffs.extend(
                        difflib.unified_diff(
                            before.decode(errors="replace").splitlines(keepends=True),
                            after.decode(errors="replace").splitlines(keepends=True),
                            fromfile=f"a/{operation.path}",
                            tofile=f"b/{operation.path}",
                        )
                    )
            self._set_state(change_set_id, ChangeSetState.APPLIED)
            diff_reference = self._diff_artifact(change_set_id, "".join(diffs).encode())
            return self._finish(
                change_set_id,
                ChangeSetState.VERIFIED,
                None,
                diff_reference=diff_reference,
            )
        except MishkanError as error:
            return self._finish(
                change_set_id,
                ChangeSetState.CONFLICT,
                error.envelope.message,
            )
        except OSError as error:
            return self._finish(
                change_set_id,
                ChangeSetState.UNCERTAIN,
                f"filesystem effect failed: {type(error).__name__}",
            )

    def reconcile(self, change_set_id: UUID) -> ChangeSetResult:
        row = self._change_row(change_set_id)
        if row.state not in {
            ChangeSetState.APPLYING.value,
            ChangeSetState.UNCERTAIN.value,
            ChangeSetState.ROLLBACK_PENDING.value,
        }:
            with Session(self._engine) as session:
                return self._result(session, str(change_set_id))
        return self.apply(change_set_id)

    def get(self, change_set_id: UUID) -> ChangeSetResult:
        with Session(self._engine) as session:
            return self._result(session, str(change_set_id))

    def list(self, *, offset: int = 0, limit: int = 100) -> tuple[ChangeSetResult, ...]:
        if offset < 0 or limit < 1 or limit > 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "change-set query bound is invalid")
        with Session(self._engine) as session:
            identifiers = session.scalars(
                select(ChangeSetRow.id)
                .order_by(ChangeSetRow.created_at, ChangeSetRow.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return tuple(self._result(session, identifier) for identifier in identifiers)

    def _recover_prepared(
        self,
        journal: ChangeOperationRow,
        operation: ChangeOperation,
        path: Path,
    ) -> str:
        actual = self._operation_token(operation, path)
        if actual == journal.expected_after_token:
            self._mark_applied(UUID(journal.change_set_id), journal.position, actual)
            return "applied"
        if self._token(path) == journal.before_token:
            return "retry"
        return "conflict"

    def _rollback_one(
        self,
        change_set_id: UUID,
        position: int,
        operation: ChangeOperation,
        path: Path,
        *,
        reason: str,
    ) -> ChangeSetResult:
        journal = self._journal(change_set_id, position)
        self._set_state(change_set_id, ChangeSetState.ROLLBACK_PENDING)
        if self._operation_token(operation, path) != journal.expected_after_token:
            return self._finish(change_set_id, ChangeSetState.CONFLICT, reason)
        if operation.rollback.value == "retain":
            return self._finish(change_set_id, ChangeSetState.UNCERTAIN, reason)
        if journal.preimage_reference is None:
            if path.exists() and not path.is_dir():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        else:
            self._atomic_write(path, self._artifacts.read_bytes(journal.preimage_reference))
        return self._finish(change_set_id, ChangeSetState.ROLLED_BACK, reason)

    def _apply_operation(
        self,
        operation: ChangeOperation,
        path: Path,
        content: bytes | None,
    ) -> None:
        kind = operation.kind
        if kind is ChangeOperationKind.MKDIR:
            path.mkdir()
        elif kind in {
            ChangeOperationKind.CREATE,
            ChangeOperationKind.WRITE,
            ChangeOperationKind.REPLACE,
            ChangeOperationKind.PATCH,
            ChangeOperationKind.REWRITE,
        }:
            assert content is not None
            self._atomic_write(path, content)
        elif kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            if destination.exists() or destination.is_symlink():
                raise MishkanError(ErrorCode.REVISION_MISMATCH, "change destination is not absent")
            if kind is ChangeOperationKind.MOVE:
                os.replace(path, destination)
            else:
                if path.is_dir():
                    shutil.copytree(path, destination, symlinks=False)
                else:
                    self._atomic_write(destination, path.read_bytes())
        elif kind is ChangeOperationKind.DELETE:
            path.rmdir() if path.is_dir() else path.unlink()

    def _expected_content(
        self,
        operation: ChangeOperation,
        path: Path,
        before: bytes | None,
    ) -> bytes | None:
        if operation.inline_content is not None:
            return operation.inline_content.encode()
        if operation.artifact_reference is not None:
            return self._artifacts.read_bytes(operation.artifact_reference)
        if operation.kind in {ChangeOperationKind.REPLACE, ChangeOperationKind.PATCH}:
            if before is None:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH, "exact replacement target is absent"
                )
            match = (operation.match or "").encode()
            replacement = (operation.replacement or "").encode()
            count = before.count(match)
            if count != operation.expected_occurrences:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "exact replacement occurrence count differs",
                    details={"expected": operation.expected_occurrences, "observed": count},
                )
            return before.replace(match, replacement)
        return None

    def _check_precondition(self, operation: ChangeOperation, path: Path) -> None:
        token = self._token(path)
        if operation.precondition is PreconditionKind.ABSENT:
            valid = token == "absent"
        elif operation.precondition is PreconditionKind.DIGEST:
            valid = token == operation.precondition_value
        elif operation.precondition is PreconditionKind.REVISION:
            valid = self._revision_token(path) == operation.precondition_value
        else:
            valid = self._git_blob(path) == operation.precondition_value
        if not valid:
            raise MishkanError(
                ErrorCode.REVISION_MISMATCH,
                "change operation precondition differs from real state",
                details={"path": operation.path, "observed": token},
            )

    def _safe_path(self, relative: str) -> Path:
        requested = Path(relative)
        if requested.is_absolute() or not requested.parts or ".." in requested.parts:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "change path escapes workspace")
        lexical = Path(os.path.abspath(self._workspace / requested))
        if not lexical.is_relative_to(self._workspace):
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "change path escapes workspace")
        current = self._workspace
        for part in requested.parts:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "change path contains a symbolic link",
                        details={"path": relative},
                    )
            except FileNotFoundError:
                continue
        return lexical

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=False, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        staged = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)

    @staticmethod
    def _read_file(path: Path) -> bytes | None:
        if not path.exists() or path.is_dir():
            return None
        return path.read_bytes()

    @staticmethod
    def _token(path: Path) -> str:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "absent"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink"
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _revision_token(path: Path) -> str:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "absent"
        return (
            f"posix:{metadata.st_dev}:{metadata.st_ino}:{metadata.st_size}:{metadata.st_mtime_ns}"
        )

    @staticmethod
    def _git_blob(path: Path) -> str:
        if not path.is_file():
            return "absent"
        completed = subprocess.run(
            ["git", "hash-object", "--", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if completed.returncode != 0:
            raise MishkanError(ErrorCode.EDIT, "Git blob precondition could not be observed")
        return completed.stdout.strip()

    def _expected_token(self, operation: ChangeOperation, path: Path, content: bytes | None) -> str:
        if operation.kind is ChangeOperationKind.MKDIR:
            return "directory"
        if operation.kind is ChangeOperationKind.DELETE:
            return "absent"
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            source_token = self._token(path)
            return json.dumps(
                {
                    "source": "absent"
                    if operation.kind is ChangeOperationKind.MOVE
                    else source_token,
                    "destination": source_token,
                    "destination_path": str(destination),
                },
                sort_keys=True,
            )
        assert content is not None
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    def _operation_token(self, operation: ChangeOperation, path: Path) -> str:
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            return json.dumps(
                {
                    "source": self._token(path),
                    "destination": self._token(destination),
                    "destination_path": str(destination),
                },
                sort_keys=True,
            )
        return self._token(path)

    def _preimage(
        self,
        change_set_id: UUID,
        position: int,
        path: Path,
        content: bytes | None,
    ) -> str | None:
        if content is None:
            return None
        manifest = self._artifacts.put_bytes(
            content,
            media_type="application/octet-stream",
            provenance=ArtifactProvenance(
                producer_identity="mishkand",
                run_id=str(change_set_id),
                task_attempt_id=f"change:{position}",
                call_id=f"preimage:{position}",
                capability="edit.apply",
                channel="preimage",
            ),
            complete=True,
            retention="change-set",
        )
        return manifest.reference

    def _diff_artifact(self, change_set_id: UUID, content: bytes) -> str:
        return self._artifacts.put_bytes(
            content,
            media_type="text/x-diff",
            provenance=ArtifactProvenance(
                producer_identity="mishkand",
                run_id=str(change_set_id),
                task_attempt_id="change-set",
                call_id="diff",
                capability="edit.apply",
                channel="diff",
            ),
            complete=True,
            retention="change-set",
        ).reference

    def _load(self, change_set_id: UUID) -> ChangeSet:
        with Session(self._engine) as session:
            row = session.get(ChangeSetRow, str(change_set_id))
            if row is None:
                raise MishkanError(ErrorCode.EDIT, "change set does not exist")
            return ChangeSet.model_validate_json(row.payload)

    def _change_row(self, change_set_id: UUID) -> ChangeSetRow:
        with Session(self._engine) as session:
            row = session.get(ChangeSetRow, str(change_set_id))
            if row is None:
                raise MishkanError(ErrorCode.EDIT, "change set does not exist")
            session.expunge(row)
            return row

    def _journal(self, change_set_id: UUID, position: int) -> ChangeOperationRow:
        with Session(self._engine) as session:
            row = session.get(ChangeOperationRow, (str(change_set_id), position))
            if row is None:
                raise MishkanError(ErrorCode.EDIT, "change operation journal does not exist")
            session.expunge(row)
            return row

    def _prepare(
        self,
        change_set_id: UUID,
        position: int,
        *,
        before_token: str,
        preimage_reference: str | None,
        expected_after_token: str,
    ) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.get(ChangeOperationRow, (str(change_set_id), position))
            assert row is not None
            row.state = "prepared"
            row.before_token = before_token
            row.preimage_reference = preimage_reference
            row.expected_after_token = expected_after_token
            row.updated_at = utc_now().isoformat()

    def _mark_applied(self, change_set_id: UUID, position: int, token: str) -> None:
        with Session(self._engine) as session, session.begin():
            operation = session.get(ChangeOperationRow, (str(change_set_id), position))
            change = session.get(ChangeSetRow, str(change_set_id))
            assert operation is not None and change is not None
            operation.state = "applied"
            operation.actual_after_token = token
            operation.updated_at = utc_now().isoformat()
            change.operation_index = max(change.operation_index, position + 1)
            change.revision += 1
            change.updated_at = utc_now().isoformat()

    def _set_state(self, change_set_id: UUID, state: ChangeSetState) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.get(ChangeSetRow, str(change_set_id))
            if row is None:
                raise MishkanError(ErrorCode.EDIT, "change set does not exist")
            row.state = state.value
            row.updated_at = utc_now().isoformat()

    def _finish(
        self,
        change_set_id: UUID,
        state: ChangeSetState,
        reason: str | None,
        *,
        diff_reference: str | None = None,
    ) -> ChangeSetResult:
        with Session(self._engine) as session, session.begin():
            row = session.get(ChangeSetRow, str(change_set_id))
            assert row is not None
            row.state = state.value
            row.reason = reason
            row.diff_reference = diff_reference or row.diff_reference
            row.revision += 1
            row.updated_at = utc_now().isoformat()
            session.flush()
            return self._result(session, str(change_set_id))

    @staticmethod
    def _result(session: Session, change_set_id: str) -> ChangeSetResult:
        row = session.get(ChangeSetRow, change_set_id)
        assert row is not None
        preimages = session.scalars(
            select(ChangeOperationRow.preimage_reference)
            .where(ChangeOperationRow.change_set_id == change_set_id)
            .order_by(ChangeOperationRow.position)
        ).all()
        return ChangeSetResult(
            change_set_id=change_set_id,
            state=ChangeSetState(row.state),
            completed_operations=row.operation_index,
            revision=row.revision,
            preimage_references=tuple(item for item in preimages if item is not None),
            diff_reference=row.diff_reference,
            reason=row.reason,
        )
