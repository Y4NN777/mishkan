"""Transactional SQLite repository for environment evidence and decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from mishkan.artifacts import ArtifactLifecycle, ArtifactManifest
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now
from mishkan.environment.models import (
    EnvironmentAttempt,
    EnvironmentBinding,
    EnvironmentBindingState,
    EnvironmentDescriptorSet,
    EnvironmentObservation,
    EnvironmentVerification,
)
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import (
    EnvironmentAttemptRow,
    EnvironmentBindingRow,
    EnvironmentDescriptorSetRow,
    EnvironmentObservationRow,
    EnvironmentVerificationRow,
    OutboxRow,
    create_local_engine,
)

RecordT = TypeVar("RecordT", bound=BaseModel)


class ArtifactLookup(Protocol):
    def manifest(self, reference: str) -> ArtifactManifest: ...

    def read_bytes(self, reference: str) -> bytes: ...


class SQLiteEnvironmentRepository:
    def __init__(
        self,
        database_path: Path,
        *,
        artifacts: ArtifactLookup,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        SchemaManager(database_path).require_current()
        self._engine = create_local_engine(database_path, busy_timeout_ms=busy_timeout_ms)
        self._artifacts = artifacts

    def record_observation(self, record: EnvironmentObservation) -> EnvironmentObservation:
        payload = self._json(record)
        with Session(self._engine) as session, session.begin():
            existing = session.get(EnvironmentObservationRow, str(record.observation_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, record)
            session.add(
                EnvironmentObservationRow(
                    id=str(record.observation_id),
                    context_id=record.context_id,
                    revision=record.revision,
                    fingerprint=record.fingerprint,
                    payload=payload,
                    observed_at=record.observed_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(record.observation_id),
                context_id=record.context_id,
                event_type="environment.observed",
                payload={
                    "observation_id": str(record.observation_id),
                    "revision": record.revision,
                    "fingerprint": record.fingerprint,
                },
            )
        return record

    def observation(self, observation_id: str) -> EnvironmentObservation:
        with Session(self._engine) as session:
            row = session.get(EnvironmentObservationRow, observation_id)
            if row is None:
                raise MishkanError(ErrorCode.ENGINEERING, "environment observation does not exist")
            return EnvironmentObservation.model_validate_json(row.payload)

    def record_binding(self, record: EnvironmentBinding) -> EnvironmentBinding:
        payload = self._json(record)
        with Session(self._engine) as session, session.begin():
            observation = session.get(
                EnvironmentObservationRow,
                str(record.request.observation_id),
            )
            if observation is None:
                raise MishkanError(ErrorCode.ENGINEERING, "binding observation does not exist")
            if (
                observation.revision != record.request.observation_revision
                or observation.fingerprint != record.request.observation_fingerprint
            ):
                raise MishkanError(
                    ErrorCode.REVISION_MISMATCH,
                    "environment observation changed before binding persistence",
                )
            existing = session.get(EnvironmentBindingRow, str(record.binding_id))
            if existing is not None:
                return self._idempotent(existing.payload, payload, record)
            session.add(
                EnvironmentBindingRow(
                    id=str(record.binding_id),
                    request_id=str(record.request.request_id),
                    observation_id=str(record.request.observation_id),
                    context_id=record.request.context_id,
                    state=record.state.value,
                    revision=record.revision,
                    payload=payload,
                    resolved_at=record.resolved_at.isoformat(),
                )
            )
            self._event(
                session,
                aggregate_id=str(record.binding_id),
                context_id=record.request.context_id,
                event_type=f"environment.binding_{record.state.value}",
                payload={
                    "binding_id": str(record.binding_id),
                    "request_id": str(record.request.request_id),
                    "state": record.state.value,
                    "policy_fingerprint": record.request.policy_fingerprint,
                },
            )
        return record

    def binding(self, binding_id: str) -> EnvironmentBinding:
        with Session(self._engine) as session:
            row = session.get(EnvironmentBindingRow, binding_id)
            if row is None:
                raise MishkanError(ErrorCode.ENGINEERING, "environment binding does not exist")
            return EnvironmentBinding.model_validate_json(row.payload)

    def record_descriptor_set(self, record: EnvironmentDescriptorSet) -> EnvironmentDescriptorSet:
        for member in record.members:
            self._require_available_artifact(member.artifact_reference)
        return self._record_child(
            record,
            row_type=EnvironmentDescriptorSetRow,
            identity_field="descriptor_set_id",
            identity=str(record.descriptor_set_id),
            binding_id=str(record.binding_id),
            event_type="environment.descriptor_set_recorded",
        )

    def record_attempt(self, record: EnvironmentAttempt) -> EnvironmentAttempt:
        for reference in record.artifact_references:
            self._require_available_artifact(reference)
        return self._record_child(
            record,
            row_type=EnvironmentAttemptRow,
            identity_field="attempt_id",
            identity=str(record.attempt_id),
            binding_id=str(record.binding_id),
            event_type=f"environment.attempt_{record.settlement.value}",
        )

    def record_verification(self, record: EnvironmentVerification) -> EnvironmentVerification:
        for reference in record.artifact_references:
            self._require_available_artifact(reference)
        return self._record_child(
            record,
            row_type=EnvironmentVerificationRow,
            identity_field="verification_id",
            identity=str(record.verification_id),
            binding_id=str(record.binding_id),
            event_type=f"environment.verification_{record.settlement.value}",
        )

    def descriptor_set(self, descriptor_set_id: str) -> EnvironmentDescriptorSet:
        return self._child(
            EnvironmentDescriptorSetRow,
            descriptor_set_id,
            EnvironmentDescriptorSet,
        )

    def attempt(self, attempt_id: str) -> EnvironmentAttempt:
        return self._child(EnvironmentAttemptRow, attempt_id, EnvironmentAttempt)

    def verification(self, verification_id: str) -> EnvironmentVerification:
        return self._child(
            EnvironmentVerificationRow,
            verification_id,
            EnvironmentVerification,
        )

    def _require_available_artifact(self, reference: str) -> ArtifactManifest:
        manifest = self._artifacts.manifest(reference)
        if manifest.lifecycle is not ArtifactLifecycle.AVAILABLE:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "environment evidence requires an available immutable artifact",
                details={"reference": reference, "lifecycle": manifest.lifecycle.value},
            )
        content = self._artifacts.read_bytes(reference)
        digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if len(content) != manifest.size_bytes or digest != manifest.digest:
            raise MishkanError(
                ErrorCode.ARTIFACT,
                "environment evidence artifact failed size or digest verification",
                details={"reference": reference},
            )
        return manifest

    def _child(
        self,
        row_type: type[Any],
        identity: str,
        model: type[RecordT],
    ) -> RecordT:
        with Session(self._engine) as session:
            row = session.get(row_type, identity)
            if row is None:
                raise MishkanError(ErrorCode.ENGINEERING, "environment evidence does not exist")
            return model.model_validate_json(row.payload)

    def _record_child(
        self,
        record: RecordT,
        *,
        row_type: type[Any],
        identity_field: str,
        identity: str,
        binding_id: str,
        event_type: str,
    ) -> RecordT:
        payload = self._json(record)
        with Session(self._engine) as session, session.begin():
            binding_row = session.get(EnvironmentBindingRow, binding_id)
            if binding_row is None:
                raise MishkanError(ErrorCode.ENGINEERING, "child evidence binding does not exist")
            if EnvironmentBindingState(binding_row.state) is not EnvironmentBindingState.COMPATIBLE:
                raise MishkanError(
                    ErrorCode.ENGINEERING,
                    "environment evidence requires a compatible binding",
                )
            existing = session.get(row_type, identity)
            if existing is not None:
                return self._idempotent(existing.payload, payload, record)
            session.add(
                row_type(
                    **{
                        identity_field: identity,
                        "binding_id": binding_id,
                        "payload": payload,
                        "recorded_at": utc_now().isoformat(),
                    }
                )
            )
            self._event(
                session,
                aggregate_id=identity,
                context_id=binding_id,
                event_type=event_type,
                payload={"record_id": identity, "binding_id": binding_id},
            )
        return record

    @staticmethod
    def _idempotent(existing: str, requested: str, record: RecordT) -> RecordT:
        if existing != requested:
            raise MishkanError(
                ErrorCode.DUPLICATE_RESULT,
                "environment evidence identity already contains different content",
            )
        return record

    @staticmethod
    def _json(record: BaseModel) -> str:
        return json.dumps(record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _event(
        session: Session,
        *,
        aggregate_id: str,
        context_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        session.add(
            OutboxRow(
                id=str(new_id()),
                schema_version="1.0",
                aggregate_id=aggregate_id,
                entity_type="environment",
                run_id=None,
                task_id=None,
                identity_id=None,
                team_id="Engineering",
                security_relevant=False,
                event_type=event_type,
                source="mishkan.environment",
                payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
                occurred_at=utc_now().isoformat(),
                command_id=None,
                correlation_id=None,
                causation_id=None,
                sensitivity="internal",
                published_at=None,
            )
        )
