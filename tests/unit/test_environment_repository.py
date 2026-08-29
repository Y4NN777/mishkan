from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.identity import new_id
from mishkan.environment import (
    EnvironmentBindingRequest,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentInvalidation,
    EnvironmentInvalidationCause,
    EnvironmentObservationRequest,
    EnvironmentOutcome,
    EnvironmentResolver,
)
from mishkan.environment.observer import EnvironmentObserver
from mishkan.environment.profile import load_environment_profile
from mishkan.environment.repository import SQLiteEnvironmentRepository
from mishkan.persistence import SchemaManager


def _artifacts(database: Path, root: Path) -> DurableArtifactService:
    return DurableArtifactService(
        database,
        root,
        max_artifact_bytes=1_000_000,
        max_chunk_bytes=1_000_000,
    )


def _upload(service: DurableArtifactService, content: bytes) -> str:
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="application/json",
        provenance=ArtifactProvenance(
            producer_identity="test",
            run_id="environment:test",
            task_attempt_id="descriptor:test",
            call_id="upload:test",
            capability="environment.descriptor",
            channel="environment.descriptor",
        ),
    )
    service.append_chunk(upload.upload_id, offset=0, content=content)
    return service.commit_upload(upload.upload_id).reference


def _records(tmp_path: Path):  # type: ignore[no-untyped-def]
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    artifacts = _artifacts(database, tmp_path / "artifacts")
    repository = SQLiteEnvironmentRepository(database, artifacts=artifacts)
    profile = load_environment_profile(
        "package://mishkan.resources.environment/default.yaml",
        tmp_path,
    )
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="operator",
            context_id="context:test",
            repository_revision="abc123",
            execution_location="local:test",
        ),
        path_value=str(Path(sys.executable).parent),
    )
    repository.record_observation(observation)
    request = EnvironmentBindingRequest(
        mission_id="mission:test",
        plan_fingerprint="a" * 64,
        owner_identity="operator",
        context_id=observation.context_id,
        observation_id=observation.observation_id,
        observation_revision=observation.revision,
        observation_fingerprint=observation.fingerprint,
        requested_outcome=EnvironmentOutcome.HOST_NATIVE,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        execution_location=observation.execution_location,
        required_semantics=("language.run",),
        required_engine_ids=("python",),
        authorized_engine_ids=("python",),
        affected_task_ids=("task:test",),
        verification_checks=("parse",),
        policy_fingerprint="b" * 64,
        rationale="No engine is selected in this persistence fixture.",
    )
    binding = EnvironmentResolver(freshness_seconds=profile.freshness_seconds).resolve(
        request,
        observation,
    )
    repository.record_binding(binding)
    return database, artifacts, repository, observation, binding


def test_environment_evidence_is_atomic_and_artifact_verified(tmp_path: Path) -> None:
    database, artifacts, repository, observation, binding = _records(tmp_path)
    reference = _upload(artifacts, b'{"image":"example@sha256:abc"}')
    descriptor_set = EnvironmentDescriptorSet(
        binding_id=binding.binding_id,
        context_fingerprint=observation.fingerprint,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        members=(
            EnvironmentDescriptorMember(
                format="devcontainer",
                specification_version="2.1",
                logical_path=".devcontainer/devcontainer.json",
                artifact_reference=reference,
                base_revision=observation.repository_revision,
            ),
        ),
    )

    assert repository.record_descriptor_set(descriptor_set) == descriptor_set
    assert repository.descriptor_set(str(descriptor_set.descriptor_set_id)) == descriptor_set
    with create_engine(f"sqlite:///{database}").connect() as connection:
        event_types = set(
            connection.execute(text("SELECT event_type FROM event_outbox")).scalars().all()
        )
    assert {
        "environment.observed",
        "environment.binding_compatible",
        "environment.descriptor_set_recorded",
    }.issubset(event_types)


def test_binding_rejects_stale_observation_and_missing_artifact(tmp_path: Path) -> None:
    _, _, repository, observation, binding = _records(tmp_path)
    stale = binding.model_copy(
        update={"request": binding.request.model_copy(update={"observation_fingerprint": "c" * 64})}
    )
    with pytest.raises(MishkanError) as stale_error:
        repository.record_binding(stale)
    assert stale_error.value.envelope.code is ErrorCode.REVISION_MISMATCH

    missing = EnvironmentDescriptorSet(
        binding_id=binding.binding_id,
        context_fingerprint=observation.fingerprint,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        members=(
            EnvironmentDescriptorMember(
                format="devcontainer",
                logical_path=".devcontainer/devcontainer.json",
                artifact_reference="artifact:00000000-0000-0000-0000-000000000000",
            ),
        ),
    )
    with pytest.raises(MishkanError) as missing_error:
        repository.record_descriptor_set(missing)
    assert missing_error.value.envelope.code is ErrorCode.ARTIFACT


def test_invalidation_is_evidence_based_and_closes_the_binding(tmp_path: Path) -> None:
    _, artifacts, repository, observation, binding = _records(tmp_path)
    reference = _upload(artifacts, b'{"image":"example@sha256:abc"}')
    descriptor_set = EnvironmentDescriptorSet(
        binding_id=binding.binding_id,
        context_fingerprint=observation.fingerprint,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        members=(
            EnvironmentDescriptorMember(
                format="devcontainer",
                logical_path=".devcontainer/devcontainer.json",
                artifact_reference=reference,
            ),
        ),
    )

    unchanged = EnvironmentInvalidation(
        binding_id=binding.binding_id,
        expected_binding_revision=binding.revision,
        owner_identity=binding.request.owner_identity,
        cause=EnvironmentInvalidationCause.CONTEXT,
        observed_context_fingerprint=observation.fingerprint,
        affected_task_ids=binding.request.affected_task_ids,
        policy_fingerprint=binding.request.policy_fingerprint,
        reason="A caller claimed the unchanged context was stale.",
    )
    with pytest.raises(MishkanError) as unchanged_error:
        repository.invalidate(unchanged)
    assert unchanged_error.value.envelope.code is ErrorCode.OUTPUT_CONTRACT

    invalidation = unchanged.model_copy(
        update={
            "invalidation_id": new_id(),
            "observed_context_fingerprint": "c" * 64,
            "reason": "The observed execution context changed.",
        }
    )
    assert repository.invalidate(invalidation) == invalidation
    assert repository.binding(str(binding.binding_id)).state.value == "stale"
    with pytest.raises(MishkanError, match="after binding invalidation"):
        repository.record_descriptor_set(descriptor_set)
