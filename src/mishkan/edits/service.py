"""Crash-recoverable, exact filesystem change-set application."""

from __future__ import annotations

import builtins
import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
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
    ChangeValidation,
    ChangeValidationKind,
    ChangeValidationResult,
    PreconditionKind,
)
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import ChangeOperationRow, ChangeSetRow, create_local_engine


class ChangeSetService:
    def __init__(
        self,
        database: Path,
        workspace: Path,
        artifacts: DurableArtifactService,
        *,
        after_effect_hook: Callable[[int], None] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        SchemaManager(database).require_current()
        self._workspace = workspace.resolve(strict=True)
        self._artifacts = artifacts
        self._after_effect_hook = after_effect_hook or (lambda _position: None)
        self._engine = create_local_engine(database, busy_timeout_ms=busy_timeout_ms)

    def plan(self, change_set: ChangeSet) -> ChangeSetResult:
        self._validate_scope(change_set)
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
                    validation_payload=None,
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
        self._validate_scope(change_set)
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
                self._check_destination_precondition(operation)
                after = self._expected_content(operation, path, before)
                preimage = self._preimage(change_set_id, position, path, before)
                expected_after = self._expected_state_token(operation, path, after)
                if (
                    operation.expected_digest is not None
                    and after is not None
                    and f"sha256:{hashlib.sha256(after).hexdigest()}" != operation.expected_digest
                ):
                    raise MishkanError(
                        ErrorCode.REVISION_MISMATCH,
                        "declared expected result differs from computed exact result",
                    )
                self._prepare(
                    change_set_id,
                    position,
                    before_token=self._state_token(path),
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
                diffs.extend(self._operation_diff(operation, before, after))
            self._set_state(change_set_id, ChangeSetState.APPLIED)
            diff_reference = self._diff_artifact(change_set_id, "".join(diffs).encode())
            validations = self._run_validations(change_set.validations)
            if not all(item.passed for item in validations):
                rollback = self._rollback_applied(change_set_id, change_set)
                return self._finish(
                    change_set_id,
                    rollback,
                    "one or more selected validations failed",
                    diff_reference=diff_reference,
                    validation_results=validations,
                )
            return self._finish(
                change_set_id,
                ChangeSetState.VERIFIED,
                None,
                diff_reference=diff_reference,
                validation_results=validations,
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

    def definition(self, change_set_id: UUID) -> ChangeSet:
        """Return the immutable planned contract for pre-dispatch authorization."""

        return self._load(change_set_id)

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
        if self._state_token(path) == journal.before_token:
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
        self._restore_operation(operation, path, journal)
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
            if operation.result_mode is not None:
                path.chmod(operation.result_mode)
        elif kind in {
            ChangeOperationKind.CREATE,
            ChangeOperationKind.WRITE,
            ChangeOperationKind.REPLACE,
            ChangeOperationKind.PATCH,
            ChangeOperationKind.REWRITE,
        }:
            assert content is not None
            self._atomic_write(path, content, mode=self._result_mode(operation, path))
        elif kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            if not path.is_file():
                raise MishkanError(
                    ErrorCode.OUTPUT_CONTRACT,
                    "move and copy currently require a regular-file source",
                )
            if destination.exists() or destination.is_symlink():
                raise MishkanError(ErrorCode.REVISION_MISMATCH, "change destination is not absent")
            if kind is ChangeOperationKind.MOVE:
                os.replace(path, destination)
            else:
                self._atomic_write(
                    destination,
                    path.read_bytes(),
                    mode=operation.result_mode or stat.S_IMODE(path.stat().st_mode),
                )
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
        if operation.kind is ChangeOperationKind.PATCH:
            if before is None or operation.patch is None:
                raise MishkanError(ErrorCode.REVISION_MISMATCH, "exact patch target is absent")
            return self._apply_unified_patch(before, operation.patch, operation.path)
        if operation.kind is ChangeOperationKind.REPLACE:
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

    @staticmethod
    def _operation_diff(
        operation: ChangeOperation, before: bytes | None, after: bytes | None
    ) -> builtins.list[str]:
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            return [f"# {operation.kind.value}: {operation.path} -> {operation.destination}\n"]
        if operation.kind is ChangeOperationKind.MKDIR:
            return [f"# mkdir: {operation.path}\n"]
        old = (before or b"").decode(errors="replace").splitlines(keepends=True)
        new = (after or b"").decode(errors="replace").splitlines(keepends=True)
        if operation.kind is ChangeOperationKind.DELETE:
            new = []
        return list(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{operation.path}",
                tofile=f"b/{operation.path}",
            )
        )

    def _run_validations(
        self, validations: tuple[ChangeValidation, ...]
    ) -> tuple[ChangeValidationResult, ...]:
        results: list[ChangeValidationResult] = []
        for validation in validations:
            path = self._safe_path(validation.path)
            if validation.kind is ChangeValidationKind.EXISTS:
                observed: str | int = self._token(path)
                passed = observed != "absent"
            elif validation.kind is ChangeValidationKind.ABSENT:
                observed = self._token(path)
                passed = observed == "absent"
            elif validation.kind is ChangeValidationKind.DIGEST:
                observed = self._token(path)
                passed = observed == validation.expected_value
            else:
                try:
                    observed = stat.S_IMODE(path.lstat().st_mode)
                except FileNotFoundError:
                    observed = "absent"
                passed = observed == validation.expected_value
            results.append(
                ChangeValidationResult(
                    validation_id=str(validation.id),
                    kind=validation.kind,
                    path=validation.path,
                    passed=passed,
                    expected=validation.expected_value,
                    observed=observed,
                )
            )
        return tuple(results)

    def _rollback_applied(self, change_set_id: UUID, change_set: ChangeSet) -> ChangeSetState:
        self._set_state(change_set_id, ChangeSetState.ROLLBACK_PENDING)
        for position in range(len(change_set.operations) - 1, -1, -1):
            operation = change_set.operations[position]
            journal = self._journal(change_set_id, position)
            if journal.state != "applied":
                continue
            if operation.rollback.value == "retain":
                return ChangeSetState.UNCERTAIN
            path = self._safe_path(operation.path)
            if self._operation_token(operation, path) != journal.expected_after_token:
                return ChangeSetState.CONFLICT
            self._restore_operation(operation, path, journal)
            self._mark_rolled_back(change_set_id, position)
        return ChangeSetState.ROLLED_BACK

    def _restore_operation(
        self, operation: ChangeOperation, path: Path, journal: ChangeOperationRow
    ) -> None:
        before = self._decode_state(journal.before_token)
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            if operation.kind is ChangeOperationKind.MOVE:
                os.replace(destination, path)
            elif destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
            return
        if journal.preimage_reference is not None:
            mode = before.get("mode")
            self._atomic_write(
                path,
                self._artifacts.read_bytes(journal.preimage_reference),
                mode=int(mode) if isinstance(mode, int) else None,
            )
            return
        if before.get("kind") == "directory":
            path.mkdir()
            mode = before.get("mode")
            if isinstance(mode, int):
                path.chmod(mode)
            return
        if path.is_dir():
            path.rmdir()
        elif path.exists():
            path.unlink()

    @staticmethod
    def _decode_state(token: str | None) -> dict[str, object]:
        if token in {None, "absent", "symlink"}:
            return {"kind": token or "unknown"}
        assert token is not None
        try:
            value = json.loads(token)
        except json.JSONDecodeError:
            return {"kind": "unknown"}
        return value if isinstance(value, dict) else {"kind": "unknown"}

    @staticmethod
    def _apply_unified_patch(before: bytes, patch: str, logical_path: str) -> bytes:
        try:
            source = before.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise MishkanError(
                ErrorCode.EDIT, "unified patch target must be valid UTF-8 text"
            ) from exc
        lines = patch.splitlines(keepends=True)
        if len(lines) < 3 or not lines[0].startswith("--- ") or not lines[1].startswith("+++ "):
            raise MishkanError(ErrorCode.EDIT, "unified patch requires exact file headers")
        ChangeSetService._require_patch_path(lines[0][4:], logical_path)
        ChangeSetService._require_patch_path(lines[1][4:], logical_path)
        hunk = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?$")
        output: list[str] = []
        cursor = 0
        index = 2
        hunks = 0
        while index < len(lines):
            header = lines[index].rstrip("\r\n")
            match = hunk.fullmatch(header)
            if match is None:
                raise MishkanError(ErrorCode.EDIT, "unified patch contains data outside a hunk")
            hunks += 1
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_count = int(match.group(4) or "1")
            if old_start < 1 or old_start - 1 < cursor or old_start - 1 > len(source):
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH, "unified patch hunk position differs"
                )
            output.extend(source[cursor : old_start - 1])
            cursor = old_start - 1
            consumed_old = 0
            produced_new = 0
            index += 1
            while index < len(lines) and not lines[index].startswith("@@ "):
                line = lines[index]
                if not line or line[0] not in {" ", "+", "-"}:
                    raise MishkanError(ErrorCode.EDIT, "unified patch hunk line is invalid")
                prefix = line[0]
                value = line[1:]
                if index + 1 < len(lines) and lines[index + 1].rstrip("\r\n") == (
                    "\\ No newline at end of file"
                ):
                    value = value.rstrip("\r\n")
                    index += 1
                if prefix in {" ", "-"}:
                    if cursor >= len(source) or source[cursor] != value:
                        raise MishkanError(
                            ErrorCode.REVISION_MISMATCH,
                            "unified patch context differs from the exact target",
                        )
                    cursor += 1
                    consumed_old += 1
                if prefix in {" ", "+"}:
                    output.append(value)
                    produced_new += 1
                index += 1
            if consumed_old != old_count or produced_new != new_count:
                raise MishkanError(ErrorCode.EDIT, "unified patch hunk counts are inconsistent")
        if hunks == 0:
            raise MishkanError(ErrorCode.EDIT, "unified patch contains no hunks")
        output.extend(source[cursor:])
        return "".join(output).encode("utf-8")

    @staticmethod
    def _require_patch_path(header: str, logical_path: str) -> None:
        candidate = header.rstrip("\r\n").split("\t", 1)[0]
        if candidate.startswith(("a/", "b/")):
            candidate = candidate[2:]
        if candidate != logical_path:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "unified patch header differs from the authorized logical path",
            )

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

    def _check_destination_precondition(self, operation: ChangeOperation) -> None:
        if operation.destination is None or operation.destination_precondition is None:
            return
        destination = self._safe_path(operation.destination)
        shadow = operation.model_copy(
            update={
                "path": operation.destination,
                "destination": None,
                "precondition": operation.destination_precondition,
                "precondition_value": operation.destination_precondition_value,
                "destination_precondition": None,
                "destination_precondition_value": None,
            }
        )
        self._check_precondition(shadow, destination)

    def _validate_scope(self, change_set: ChangeSet) -> None:
        requested = {
            value
            for operation in change_set.operations
            for value in (operation.path, operation.destination)
            if value is not None
        }
        requested.update(validation.path for validation in change_set.validations)
        for path in requested:
            self._safe_path(path)
        normalized_scopes = tuple(Path(item) for item in change_set.path_scopes)
        deviations = tuple(
            sorted(
                path
                for path in requested
                if not any(
                    scope == Path(".") or Path(path) == scope or Path(path).is_relative_to(scope)
                    for scope in normalized_scopes
                )
            )
        )
        if deviations:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "change paths exceed the declared change-set scopes",
                details={"scope_deviations": list(deviations)},
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
    def _atomic_write(path: Path, content: bytes, *, mode: int | None = None) -> None:
        path.parent.mkdir(parents=False, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        staged = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if mode is not None:
                    os.fchmod(stream.fileno(), mode)
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

    @classmethod
    def _state_token(cls, path: Path) -> str:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            return "absent"
        if stat.S_ISLNK(metadata.st_mode):
            return "symlink"
        kind = "directory" if stat.S_ISDIR(metadata.st_mode) else "file"
        payload: dict[str, str | int] = {
            "kind": kind,
            "mode": stat.S_IMODE(metadata.st_mode),
        }
        if kind == "file":
            payload["digest"] = cls._token(path)
        else:
            entries = sorted(item.name for item in path.iterdir())
            payload["entries"] = json.dumps(entries[:1_001], separators=(",", ":"))
        return json.dumps(payload, sort_keys=True)

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

    def _expected_state_token(
        self, operation: ChangeOperation, path: Path, content: bytes | None
    ) -> str:
        if operation.kind is ChangeOperationKind.MKDIR:
            return json.dumps(
                {
                    "entries": "[]",
                    "kind": "directory",
                    "mode": self._result_mode(operation, path),
                },
                sort_keys=True,
            )
        if operation.kind is ChangeOperationKind.DELETE:
            return "absent"
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            source_token = self._state_token(path)
            destination_token = source_token
            if operation.kind is ChangeOperationKind.COPY and operation.result_mode is not None:
                decoded = self._decode_state(source_token)
                decoded["mode"] = operation.result_mode
                destination_token = json.dumps(decoded, sort_keys=True)
            return json.dumps(
                {
                    "source": "absent"
                    if operation.kind is ChangeOperationKind.MOVE
                    else source_token,
                    "destination": destination_token,
                    "destination_path": str(destination),
                },
                sort_keys=True,
            )
        assert content is not None
        return json.dumps(
            {
                "digest": f"sha256:{hashlib.sha256(content).hexdigest()}",
                "kind": "file",
                "mode": self._result_mode(operation, path),
            },
            sort_keys=True,
        )

    def _operation_token(self, operation: ChangeOperation, path: Path) -> str:
        if operation.kind in {ChangeOperationKind.MOVE, ChangeOperationKind.COPY}:
            assert operation.destination is not None
            destination = self._safe_path(operation.destination)
            return json.dumps(
                {
                    "source": self._state_token(path),
                    "destination": self._state_token(destination),
                    "destination_path": str(destination),
                },
                sort_keys=True,
            )
        return self._state_token(path)

    @staticmethod
    def _result_mode(operation: ChangeOperation, path: Path) -> int:
        if operation.result_mode is not None:
            return operation.result_mode
        try:
            return stat.S_IMODE(path.lstat().st_mode)
        except FileNotFoundError:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "a newly created path has no declared result mode",
            ) from None

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

    def _mark_rolled_back(self, change_set_id: UUID, position: int) -> None:
        with Session(self._engine) as session, session.begin():
            operation = session.get(ChangeOperationRow, (str(change_set_id), position))
            change = session.get(ChangeSetRow, str(change_set_id))
            assert operation is not None and change is not None
            operation.state = "rolled_back"
            operation.updated_at = utc_now().isoformat()
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
        validation_results: tuple[ChangeValidationResult, ...] | None = None,
    ) -> ChangeSetResult:
        with Session(self._engine) as session, session.begin():
            row = session.get(ChangeSetRow, str(change_set_id))
            assert row is not None
            row.state = state.value
            row.reason = reason
            row.diff_reference = diff_reference or row.diff_reference
            if validation_results is not None:
                row.validation_payload = json.dumps(
                    [item.model_dump(mode="json") for item in validation_results],
                    sort_keys=True,
                )
            row.revision += 1
            row.updated_at = utc_now().isoformat()
            session.flush()
            return self._result(session, str(change_set_id))

    @staticmethod
    def _result(session: Session, change_set_id: str) -> ChangeSetResult:
        row = session.get(ChangeSetRow, change_set_id)
        if row is None:
            raise MishkanError(ErrorCode.EDIT, "change set does not exist")
        preimages = session.scalars(
            select(ChangeOperationRow.preimage_reference)
            .where(ChangeOperationRow.change_set_id == change_set_id)
            .order_by(ChangeOperationRow.position)
        ).all()
        journals = session.scalars(
            select(ChangeOperationRow)
            .where(ChangeOperationRow.change_set_id == change_set_id)
            .order_by(ChangeOperationRow.position)
        ).all()
        definition = ChangeSet.model_validate_json(row.payload)
        changed_paths: set[str] = set()
        for journal, operation in zip(journals, definition.operations, strict=True):
            if journal.state not in {"applied", "rolled_back"}:
                continue
            changed_paths.add(operation.path)
            if operation.destination is not None:
                changed_paths.add(operation.destination)
        validations = (
            tuple(
                ChangeValidationResult.model_validate(item)
                for item in json.loads(row.validation_payload)
            )
            if row.validation_payload is not None
            else ()
        )
        return ChangeSetResult(
            change_set_id=change_set_id,
            state=ChangeSetState(row.state),
            completed_operations=row.operation_index,
            revision=row.revision,
            preimage_references=tuple(item for item in preimages if item is not None),
            diff_reference=row.diff_reference,
            changed_paths=tuple(sorted(changed_paths)),
            scope_deviations=(),
            validation_results=validations,
            reason=row.reason,
        )
