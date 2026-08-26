"""Durable artifact sessions with SQLite manifests and filesystem CAS bodies."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from mishkan.artifacts.models import (
    ArtifactCollection,
    ArtifactLifecycle,
    ArtifactManifest,
    ArtifactProvenance,
    ArtifactValidation,
    GarbageCollectionPlan,
    UploadSession,
    WorkingReference,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import (
    ArtifactCollectionRow,
    ArtifactGCPlanRow,
    ArtifactHoldRow,
    ArtifactPinRow,
    ArtifactReferenceRow,
    ArtifactRow,
    ArtifactUploadRow,
    LocalRunRepository,
)


class DurableArtifactService:
    """Keep authoritative metadata transactional and bodies immutable in local CAS."""

    def __init__(
        self,
        database: Path,
        root: Path,
        *,
        max_artifact_bytes: int,
        max_chunk_bytes: int,
    ) -> None:
        SchemaManager(database).require_current()
        self._root = root.resolve()
        self._blobs = self._root / "blobs"
        self._staging = self._root / "staging"
        self._legacy_manifests = self._root / "manifests"
        self._max_artifact_bytes = max_artifact_bytes
        self._max_chunk_bytes = max_chunk_bytes
        for directory in (self._blobs, self._staging):
            directory.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{database.resolve()}")
        event.listen(self._engine, "connect", LocalRunRepository._configure_connection)

    def open_upload(
        self,
        *,
        expected_size: int,
        expected_digest: str,
        media_type: str,
        provenance: ArtifactProvenance,
        sensitivity: str = "internal",
        retention: str = "run",
    ) -> UploadSession:
        self._validate_digest(expected_digest)
        if expected_size < 0 or expected_size > self._max_artifact_bytes:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact upload size is outside the configured bound",
                details={"size_bytes": expected_size, "limit": self._max_artifact_bytes},
            )
        upload_id = new_id()
        staged = self._staging / f"{upload_id}.upload"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        now = utc_now()
        metadata = {
            "provenance": provenance.model_dump(mode="json"),
            "sensitivity": sensitivity,
            "retention": retention,
        }
        try:
            with Session(self._engine) as session, session.begin():
                session.add(
                    ArtifactUploadRow(
                        id=str(upload_id),
                        expected_digest=expected_digest,
                        expected_size=expected_size,
                        media_type=media_type,
                        offset=0,
                        state="staging",
                        artifact_id=None,
                        staging_path=staged.name,
                        metadata_payload=json.dumps(metadata, sort_keys=True),
                        created_at=now.isoformat(),
                        updated_at=now.isoformat(),
                    )
                )
        except Exception:
            staged.unlink(missing_ok=True)
            raise
        return UploadSession(
            upload_id=upload_id,
            expected_size=expected_size,
            expected_digest=expected_digest,
            media_type=media_type,
            offset=0,
            lifecycle="staging",
            created_at=now,
        )

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        provenance: ArtifactProvenance,
        complete: bool,
        sensitivity: str = "internal",
        retention: str = "run",
    ) -> ArtifactManifest:
        if not complete:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "durable artifact publication requires complete content",
            )
        upload = self.open_upload(
            expected_size=len(content),
            expected_digest=self._digest(content),
            media_type=media_type,
            provenance=provenance,
            sensitivity=sensitivity,
            retention=retention,
        )
        for offset in range(0, len(content), self._max_chunk_bytes):
            self.append_chunk(
                upload.upload_id,
                offset=offset,
                content=content[offset : offset + self._max_chunk_bytes],
            )
        return self.commit_upload(upload.upload_id)

    def append_chunk(self, upload_id: UUID, *, offset: int, content: bytes) -> UploadSession:
        if not content or len(content) > self._max_chunk_bytes:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact chunk is empty or exceeds the configured backpressure bound",
                details={"size_bytes": len(content), "limit": self._max_chunk_bytes},
            )
        with Session(self._engine) as session, session.begin():
            row = self._require_upload(session, upload_id)
            if row.state != "staging" or offset != row.offset:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "artifact chunk offset does not match the durable upload cursor",
                    details={"expected_offset": row.offset, "received_offset": offset},
                )
            if row.offset + len(content) > row.expected_size:
                raise MishkanError(ErrorCode.ARTIFACT, "artifact chunk exceeds expected size")
            staged = self._staging_path(row)
            observed = staged.stat().st_size
            if observed != row.offset:
                row.state = "uncertain"
                raise MishkanError(
                    ErrorCode.ARTIFACT,
                    "artifact staging body differs from its durable cursor",
                    details={"durable_offset": row.offset, "observed_size": observed},
                )
            descriptor = os.open(staged, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            try:
                written = os.write(descriptor, content)
                if written != len(content):
                    raise OSError("short artifact chunk write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            row.offset += len(content)
            row.updated_at = utc_now().isoformat()
            return self._upload_model(row)

    def commit_upload(self, upload_id: UUID) -> ArtifactManifest:
        with Session(self._engine) as session, session.begin():
            row = self._require_upload(session, upload_id)
            if row.state == "committed" and row.artifact_id is not None:
                artifact = session.get(ArtifactRow, row.artifact_id)
                if artifact is not None:
                    return ArtifactManifest.model_validate_json(artifact.manifest_payload)
            if row.state != "staging":
                raise MishkanError(ErrorCode.ARTIFACT, "artifact upload is not committable")
            staged = self._staging_path(row)
            content_digest, size = self._hash_file(staged)
            observed_digest = f"sha256:{content_digest}"
            if size != row.expected_size or observed_digest != row.expected_digest:
                raise MishkanError(
                    ErrorCode.ARTIFACT,
                    "artifact upload failed size or digest verification",
                    details={
                        "expected_size": row.expected_size,
                        "observed_size": size,
                        "expected_digest": row.expected_digest,
                        "observed_digest": observed_digest,
                    },
                )
            storage_ref = f"sha256/{content_digest[:2]}/{content_digest[2:]}"
            destination = self._safe_blob(storage_ref)
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._commit_staged_blob(staged, destination, content_digest, size)
            metadata = json.loads(row.metadata_payload)
            manifest = ArtifactManifest(
                digest=observed_digest,
                size_bytes=size,
                declared_media_type=row.media_type,
                detected_media_type=row.media_type,
                provenance=ArtifactProvenance.model_validate(metadata["provenance"]),
                sensitivity=str(metadata["sensitivity"]),
                retention=str(metadata["retention"]),
                validation=ArtifactValidation.INTEGRITY_VERIFIED,
                lifecycle=ArtifactLifecycle.AVAILABLE,
                storage_ref=storage_ref,
            )
            session.add(
                ArtifactRow(
                    id=str(manifest.id),
                    digest=manifest.digest,
                    size_bytes=manifest.size_bytes,
                    media_type=manifest.declared_media_type,
                    lifecycle=manifest.lifecycle.value,
                    storage_ref=manifest.storage_ref,
                    manifest_payload=manifest.model_dump_json(),
                    created_at=manifest.created_at.isoformat(),
                    tombstoned_at=None,
                )
            )
            row.state = "committed"
            row.artifact_id = str(manifest.id)
            row.updated_at = utc_now().isoformat()
            return manifest

    def manifest(self, reference: str) -> ArtifactManifest:
        artifact_id = self._reference_id(reference)
        with Session(self._engine) as session:
            row = session.get(ArtifactRow, str(artifact_id))
            if row is None:
                raise MishkanError(ErrorCode.ARTIFACT, "artifact manifest does not exist")
            return ArtifactManifest.model_validate_json(row.manifest_payload).model_copy(
                update={"lifecycle": ArtifactLifecycle(row.lifecycle)}
            )

    def list_manifests(self, *, offset: int = 0, limit: int = 100) -> tuple[ArtifactManifest, ...]:
        if offset < 0 or limit < 1 or limit > 1_000:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "artifact query bound is invalid")
        with Session(self._engine) as session:
            rows = session.scalars(
                select(ArtifactRow)
                .order_by(ArtifactRow.created_at, ArtifactRow.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return tuple(
                ArtifactManifest.model_validate_json(row.manifest_payload).model_copy(
                    update={"lifecycle": ArtifactLifecycle(row.lifecycle)}
                )
                for row in rows
            )

    def read_bytes(self, reference: str) -> bytes:
        return self.body_path(reference).read_bytes()

    def body_path(self, reference: str) -> Path:
        manifest = self.manifest(reference)
        if manifest.lifecycle is not ArtifactLifecycle.AVAILABLE:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact body is not available",
                details={"lifecycle": manifest.lifecycle.value},
            )
        blob = self._safe_blob(manifest.storage_ref)
        try:
            digest, size = self._hash_file(blob)
        except OSError as exc:
            self._set_lifecycle(manifest.id, ArtifactLifecycle.MISSING)
            raise MishkanError(ErrorCode.ARTIFACT, "artifact body is missing") from exc
        if size != manifest.size_bytes or f"sha256:{digest}" != manifest.digest:
            self._set_lifecycle(manifest.id, ArtifactLifecycle.CORRUPT)
            raise MishkanError(ErrorCode.ARTIFACT, "artifact body failed integrity verification")
        return blob

    def create_collection(self, entries: dict[str, str]) -> ArtifactCollection:
        normalized: dict[str, str] = {}
        for logical_path, reference in entries.items():
            self._validate_logical_path(logical_path)
            self.manifest(reference)
            normalized[logical_path] = reference
        collection = ArtifactCollection(collection_id=new_id(), entries=normalized)
        with Session(self._engine) as session, session.begin():
            session.add(
                ArtifactCollectionRow(
                    id=str(collection.collection_id),
                    entries_payload=json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                    created_at=collection.created_at.isoformat(),
                )
            )
        return collection

    def update_reference(
        self,
        scope: str,
        name: str,
        artifact_reference: str,
        *,
        expected_revision: int,
    ) -> WorkingReference:
        artifact_id = self._reference_id(artifact_reference)
        WorkingReference(
            scope=scope,
            name=name,
            artifact_reference=artifact_reference,
            revision=max(expected_revision + 1, 1),
        )
        with Session(self._engine) as session, session.begin():
            artifact = session.get(ArtifactRow, str(artifact_id))
            if artifact is None or artifact.lifecycle != ArtifactLifecycle.AVAILABLE.value:
                raise MishkanError(ErrorCode.ARTIFACT, "working reference target is unavailable")
            row = session.get(ArtifactReferenceRow, (scope, name))
            current = row.revision if row is not None else 0
            if current != expected_revision:
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "working reference compare-and-swap revision differs",
                    details={"expected": expected_revision, "current": current},
                )
            now = utc_now()
            if row is None:
                row = ArtifactReferenceRow(
                    scope=scope,
                    name=name,
                    artifact_id=str(artifact_id),
                    revision=1,
                    updated_at=now.isoformat(),
                )
                session.add(row)
            else:
                row.artifact_id = str(artifact_id)
                row.revision += 1
                row.updated_at = now.isoformat()
            session.flush()
            return WorkingReference(
                scope=scope,
                name=name,
                artifact_reference=artifact_reference,
                revision=row.revision,
                updated_at=now,
            )

    def hold(self, reference: str, reason: str) -> None:
        artifact_id = self._reference_id(reference)
        with Session(self._engine) as session, session.begin():
            if session.get(ArtifactRow, str(artifact_id)) is None:
                raise MishkanError(ErrorCode.ARTIFACT, "artifact hold target does not exist")
            session.merge(
                ArtifactHoldRow(
                    artifact_id=str(artifact_id), reason=reason, created_at=utc_now().isoformat()
                )
            )

    def pin(self, reference: str) -> None:
        artifact_id = self._reference_id(reference)
        with Session(self._engine) as session, session.begin():
            if session.get(ArtifactRow, str(artifact_id)) is None:
                raise MishkanError(ErrorCode.ARTIFACT, "artifact pin target does not exist")
            session.merge(
                ArtifactPinRow(artifact_id=str(artifact_id), created_at=utc_now().isoformat())
            )

    def plan_gc(self, *, watermark: datetime) -> GarbageCollectionPlan:
        with Session(self._engine) as session, session.begin():
            rooted = self._rooted_ids(session)
            rows = session.scalars(
                select(ArtifactRow).where(
                    ArtifactRow.lifecycle == ArtifactLifecycle.AVAILABLE.value,
                    ArtifactRow.created_at < watermark.isoformat(),
                )
            ).all()
            candidates = tuple(f"artifact:{row.id}" for row in rows if row.id not in rooted)
            plan = GarbageCollectionPlan(
                plan_id=new_id(), candidates=candidates, watermark=watermark
            )
            session.add(
                ArtifactGCPlanRow(
                    id=str(plan.plan_id),
                    watermark=watermark.isoformat(),
                    candidates_payload=json.dumps(candidates),
                    applied_at=None,
                    created_at=utc_now().isoformat(),
                )
            )
            return plan

    def apply_gc(self, plan_id: UUID) -> GarbageCollectionPlan:
        blobs: set[str] = set()
        with Session(self._engine) as session, session.begin():
            plan_row = session.get(ArtifactGCPlanRow, str(plan_id))
            if plan_row is None:
                raise MishkanError(ErrorCode.ARTIFACT, "garbage collection plan does not exist")
            candidates = tuple(json.loads(plan_row.candidates_payload))
            watermark = datetime.fromisoformat(plan_row.watermark)
            if plan_row.applied_at is not None:
                return GarbageCollectionPlan(
                    plan_id=plan_id,
                    candidates=candidates,
                    watermark=watermark,
                    applied=True,
                )
            rooted = self._rooted_ids(session)
            for reference in candidates:
                artifact_id = str(self._reference_id(reference))
                if artifact_id in rooted:
                    raise MishkanError(
                        ErrorCode.REVISION_MISMATCH,
                        "garbage collection roots changed after planning",
                        details={"artifact": reference},
                    )
                row = session.get(ArtifactRow, artifact_id)
                if row is not None and row.lifecycle == ArtifactLifecycle.AVAILABLE.value:
                    row.lifecycle = ArtifactLifecycle.TOMBSTONED.value
                    row.tombstoned_at = utc_now().isoformat()
                    manifest = ArtifactManifest.model_validate_json(row.manifest_payload)
                    row.manifest_payload = manifest.model_copy(
                        update={"lifecycle": ArtifactLifecycle.TOMBSTONED}
                    ).model_dump_json()
                    blobs.add(row.storage_ref)
            plan_row.applied_at = utc_now().isoformat()
        for storage_ref in blobs:
            with Session(self._engine) as session:
                live = session.scalar(
                    select(ArtifactRow.id).where(
                        ArtifactRow.storage_ref == storage_ref,
                        ArtifactRow.lifecycle == ArtifactLifecycle.AVAILABLE.value,
                    )
                )
            if live is None:
                self._safe_blob(storage_ref).unlink(missing_ok=True)
        return GarbageCollectionPlan(
            plan_id=plan_id,
            candidates=candidates,
            watermark=watermark,
            applied=True,
        )

    def import_legacy_manifests(self) -> int:
        """Import I02 JSON manifests and classify unavailable bodies without hiding them."""
        if not self._legacy_manifests.is_dir():
            return 0
        imported = 0
        for path in sorted(self._legacy_manifests.glob("*.json")):
            try:
                manifest = ArtifactManifest.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValidationError):
                continue
            blob = self._safe_blob(manifest.storage_ref)
            lifecycle = manifest.lifecycle
            if not blob.is_file():
                lifecycle = ArtifactLifecycle.MISSING
            else:
                digest, size = self._hash_file(blob)
                if size != manifest.size_bytes or f"sha256:{digest}" != manifest.digest:
                    lifecycle = ArtifactLifecycle.CORRUPT
            imported_manifest = manifest.model_copy(update={"lifecycle": lifecycle})
            with Session(self._engine) as session, session.begin():
                if session.get(ArtifactRow, str(manifest.id)) is not None:
                    continue
                session.add(
                    ArtifactRow(
                        id=str(manifest.id),
                        digest=manifest.digest,
                        size_bytes=manifest.size_bytes,
                        media_type=manifest.declared_media_type,
                        lifecycle=lifecycle.value,
                        storage_ref=manifest.storage_ref,
                        manifest_payload=imported_manifest.model_dump_json(),
                        created_at=manifest.created_at.isoformat(),
                        tombstoned_at=None,
                    )
                )
                imported += 1
        return imported

    def _set_lifecycle(self, artifact_id: UUID, lifecycle: ArtifactLifecycle) -> None:
        with Session(self._engine) as session, session.begin():
            row = session.get(ArtifactRow, str(artifact_id))
            if row is not None:
                row.lifecycle = lifecycle.value
                manifest = ArtifactManifest.model_validate_json(row.manifest_payload)
                row.manifest_payload = manifest.model_copy(
                    update={"lifecycle": lifecycle}
                ).model_dump_json()

    def _staging_path(self, row: ArtifactUploadRow) -> Path:
        path = (self._staging / row.staging_path).resolve()
        if not path.is_relative_to(self._staging) or path.parent != self._staging:
            raise MishkanError(ErrorCode.ARTIFACT, "artifact staging path escaped its root")
        return path

    def _safe_blob(self, storage_ref: str) -> Path:
        path = (self._blobs / storage_ref).resolve()
        if not path.is_relative_to(self._blobs):
            raise MishkanError(ErrorCode.ARTIFACT, "artifact storage path escaped its root")
        return path

    @staticmethod
    def _require_upload(session: Session, upload_id: UUID) -> ArtifactUploadRow:
        row = session.get(ArtifactUploadRow, str(upload_id))
        if row is None:
            raise MishkanError(ErrorCode.ARTIFACT, "artifact upload session does not exist")
        return row

    @staticmethod
    def _upload_model(row: ArtifactUploadRow) -> UploadSession:
        observed = row.state if row.state in {"staging", "committed", "aborted"} else "aborted"
        lifecycle = cast(Literal["staging", "committed", "aborted"], observed)
        return UploadSession(
            upload_id=UUID(row.id),
            expected_size=row.expected_size,
            expected_digest=row.expected_digest,
            media_type=row.media_type,
            offset=row.offset,
            lifecycle=lifecycle,
            created_at=datetime.fromisoformat(row.created_at),
        )

    @staticmethod
    def _commit_staged_blob(staged: Path, destination: Path, digest: str, size: int) -> None:
        try:
            os.link(staged, destination, follow_symlinks=False)
            descriptor = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            observed_digest, observed_size = DurableArtifactService._hash_file(destination)
            if observed_digest != digest or observed_size != size:
                raise MishkanError(
                    ErrorCode.ARTIFACT, "artifact CAS collision was detected"
                ) from None
        staged.unlink(missing_ok=True)

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _digest(content: bytes) -> str:
        return f"sha256:{hashlib.sha256(content).hexdigest()}"

    @staticmethod
    def _validate_digest(value: str) -> None:
        if len(value) != 71 or not value.startswith("sha256:"):
            raise MishkanError(ErrorCode.ARTIFACT, "artifact digest is invalid")
        try:
            int(value.removeprefix("sha256:"), 16)
        except ValueError as exc:
            raise MishkanError(ErrorCode.ARTIFACT, "artifact digest is invalid") from exc

    @staticmethod
    def _reference_id(reference: str) -> UUID:
        prefix, separator, value = reference.partition(":")
        if prefix != "artifact" or not separator:
            raise MishkanError(ErrorCode.ARTIFACT, "artifact reference is invalid")
        try:
            return UUID(value)
        except ValueError as exc:
            raise MishkanError(ErrorCode.ARTIFACT, "artifact reference is invalid") from exc

    @staticmethod
    def _validate_logical_path(value: str) -> None:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise MishkanError(ErrorCode.ARTIFACT, "collection logical path is unsafe")

    @staticmethod
    def _rooted_ids(session: Session) -> set[str]:
        rooted = set(session.scalars(select(ArtifactReferenceRow.artifact_id)).all())
        rooted.update(session.scalars(select(ArtifactHoldRow.artifact_id)).all())
        rooted.update(session.scalars(select(ArtifactPinRow.artifact_id)).all())
        for payload in session.scalars(select(ArtifactCollectionRow.entries_payload)).all():
            rooted.update(
                str(DurableArtifactService._reference_id(ref))
                for ref in json.loads(payload).values()
            )
        return rooted
