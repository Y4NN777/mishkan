from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

import pytest

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.edits import (
    ChangeOperation,
    ChangeOperationKind,
    ChangeSet,
    ChangeSetService,
    ChangeSetState,
    ChangeValidation,
    ChangeValidationKind,
    PreconditionKind,
)
from mishkan.persistence import SchemaManager
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


def _services(
    tmp_path: Path,
    *,
    hook: Callable[[int], None] | None = None,
) -> tuple[ChangeSetService, DurableArtifactService, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    artifacts = DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=1024 * 1024,
        max_chunk_bytes=1024,
    )
    return (
        ChangeSetService(database, workspace, artifacts, after_effect_hook=hook),
        artifacts,
        workspace,
    )


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def test_create_and_exact_replace_are_verified_and_journaled(tmp_path: Path) -> None:
    service, artifacts, workspace = _services(tmp_path)
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="app.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="hello world",
                result_mode=0o640,
                expected_digest=_digest(b"hello world"),
            ),
            ChangeOperation(
                kind=ChangeOperationKind.REPLACE,
                path="app.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"hello world"),
                match="world",
                replacement="MISHKAN",
                expected_occurrences=1,
                expected_digest=_digest(b"hello MISHKAN"),
            ),
        ),
    )
    service.plan(change)
    result = service.apply(change.id)

    assert result.state is ChangeSetState.VERIFIED
    assert result.completed_operations == 2
    assert result.diff_reference is not None
    assert b"MISHKAN" in artifacts.read_bytes(result.diff_reference)
    assert (workspace / "app.txt").read_text() == "hello MISHKAN"


@pytest.mark.secrets
def test_secret_like_inline_change_is_blocked_before_journal_persistence(
    tmp_path: Path,
) -> None:
    _service, artifacts, workspace = _services(tmp_path)
    inspector = ContentInspector(
        InspectionProfileLoader().load(
            "package://mishkan.resources.inspection/default-security.yaml",
            tmp_path,
        )
    )
    service = ChangeSetService(
        tmp_path / "mishkan.db",
        workspace,
        artifacts,
        content_inspector=inspector,
    )
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="secret.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="api_key=must-not-persist",
                result_mode=0o600,
                expected_digest=_digest(b"api_key=must-not-persist"),
            ),
        ),
    )

    with pytest.raises(MishkanError) as caught:
        service.plan(change)

    assert caught.value.envelope.code is ErrorCode.SECRET_CONTENT
    assert service.list() == ()


def test_unified_patch_with_stale_context_is_a_conflict_without_mutation(tmp_path: Path) -> None:
    service, _artifacts, workspace = _services(tmp_path)
    target = workspace / "app.txt"
    target.write_text("x x")
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.PATCH,
                path="app.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"x x"),
                patch="--- a/app.txt\n+++ b/app.txt\n@@ -1 +1 @@\n-x y\n+y y\n",
            ),
        ),
    )
    service.plan(change)

    assert service.apply(change.id).state is ChangeSetState.CONFLICT
    assert target.read_text() == "x x"


def test_exact_unified_patch_applies_multiple_hunks_without_fuzzy_matching(
    tmp_path: Path,
) -> None:
    service, artifacts, workspace = _services(tmp_path)
    target = workspace / "app.txt"
    target.write_text("one\ntwo\nthree\nfour\n")
    expected = b"ONE\ntwo\nthree\nFOUR\n"
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.PATCH,
                path="app.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(target.read_bytes()),
                patch=(
                    "--- a/app.txt\n"
                    "+++ b/app.txt\n"
                    "@@ -1,2 +1,2 @@\n"
                    "-one\n"
                    "+ONE\n"
                    " two\n"
                    "@@ -3,2 +3,2 @@\n"
                    " three\n"
                    "-four\n"
                    "+FOUR\n"
                ),
                expected_digest=_digest(expected),
            ),
        ),
    )

    service.plan(change)
    result = service.apply(change.id)

    assert result.state is ChangeSetState.VERIFIED
    assert target.read_bytes() == expected
    assert result.diff_reference is not None
    assert b"+FOUR" in artifacts.read_bytes(result.diff_reference)


def test_crash_after_effect_is_reconciled_without_reapplying(tmp_path: Path) -> None:
    crashed = False

    def hook(_position: int) -> None:
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("fault injection")

    service, artifacts, workspace = _services(tmp_path, hook=hook)
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="once.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="once",
                result_mode=0o600,
            ),
        ),
    )
    service.plan(change)
    with pytest.raises(RuntimeError):
        service.apply(change.id)
    assert (workspace / "once.txt").read_text() == "once"

    recovered = ChangeSetService(tmp_path / "mishkan.db", workspace, artifacts).reconcile(change.id)
    assert recovered.state is ChangeSetState.VERIFIED
    assert recovered.completed_operations == 1


def test_symlink_and_concurrent_after_state_are_never_overwritten(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    def concurrent(_position: int) -> None:
        (tmp_path / "workspace" / "target.txt").write_text("concurrent")

    service, _artifacts, workspace = _services(tmp_path, hook=concurrent)
    (workspace / "link").symlink_to(outside)
    target = workspace / "target.txt"
    target.write_text("before")
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.WRITE,
                path="target.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"before"),
                inline_content="after",
            ),
        ),
    )
    service.plan(change)
    result = service.apply(change.id)
    assert result.state is ChangeSetState.CONFLICT
    assert target.read_text() == "concurrent"

    symlink_change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.delete",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.DELETE,
                path="link",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"outside"),
            ),
        ),
    )
    with pytest.raises(MishkanError):
        service.plan(symlink_change)
    assert outside.read_text() == "outside"


