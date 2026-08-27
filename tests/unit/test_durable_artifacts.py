from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from mishkan.artifacts import (
    ArtifactLifecycle,
    ArtifactProvenance,
    ArtifactReconciliationAction,
)
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import SchemaManager


def _provenance() -> ArtifactProvenance:
    return ArtifactProvenance(
        producer_identity="engineer",
        run_id="run-1",
        task_attempt_id="attempt-1",
        call_id="call-1",
        capability="terminal.process",
        channel="stdout",
    )


def _service(tmp_path: Path) -> DurableArtifactService:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    return DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=1024,
        max_chunk_bytes=4,
    )


def _upload(service: DurableArtifactService, content: bytes) -> str:
    digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=digest,
        media_type="text/plain",
        provenance=_provenance(),
    )
    offset = 0
    for start in range(0, len(content), 4):
        chunk = content[start : start + 4]
        service.append_chunk(upload.upload_id, offset=offset, content=chunk)
        offset += len(chunk)
    return service.commit_upload(upload.upload_id).reference


def test_chunked_upload_is_invisible_until_verified_commit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    content = b"streamed"
    reference = _upload(service, content)

    assert service.read_bytes(reference) == content
    assert service.manifest(reference).lifecycle is ArtifactLifecycle.AVAILABLE


def test_upload_rejects_stale_offset_and_wrong_digest(tmp_path: Path) -> None:
    service = _service(tmp_path)
    upload = service.open_upload(
        expected_size=4,
        expected_digest=f"sha256:{'0' * 64}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    service.append_chunk(upload.upload_id, offset=0, content=b"data")

    with pytest.raises(MishkanError) as stale:
        service.append_chunk(upload.upload_id, offset=0, content=b"x")
    assert stale.value.envelope.code is ErrorCode.REVISION_MISMATCH

    with pytest.raises(MishkanError) as invalid:
        service.commit_upload(upload.upload_id)
    assert invalid.value.envelope.code is ErrorCode.ARTIFACT


def test_working_reference_requires_compare_and_swap(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _upload(service, b"one")
    second = _upload(service, b"two")

    current = service.update_reference("run:1", "latest", first, expected_revision=0)
    assert current.revision == 1
    with pytest.raises(MishkanError) as conflict:
        service.update_reference("run:1", "latest", second, expected_revision=0)
    assert conflict.value.envelope.code is ErrorCode.REVISION_MISMATCH
    assert service.update_reference("run:1", "latest", second, expected_revision=1).revision == 2


def test_collections_validate_paths_and_holds_root_gc(tmp_path: Path) -> None:
    service = _service(tmp_path)
    kept = _upload(service, b"keep")
    removed = _upload(service, b"drop")
    service.create_collection({"logs/output.txt": kept})
    with pytest.raises(MishkanError):
        service.create_collection({"../escape": kept})
    service.hold(kept, "incident evidence")

    plan = service.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert kept not in plan.candidates
    assert removed in plan.candidates
    applied = service.apply_gc(plan.plan_id)
    assert applied.applied
    assert service.manifest(removed).lifecycle is ArtifactLifecycle.TOMBSTONED
    assert service.read_bytes(kept) == b"keep"


def test_missing_body_is_persistently_classified(tmp_path: Path) -> None:
    service = _service(tmp_path)
    reference = _upload(service, b"gone")
    manifest = service.manifest(reference)
    blob = tmp_path / "artifacts" / "blobs" / manifest.storage_ref
    blob.unlink()

    with pytest.raises(MishkanError):
        service.read_bytes(reference)
    assert service.manifest(reference).lifecycle is ArtifactLifecycle.MISSING


def test_artifact_bounds_corruption_and_idempotent_commit_fail_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(MishkanError):
        service.put_bytes(
            b"partial",
            media_type="text/plain",
            provenance=_provenance(),
            complete=False,
        )
    with pytest.raises(MishkanError):
        service.open_upload(
            expected_size=1,
            expected_digest="invalid",
            media_type="text/plain",
            provenance=_provenance(),
        )
    upload = service.open_upload(
        expected_size=1,
        expected_digest=f"sha256:{hashlib.sha256(b'x').hexdigest()}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    with pytest.raises(MishkanError):
        service.append_chunk(upload.upload_id, offset=0, content=b"")
    with pytest.raises(MishkanError):
        service.append_chunk(upload.upload_id, offset=0, content=b"too-long")
    service.append_chunk(upload.upload_id, offset=0, content=b"x")
    first = service.commit_upload(upload.upload_id)
    assert service.commit_upload(upload.upload_id).reference == first.reference

    blob = service.body_path(first.reference)
    blob.write_bytes(b"y")
    with pytest.raises(MishkanError):
        service.read_bytes(first.reference)
    assert service.manifest(first.reference).lifecycle is ArtifactLifecycle.CORRUPT

    with pytest.raises(MishkanError):
        service.list_manifests(limit=0)
    with pytest.raises(MishkanError):
        service.manifest("not-an-artifact")


def test_reconciliation_plan_repairs_metadata_and_orphans_only_after_apply(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    reference = _upload(service, b"evidence")
    service.update_reference("run:1", "latest", reference, expected_revision=0)
    service.create_collection({"evidence/output.txt": reference})
    manifest = service.manifest(reference)
    blob = tmp_path / "artifacts" / "blobs" / manifest.storage_ref
    blob.unlink()
    orphan = tmp_path / "artifacts" / "blobs" / "sha256" / "aa" / ("b" * 62)
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")

    plan = service.plan_reconciliation()
    actions = {issue.action for issue in plan.issues}

    assert actions == {
        ArtifactReconciliationAction.MARK_MISSING,
        ArtifactReconciliationAction.DELETE_ORPHAN_BLOB,
        ArtifactReconciliationAction.DELETE_INVALID_REFERENCE,
        ArtifactReconciliationAction.DELETE_INCOMPLETE_COLLECTION,
    }
    assert orphan.exists()
    assert service.manifest(reference).lifecycle is ArtifactLifecycle.AVAILABLE

    applied = service.apply_reconciliation(plan.plan_id)

    assert applied.applied is True
    assert not orphan.exists()
    assert service.manifest(reference).lifecycle is ArtifactLifecycle.MISSING
    assert service.apply_reconciliation(plan.plan_id) == applied
    assert service.plan_reconciliation().issues == ()
