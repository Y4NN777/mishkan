from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import MishkanError
from mishkan.edits import (
    ChangeOperation,
    ChangeOperationKind,
    ChangeSet,
    ChangeSetService,
    ChangeSetState,
    PreconditionKind,
)
from mishkan.persistence import SchemaManager


def _services(
    tmp_path: Path,
    *,
    hook=None,  # type: ignore[no-untyped-def]
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


def test_ambiguous_replace_is_a_conflict_without_mutation(tmp_path: Path) -> None:
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
                match="x",
                replacement="y",
                expected_occurrences=1,
            ),
        ),
    )
    service.plan(change)

    assert service.apply(change.id).state is ChangeSetState.CONFLICT
    assert target.read_text() == "x x"


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
    service.plan(symlink_change)
    assert service.apply(symlink_change.id).state is ChangeSetState.CONFLICT
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
            ),
            ChangeOperation(
                kind=ChangeOperationKind.CREATE,
                path="tree/a.txt",
                precondition=PreconditionKind.ABSENT,
                inline_content="A",
            ),
            ChangeOperation(
                kind=ChangeOperationKind.COPY,
                path="tree/a.txt",
                destination="tree/b.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
            ),
            ChangeOperation(
                kind=ChangeOperationKind.MOVE,
                path="tree/b.txt",
                destination="tree/c.txt",
                precondition=PreconditionKind.DIGEST,
                precondition_value=_digest(b"A"),
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
