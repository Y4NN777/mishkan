from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.domain.time import utc_now
from mishkan.environment import (
    EnvironmentBindingRequest,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentDescriptorValidator,
    EnvironmentObservationRequest,
    EnvironmentOperation,
    EnvironmentOperationPlanner,
    EnvironmentOperationRequest,
    EnvironmentOutcome,
    EnvironmentResolver,
    load_environment_profile,
)
from mishkan.environment.observer import EnvironmentObserver
from mishkan.environment.repository import SQLiteEnvironmentRepository
from mishkan.persistence import SchemaManager
from mishkan.tools.execution import ExecutionMode


def _upload(service: DurableArtifactService, content: bytes, *, media_type: str) -> str:
    upload = service.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type=media_type,
        provenance=ArtifactProvenance(
            producer_identity="test",
            run_id="environment:test",
            task_attempt_id="descriptor:test",
            call_id=f"descriptor:{hashlib.sha256(content).hexdigest()}",
            capability="environment.descriptor",
            channel="environment.descriptor",
        ),
    )
    service.append_chunk(upload.upload_id, offset=0, content=content)
    return service.commit_upload(upload.upload_id).reference


def _foundation(tmp_path: Path, executable_name: str):  # type: ignore[no-untyped-def]
    database = tmp_path / ".mishkan" / "mishkan.db"
    database.parent.mkdir()
    SchemaManager(database).initialize()
    artifacts = DurableArtifactService(
        database,
        tmp_path / ".mishkan" / "artifacts",
        max_artifact_bytes=1_000_000,
        max_chunk_bytes=1_000_000,
    )
    repository = SQLiteEnvironmentRepository(database, artifacts=artifacts)
    profile = load_environment_profile(
        "package://mishkan.resources.environment/default.yaml",
        tmp_path,
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    executable = binaries / executable_name
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return artifacts, repository, profile, binaries, executable


def test_devcontainer_jsonc_reuse_preserves_exact_observed_bytes(tmp_path: Path) -> None:
    artifacts, repository, profile, binaries, _ = _foundation(tmp_path, "devcontainer")
    descriptor_path = tmp_path / ".devcontainer" / "devcontainer.json"
    descriptor_path.parent.mkdir()
    content = b'{\n  // retained comment\n  "image": "example@sha256:abc",\n}\n'
    descriptor_path.write_bytes(content)
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="operator",
            context_id="context:devcontainer",
            repository_revision="abc123",
            execution_location="local:test",
        ),
        path_value=str(binaries),
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
        requested_outcome=EnvironmentOutcome.REUSE_EXISTING,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        execution_location=observation.execution_location,
        required_semantics=("devcontainer.lifecycle",),
        allowed_descriptor_formats=("devcontainer",),
        required_engine_ids=("devcontainer-cli",),
        authorized_engine_ids=("devcontainer-cli",),
        affected_task_ids=("task:workspace",),
        verification_checks=("parse", "workspace"),
        policy_fingerprint="b" * 64,
        rationale="Reuse the existing compatible development container.",
    )
    binding = EnvironmentResolver(freshness_seconds=profile.freshness_seconds).resolve(
        request,
        observation,
    )
    repository.record_binding(binding)
    reference = _upload(artifacts, content, media_type="application/json")
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
                base_revision="abc123",
            ),
        ),
    )

    result = EnvironmentDescriptorValidator(
        repository,
        artifacts,
        max_descriptor_bytes=profile.max_descriptor_bytes,
    ).validate(descriptor_set)

    assert result.valid
    assert result.violations == ()
    assert artifacts.read_bytes(reference) == content


def test_podman_operation_is_a_literal_governed_job_plan(tmp_path: Path) -> None:
    artifacts, repository, profile, binaries, podman = _foundation(tmp_path, "podman")
    content = b"FROM scratch\n"
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="operator",
            context_id="context:oci",
            repository_revision="abc123",
            execution_location="local:test",
        ),
        path_value=str(binaries),
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
        requested_outcome=EnvironmentOutcome.GENERATE,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        execution_location=observation.execution_location,
        required_semantics=("oci.build",),
        allowed_descriptor_formats=("containerfile",),
        required_engine_ids=("podman",),
        authorized_engine_ids=("podman",),
        affected_task_ids=("task:image",),
        verification_checks=("build", "cleanup"),
        policy_fingerprint="b" * 64,
        rationale="Build the accepted OCI image with observed Podman.",
    )
    binding = EnvironmentResolver(freshness_seconds=profile.freshness_seconds).resolve(
        request,
        observation,
    )
    repository.record_binding(binding)
    reference = _upload(artifacts, content, media_type="text/plain")
    descriptor_set = EnvironmentDescriptorSet(
        binding_id=binding.binding_id,
        context_fingerprint=observation.fingerprint,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        members=(
            EnvironmentDescriptorMember(
                format="containerfile",
                logical_path="Containerfile.generated",
                artifact_reference=reference,
                base_revision="abc123",
            ),
        ),
    )
    validator = EnvironmentDescriptorValidator(
        repository,
        artifacts,
        max_descriptor_bytes=profile.max_descriptor_bytes,
    )
    assert validator.validate(descriptor_set).valid
    repository.record_descriptor_set(descriptor_set)
    (tmp_path / "Containerfile.generated").write_bytes(content)
    operation = EnvironmentOperationRequest(
        binding_id=binding.binding_id,
        descriptor_set_id=descriptor_set.descriptor_set_id,
        adapter_id="podman.cli",
        operation=EnvironmentOperation.BUILD,
        descriptor_path="Containerfile.generated",
        parameters={"image": "example.test/app:plan-abc"},
        network_destinations=("registry.example.test:443",),
        owner_identity="operator",
        run_id="run:test",
        task_id="task:image",
        session_profile="default-job",
        deadline=utc_now() + timedelta(minutes=10),
        timeout_seconds=300,
    )

    plan = EnvironmentOperationPlanner(profile, repository, artifacts).plan(operation)

    assert plan.execution.mode is ExecutionMode.JOB
    assert plan.execution.executable == str(podman.resolve())
    assert plan.execution.args == (
        "build",
        "--file",
        "Containerfile.generated",
        "--tag",
        "example.test/app:plan-abc",
        ".",
    )
    assert plan.execution.declared_effects == ("container.image.build",)
    assert plan.execution.network_destinations == ("registry.example.test:443",)
