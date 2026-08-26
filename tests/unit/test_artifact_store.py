from __future__ import annotations

from pathlib import Path

import pytest

from mishkan.artifacts import (
    ArtifactLifecycle,
    ArtifactProvenance,
    ArtifactValidation,
    FilesystemArtifactStore,
)
from mishkan.domain.errors import ErrorCode, MishkanError


def provenance(channel: str = "stdout") -> ArtifactProvenance:
    return ArtifactProvenance(
        producer_identity="role:Engineer",
        run_id="run-1",
        task_attempt_id="task:1",
        call_id="call-1",
        capability="core.process.exec",
        channel=channel,
    )


def test_store_deduplicates_content_but_preserves_distinct_immutable_manifests(
    tmp_path: Path,
) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024)

    first = store.put_bytes(
        b"same-content", media_type="text/plain", provenance=provenance(), complete=True
    )
    second = store.put_bytes(
        b"same-content", media_type="text/plain", provenance=provenance(), complete=True
    )

    assert first.id != second.id
    assert first.digest == second.digest
    assert first.storage_ref == second.storage_ref
    assert store.read_bytes(first.reference) == b"same-content"
    assert len(tuple((tmp_path / "artifacts" / "blobs" / "sha256").glob("*/*"))) == 1
    assert len(tuple((tmp_path / "artifacts" / "manifests").glob("*.json"))) == 2
    assert not tuple((tmp_path / "artifacts" / "staging").iterdir())


def test_partial_content_is_quarantined_and_never_marked_accepted(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024)

    manifest = store.put_bytes(
        b"partial", media_type="text/plain", provenance=provenance(), complete=False
    )

    assert manifest.lifecycle is ArtifactLifecycle.QUARANTINED
    assert manifest.validation is ArtifactValidation.PARTIAL
    assert manifest.acceptance == "unaccepted"
    assert store.read_bytes(manifest.reference) == b"partial"


def test_store_detects_blob_corruption_on_read(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts", max_artifact_bytes=1024)
    manifest = store.put_bytes(
        b"trusted", media_type="text/plain", provenance=provenance(), complete=True
    )
    blob = tmp_path / "artifacts" / "blobs" / manifest.storage_ref
    blob.write_bytes(b"corrupt")

    with pytest.raises(MishkanError) as caught:
        store.read_bytes(manifest.reference)

    assert caught.value.envelope.code is ErrorCode.ARTIFACT


def test_store_refuses_content_over_its_configured_bound(tmp_path: Path) -> None:
    store = FilesystemArtifactStore(tmp_path / "artifacts", max_artifact_bytes=4)

    with pytest.raises(MishkanError) as caught:
        store.put_bytes(b"12345", media_type="text/plain", provenance=provenance(), complete=True)

    assert caught.value.envelope.code is ErrorCode.ARTIFACT
    assert not tuple((tmp_path / "artifacts" / "manifests").iterdir())
