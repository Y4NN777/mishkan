from __future__ import annotations

import os
from pathlib import Path

from mishkan.environment import (
    AvailabilityState,
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentObservationRequest,
    EnvironmentObserver,
    EnvironmentOutcome,
    EnvironmentProfile,
    EnvironmentResolver,
    load_environment_profile,
)


def _profile(tmp_path: Path) -> EnvironmentProfile:
    return load_environment_profile(
        "package://mishkan.resources.environment/default.yaml",
        tmp_path,
    )


def test_observation_keeps_availability_dimensions_independent(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.test/app\n", encoding="utf-8")
    (tmp_path / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    podman = binaries / "podman"
    podman.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    podman.chmod(0o755)

    observation = EnvironmentObserver(_profile(tmp_path)).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="engineer:test",
            context_id="repository:example",
            execution_location="local:test",
            repository_id="repo-example",
            repository_revision="abc1234",
        ),
        path_value=str(binaries),
    )

    podman_observation = next(item for item in observation.engines if item.engine_id == "podman")
    assert podman_observation.fact("detected") is AvailabilityState.TRUE
    assert podman_observation.fact("executable") is AvailabilityState.TRUE
    assert podman_observation.fact("authenticated") is AvailabilityState.UNKNOWN
    assert podman_observation.fact("healthy") is AvailabilityState.UNKNOWN
    assert podman_observation.fact("authorized") is AvailabilityState.UNKNOWN
    assert podman_observation.version is None
    assert podman_observation.safe_probe == (str(podman.resolve()), "--version")
    assert observation.manifests["go"] == ("go.mod",)
    assert observation.descriptors[0].logical_path == "Containerfile"


def test_resolver_honors_exact_outcome_authority_and_context(tmp_path: Path) -> None:
    (tmp_path / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    podman = binaries / "podman"
    podman.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    podman.chmod(0o755)
    profile = _profile(tmp_path)
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity="engineer:test",
            context_id="repository:example",
            execution_location="local:test",
            repository_id="repo-example",
            repository_revision="abc1234",
        ),
        path_value=str(binaries),
    )
    request = EnvironmentBindingRequest(
        mission_id="mission-1",
        plan_fingerprint="a" * 64,
        owner_identity="Platform_Engineer",
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
        affected_task_ids=("task-build",),
        verification_checks=("build", "startup", "cleanup"),
        policy_fingerprint="b" * 64,
        rationale="Generate the requested OCI build descriptor only.",
    )
    resolver = EnvironmentResolver(freshness_seconds=profile.freshness_seconds)

    compatible = resolver.resolve(request, observation)
    unauthorized = resolver.resolve(
        request.model_copy(update={"authorized_engine_ids": ()}),
        observation,
    )
    stale = resolver.resolve(
        request.model_copy(update={"observation_fingerprint": "c" * 64}),
        observation,
    )

    assert compatible.state is EnvironmentBindingState.COMPATIBLE
    assert compatible.selected_engine_ids == ("podman",)
    assert compatible.selected_adapter_ids == ("podman.cli",)
    assert unauthorized.state is EnvironmentBindingState.INCOMPATIBLE
    assert unauthorized.selected_engine_ids == ()
    assert "authorized-compatible-engine" in unauthorized.missing_conditions
    assert stale.state is EnvironmentBindingState.STALE
    assert stale.missing_conditions == ("observation-fingerprint",)
    assert os.path.exists(podman)