def test_all_structural_operations_and_artifact_content_are_exact(tmp_path: Path) -> None:
    service, artifacts, workspace = _services(tmp_path)
    content_reference = artifacts.put_bytes(
        b"artifact-content",
        media_type="text/plain",
        provenance=ArtifactProvenance(
            producer_identity="engineer",
            run_id="run-1",
            task_attempt_id="task-1",
            call_id="call-1",
            capability="edit.apply",
            channel="input",
        ),
        complete=True,
    ).reference
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.write", "filesystem.delete"),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.MKDIR,
                path="tree",
                precondition=PreconditionKind.ABSENT,
                result_mode=0o750,
            ),
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="tree/a.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="A",
                result_mode=0o640,
            ),
            ChangeOperation(
                kind=ChangeOperationKind.COPY,
                path="tree/a.txt",
                destination="tree/b.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
                destination_precondition=PreconditionKind.ABSENT,
            ),
            ChangeOperation(
                kind=ChangeOperationKind.MOVE,
                path="tree/b.txt",
                destination="tree/c.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
                destination_precondition=PreconditionKind.ABSENT,
            ),
            ChangeOperation(
                kind=ChangeOperationKind.WRITE,
                path="tree/a.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
                inline_content="B",
            ),
            ChangeOperation(
                kind=ChangeOperationKind.REWRITE,
                path="tree/a.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"B"),
                artifact_reference=content_reference,
                rewrite_engine="fixture",
                rewrite_version="1.0",
                rewrite_rule="replace-exact-content",
                rewrite_language="text",
                rewrite_scope="single-file",
                rewrite_matches=1,
                rewrite_formatting="exact-replacement",
                rewrite_limits={"files": 1, "bytes": 1024},
            ),
            ChangeOperation(
                kind=ChangeOperationKind.DELETE,
                path="tree/c.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
            ),
        ),
    )

    assert service.plan(change).state is ChangeSetState.PLANNED
    assert service.plan(change).state is ChangeSetState.PLANNED
    result = service.apply(change.id)

    assert result.state is ChangeSetState.VERIFIED
    assert result.completed_operations == len(change.operations)
    assert (workspace / "tree" / "a.txt").read_bytes() == b"artifact-content"
    assert not (workspace / "tree" / "b.txt").exists()
    assert not (workspace / "tree" / "c.txt").exists()
    observed = service.get(change.id)
    assert observed.model_dump(exclude={"id", "created_at"}) == result.model_dump(
        exclude={"id", "created_at"}
    )
    assert service.list()[0].change_set_id == str(change.id)
    assert service.reconcile(change.id).state is ChangeSetState.VERIFIED


def test_change_set_queries_and_missing_state_fail_closed(tmp_path: Path) -> None:
    service, _artifacts, _workspace = _services(tmp_path)
    with pytest.raises(MishkanError):
        service.list(limit=0)
    with pytest.raises(MishkanError):
        service.get(uuid4())
    with pytest.raises(MishkanError):
        service.apply(uuid4())


def test_scopes_modes_and_selected_validation_are_enforced_and_durable(tmp_path: Path) -> None:
    service, _artifacts, workspace = _services(tmp_path)
    target = workspace / "src" / "app.txt"
    target.parent.mkdir()
    target.write_text("before")
    target.chmod(0o640)

    outside_scope = ChangeSet(
        scope="source-only",
        path_scopes=("src",),
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="other.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="forbidden",
                result_mode=0o600,
            ),
        ),
    )
    with pytest.raises(MishkanError):
        service.plan(outside_scope)
    assert not (workspace / "other.txt").exists()

    change = ChangeSet(
        scope="source-only",
        path_scopes=("src",),
        declared_effects=("filesystem.write",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.WRITE,
                path="src/app.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"before"),
                inline_content="after",
            ),
        ),
        validations=(
            ChangeValidation(
                kind=ChangeValidationKind.DIGEST,
                path="src/app.txt",
                expected_value=_digest(b"wrong"),
            ),
        ),
    )
    service.plan(change)
    result = service.apply(change.id)

    assert result.state is ChangeSetState.ROLLED_BACK
    assert result.validation_results[0].passed is False
    assert result.changed_paths == ("src/app.txt",)
    assert target.read_text() == "before"
    assert target.stat().st_mode & 0o777 == 0o640
    assert service.get(change.id).validation_results == result.validation_results


def test_move_crash_recovery_uses_both_path_identities_without_replay(tmp_path: Path) -> None:
    crashed = False

    def hook(position: int) -> None:
        nonlocal crashed
        if position == 0 and not crashed:
            crashed = True
            raise RuntimeError("fault injection")

    service, artifacts, workspace = _services(tmp_path, hook=hook)
    source = workspace / "source.txt"
    source.write_text("content")
    source.chmod(0o640)
    change = ChangeSet(
        scope="workspace",
        declared_effects=("filesystem.move",),
        operations=(
            ChangeOperation(
                kind=ChangeOperationKind.MOVE,
                path="source.txt",
                destination="destination.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"content"),
                destination_precondition=PreconditionKind.ABSENT,
            ),
        ),
    )
    service.plan(change)
    with pytest.raises(RuntimeError):
        service.apply(change.id)

    recovered = ChangeSetService(tmp_path / "mishkan.db", workspace, artifacts).reconcile(change.id)
    assert recovered.state is ChangeSetState.VERIFIED
    assert not source.exists()
    assert (workspace / "destination.txt").read_text() == "content"
    assert (workspace / "destination.txt").stat().st_mode & 0o777 == 0o640
