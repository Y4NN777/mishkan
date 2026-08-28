from __future__ import annotations

import hashlib
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from mishkan.artifacts import (
    ArtifactAvailability,
    ArtifactFactState,
    ArtifactLifecycle,
    ArtifactProvenance,
    ArtifactReconciliationAction,
    ArtifactReconciliationIssue,
)
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import SchemaManager
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader


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
    manifest = service.manifest(reference)
    assert manifest.lifecycle is ArtifactLifecycle.AVAILABLE
    assert manifest.detected_media_type is None
    assert manifest.facts.integrity is ArtifactFactState.PASSED
    assert manifest.facts.availability is ArtifactAvailability.AVAILABLE
    assert manifest.facts.authorization == "contextual_policy_required"


def test_stream_uses_one_verified_no_follow_descriptor_during_path_replacement(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    content = b"immutable-stream"
    reference = _upload(service, content)
    blob = service.body_path(reference)
    stream = service.iter_bytes(reference, chunk_size=4)

    first = next(stream)
    blob.unlink()
    blob.write_bytes(b"replaced")
    observed = first + b"".join(stream)

    assert observed == content
    with pytest.raises(MishkanError) as caught:
        service.read_bytes(reference)
    assert caught.value.envelope.code is ErrorCode.ARTIFACT
    assert service.manifest(reference).lifecycle is ArtifactLifecycle.CORRUPT


def test_security_scan_blocks_secret_split_across_upload_chunks(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    inspector = ContentInspector(
        InspectionProfileLoader().load(
            "package://mishkan.resources.inspection/default-security.yaml",
            tmp_path,
        )
    )
    service = DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=1_024,
        max_chunk_bytes=8,
        content_inspector=inspector,
    )
    content = b"api_key=split-across-chunks"
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    for offset in range(0, len(content), 8):
        service.append_chunk(
            upload.upload_id,
            offset=offset,
            content=content[offset : offset + 8],
        )

    with pytest.raises(MishkanError) as caught:
        service.commit_upload(upload.upload_id)

    assert caught.value.envelope.code is ErrorCode.SECRET_CONTENT
    assert service.list_manifests() == ()
    assert service.upload(upload.upload_id).lifecycle == "aborted"
    staged = tmp_path / "artifacts" / "staging" / f"{upload.upload_id}.upload"
    assert not staged.exists()


@pytest.mark.symlinks
def test_commit_refuses_an_in_root_staging_symlink(tmp_path: Path) -> None:
    service = _service(tmp_path)
    content = b"x"
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    staging = tmp_path / "artifacts" / "staging"
    staged = staging / f"{upload.upload_id}.upload"
    substitute = staging / "substitute.upload"
    substitute.write_bytes(content)
    staged.unlink()
    staged.symlink_to(substitute)

    with pytest.raises(MishkanError) as caught:
        service.commit_upload(upload.upload_id)

    assert caught.value.envelope.code is ErrorCode.ARTIFACT
    assert service.list_manifests() == ()
    assert substitute.read_bytes() == content


def test_commit_recovers_after_cas_publication_preceded_database_finalize(
    tmp_path: Path,
) -> None:
    service = _service(tmp_path)
    content = b"recoverable"
    digest = hashlib.sha256(content).hexdigest()
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{digest}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    for offset in range(0, len(content), 4):
        service.append_chunk(
            upload.upload_id,
            offset=offset,
            content=content[offset : offset + 4],
        )
    staged = tmp_path / "artifacts" / "staging" / f"{upload.upload_id}.upload"
    destination = tmp_path / "artifacts" / "blobs" / "sha256" / digest[:2] / digest[2:]
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)
    staged.unlink()

    manifest = service.commit_upload(upload.upload_id)

    assert manifest.digest == f"sha256:{digest}"
    assert service.read_bytes(manifest.reference) == content
    assert service.commit_upload(upload.upload_id) == manifest


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


def test_interrupted_upload_is_resumable_or_explicitly_aborted(tmp_path: Path) -> None:
    service = _service(tmp_path)
    content = b"resume"
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="text/plain",
        provenance=_provenance(),
    )
    service.append_chunk(upload.upload_id, offset=0, content=content[:4])
    assert service.upload(upload.upload_id).offset == 4
    assert service.abort_upload(upload.upload_id).lifecycle == "aborted"
    with pytest.raises(MishkanError):
        service.append_chunk(upload.upload_id, offset=4, content=content[4:])


