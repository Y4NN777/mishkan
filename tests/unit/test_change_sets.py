from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from mishkan.artifacts.service import DurableArtifactService
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
