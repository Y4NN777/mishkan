"""Atomic local content-addressed artifact store for the execution boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from mishkan.artifacts.models import (
    ArtifactAvailability,
    ArtifactFacts,
    ArtifactFactState,
    ArtifactLifecycle,
    ArtifactManifest,
    ArtifactProvenance,
    ArtifactTrust,
    ArtifactValidation,
)
from mishkan.domain.errors import ErrorCode, MishkanError


class ArtifactStore(Protocol):
    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        provenance: ArtifactProvenance,
        complete: bool,
        sensitivity: str = "internal",
        retention: str = "run",
    ) -> ArtifactManifest: ...


class FilesystemArtifactStore:
    """Persist immutable manifests and deduplicated blobs without mutable references."""

    def __init__(self, root: Path, *, max_artifact_bytes: int) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("artifact byte bound must be positive")
        self._root = root.resolve()
        self._blobs = self._root / "blobs"
        self._manifests = self._root / "manifests"
        self._staging = self._root / "staging"
        self._max_artifact_bytes = max_artifact_bytes
        for directory in (self._blobs, self._manifests, self._staging):
            directory.mkdir(parents=True, exist_ok=True)

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
        try:
            return self._put_bytes(
                content,
                media_type=media_type,
                provenance=provenance,
                complete=complete,
                sensitivity=sensitivity,
                retention=retention,
            )
        except MishkanError:
            raise
        except OSError as exc:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact content could not be committed atomically",
                details={"reason": type(exc).__name__},
            ) from exc

    def _put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        provenance: ArtifactProvenance,
        complete: bool,
        sensitivity: str = "internal",
        retention: str = "run",
    ) -> ArtifactManifest:
        if len(content) > self._max_artifact_bytes:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact content exceeds the configured storage bound",
                details={"size_bytes": len(content), "limit": self._max_artifact_bytes},
            )
        digest_hex = hashlib.sha256(content).hexdigest()
        storage_ref = f"sha256/{digest_hex[:2]}/{digest_hex[2:]}"
        blob = self._blobs / storage_ref
        blob.parent.mkdir(parents=True, exist_ok=True)
        self._commit_blob(content, blob, digest_hex)
        manifest = ArtifactManifest(
            digest=f"sha256:{digest_hex}",
            size_bytes=len(content),
            declared_media_type=media_type,
            provenance=provenance,
            sensitivity=sensitivity,
            retention=retention,
            validation=(
                ArtifactValidation.INTEGRITY_VERIFIED if complete else ArtifactValidation.PARTIAL
            ),
            lifecycle=(ArtifactLifecycle.AVAILABLE if complete else ArtifactLifecycle.QUARANTINED),
            storage_ref=storage_ref,
            facts=ArtifactFacts(
                integrity=(
                    ArtifactFactState.PASSED if complete else ArtifactFactState.NOT_EVALUATED
                ),
                sensitivity=sensitivity,
                availability=ArtifactAvailability.AVAILABLE,
                trust=(ArtifactTrust.UNTRUSTED if complete else ArtifactTrust.QUARANTINED),
            ),
        )
        self._commit_manifest(manifest)
        return manifest

    def read_manifest(self, reference: str) -> ArtifactManifest:
        identifier = self._reference_id(reference)
        path = self._manifests / f"{identifier}.json"
        try:
            raw = path.read_text(encoding="utf-8")
            return ArtifactManifest.model_validate_json(raw)
        except (OSError, ValidationError) as exc:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact manifest is missing or invalid",
                details={"reference": reference},
            ) from exc

    def read_bytes(self, reference: str) -> bytes:
        manifest = self.read_manifest(reference)
        blob = (self._blobs / manifest.storage_ref).resolve()
        if not blob.is_relative_to(self._blobs.resolve()):
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact storage reference escapes the configured store",
                details={"reference": reference},
            )
        try:
            content = blob.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact blob is missing",
                details={"reference": reference},
            ) from exc
        observed = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if len(content) != manifest.size_bytes or observed != manifest.digest:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact blob failed integrity verification",
                details={"reference": reference},
            )
        return content

    def _commit_blob(self, content: bytes, destination: Path, digest_hex: str) -> None:
        staged = self._staging / f"{uuid4()}.blob"
        try:
            self._write_fsynced(staged, content)
            try:
                os.link(staged, destination)
                self._fsync_directory(destination.parent)
            except FileExistsError:
                existing = destination.read_bytes()
                if hashlib.sha256(existing).hexdigest() != digest_hex or len(existing) != len(
                    content
                ):
                    raise MishkanError(
                        ErrorCode.ARTIFACT,
                        "content-addressed artifact collision or corruption detected",
                        details={"storage_ref": destination.relative_to(self._blobs).as_posix()},
                    ) from None
        finally:
            staged.unlink(missing_ok=True)

    def _commit_manifest(self, manifest: ArtifactManifest) -> None:
        destination = self._manifests / f"{manifest.id}.json"
        staged = self._staging / f"{uuid4()}.manifest"
        payload = json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        try:
            self._write_fsynced(staged, payload)
            try:
                os.link(staged, destination)
                self._fsync_directory(destination.parent)
            except FileExistsError as exc:
                raise MishkanError(
                    ErrorCode.ARTIFACT,
                    "artifact manifest identity already exists",
                    details={"artifact_id": str(manifest.id)},
                ) from exc
        finally:
            staged.unlink(missing_ok=True)

    @staticmethod
    def _write_fsynced(path: Path, content: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _reference_id(reference: str) -> UUID:
        prefix, separator, raw = reference.partition(":")
        if prefix != "artifact" or not separator:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact reference is invalid",
                details={"reference": reference},
            )
        try:
            return UUID(raw)
        except ValueError as exc:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "artifact reference is invalid",
                details={"reference": reference},
            ) from exc