def test_working_reference_requires_compare_and_swap(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _upload(service, b"one")
    second = _upload(service, b"two")

    current = service.update_reference("run:1", "latest", first, expected_revision=0)
    assert current.revision == 1
    assert current.prior_artifact_reference is None
    with pytest.raises(MishkanError) as conflict:
        service.update_reference("run:1", "latest", second, expected_revision=0)
    assert conflict.value.envelope.code is ErrorCode.REVISION_MISMATCH
    updated = service.update_reference("run:1", "latest", second, expected_revision=1)
    assert updated.revision == 2
    assert updated.prior_artifact_reference == first
    assert updated.prior_revision == 1
    assert service.list_references() == (updated,)


def test_collections_validate_paths_and_holds_root_gc(tmp_path: Path) -> None:
    service = _service(tmp_path)
    kept = _upload(service, b"keep")
    removed = _upload(service, b"drop")
    collection = service.create_collection({"logs/output.txt": kept})
    assert collection.ordered_paths == ("logs/output.txt",)
    assert service.collection(collection.collection_id) == collection
    assert service.list_collections() == (collection,)
    with pytest.raises(MishkanError):
        service.create_collection({"../escape": kept})
    hold = service.hold(kept, "incident evidence")
    assert service.list_holds() == (hold,)

    plan = service.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert kept not in plan.candidates
    assert removed in plan.candidates
    applied = service.apply_gc(plan.plan_id)
    assert applied.applied
    assert service.manifest(removed).lifecycle is ArtifactLifecycle.DELETED
    assert service.manifest(removed).facts.availability is ArtifactAvailability.UNAVAILABLE
    assert service.read_bytes(kept) == b"keep"

    assert service.release_hold(kept) == hold
    assert service.list_holds() == ()


def test_pinned_derivation_roots_its_source_until_pin_release(tmp_path: Path) -> None:
    service = _service(tmp_path)
    source = _upload(service, b"source")
    derived_content = b"preview"
    provenance = _provenance().model_copy(
        update={
            "source_artifacts": (source,),
            "engine": "preview-engine",
            "engine_version": "1.0",
            "configuration_fingerprint": "c" * 64,
            "declared_loss": "text-only preview",
        }
    )
    upload = service.open_upload(
        expected_size=len(derived_content),
        expected_digest=f"sha256:{hashlib.sha256(derived_content).hexdigest()}",
        media_type="text/plain",
        provenance=provenance,
    )
    service.append_chunk(upload.upload_id, offset=0, content=derived_content[:4])
    service.append_chunk(upload.upload_id, offset=4, content=derived_content[4:])
    derived = service.commit_upload(upload.upload_id).reference

    pin = service.pin(derived)
    assert service.list_pins() == (pin,)
    rooted = service.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert source not in rooted.candidates
    assert derived not in rooted.candidates

    assert service.release_pin(derived) == pin
    unrooted = service.plan_gc(watermark=utc_now() + timedelta(seconds=1))
    assert set(unrooted.candidates) == {source, derived}


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


def test_expired_staging_upload_requires_reconciliation_plan_then_apply(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    service = DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=1_024,
        max_chunk_bytes=64,
        staging_ttl_seconds=60,
    )
    upload = service.open_upload(
        expected_size=1,
        expected_digest="sha256:" + hashlib.sha256(b"x").hexdigest(),
        media_type="text/plain",
        provenance=_provenance(),
    )
    staged = tmp_path / "artifacts" / "staging" / f"{upload.upload_id}.upload"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE artifact_uploads SET updated_at = ? WHERE id = ?",
            ((utc_now() - timedelta(minutes=2)).isoformat(), str(upload.upload_id)),
        )

    plan = service.plan_reconciliation()

    assert plan.issues == (
        ArtifactReconciliationIssue(
            action=ArtifactReconciliationAction.ABORT_EXPIRED_UPLOAD,
            upload_id=upload.upload_id,
        ),
    )
    assert service.upload(upload.upload_id).lifecycle == "staging"
    assert staged.exists()

    service.apply_reconciliation(plan.plan_id)

    assert service.upload(upload.upload_id).lifecycle == "aborted"
    assert not staged.exists()
