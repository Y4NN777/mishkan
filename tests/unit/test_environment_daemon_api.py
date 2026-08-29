from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.domain.time import utc_now
from mishkan.environment import (
    EngineeringCommandPlan,
    EngineeringCommandRequest,
    EngineeringCommandState,
    EnvironmentBinding,
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentDescriptorChangePlan,
    EnvironmentDescriptorChangeRequest,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentInvalidation,
    EnvironmentInvalidationCause,
    EnvironmentObservation,
    EnvironmentObservationRequest,
    EnvironmentOperation,
    EnvironmentOperationPlan,
    EnvironmentOperationRequest,
    EnvironmentOutcome,
    EnvironmentSettlement,
    EnvironmentVerification,
    EnvironmentVerificationRequest,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = tmp_path / "config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})


@pytest.mark.anyio
async def test_environment_commands_share_authority_persistence_and_queries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "go.mod").write_text("module example.test/project\n", encoding="utf-8")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    go = binaries / "go"
    go.write_text("#!/bin/sh\nprintf 'pack-command-executed\\n'\n", encoding="utf-8")
    go.chmod(0o755)
    monkeypatch.setenv("PATH", str(binaries))
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    observation_request = EnvironmentObservationRequest(
        actor_identity=token.principal_id,
        context_id="mission:build-api",
        repository_id="repo:test",
        repository_revision="deadbeef",
        execution_location="local:test",
    )
    observe_command = ApplicationCommand(
        command_type="environment.observe",
        actor_id=token.principal_id,
        target_type="environment_observation",
        target_id=str(observation_request.observation_id),
        payload={"request": observation_request.model_dump(mode="json")},
    )
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        observed_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=observe_command.model_dump(mode="json"),
        )
        assert observed_response.status_code == 200
        observation = EnvironmentObservation.model_validate(observed_response.json()["payload"])
        assert observation.manifests["go"] == ("go.mod",)
        assert observation.path_sensitivity == "machine_local"

        candidates_response = await client.get(
            f"/v1/environment/observations/{observation.observation_id}/command-candidates",
            headers=headers,
        )
        assert candidates_response.status_code == 200
        go_test = next(
            item
            for item in candidates_response.json()
            if item["pack_id"] == "go" and item["action"] == "test"
        )
        assert go_test["state"] == EngineeringCommandState.READY.value
        assert Path(go_test["executable"]).is_absolute()

        engineering_request = EngineeringCommandRequest(
            observation_id=observation.observation_id,
            observation_fingerprint=observation.fingerprint,
            pack_id="go",
            action="test",
            owner_identity=token.principal_id,
            run_id="run:pack-test",
            task_id="task:pack-test",
            session_profile="standard",
            deadline=utc_now() + timedelta(minutes=5),
            timeout_seconds=60,
        )
        command_plan_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.command.plan",
                actor_id=token.principal_id,
                target_type="engineering_command",
                target_id=str(engineering_request.request_id),
                payload={"request": engineering_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert command_plan_response.status_code == 200
        engineering_plan = EngineeringCommandPlan.model_validate(
            command_plan_response.json()["payload"]
        )
        assert engineering_plan.execution.executable == go_test["executable"]
        assert engineering_plan.execution.policy_fingerprint == "0" * 64

        session_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="session.start",
                actor_id=token.principal_id,
                target_type="session_service",
                payload={"request": engineering_plan.execution.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert session_response.status_code == 200
        session_id = session_response.json()["payload"]["execution_id"]
        settled = await _wait_for_session(client, headers, session_id)
        assert settled["state"] == "settled"
        assert settled["result"]["exit_code"] == 0
        output_response = await client.get(
            f"/v1/sessions/{session_id}/output",
            headers=headers,
        )
        assert output_response.json()["data"] == "pack-command-executed\n"

        binding_request = EnvironmentBindingRequest(
            mission_id="mission:test",
            plan_fingerprint="a" * 64,
            owner_identity=token.principal_id,
            context_id=observation.context_id,
            observation_id=observation.observation_id,
            observation_revision=observation.revision,
            observation_fingerprint=observation.fingerprint,
            requested_outcome=EnvironmentOutcome.UNRESOLVED,
            target_platform=observation.platform,
            target_architecture=observation.architecture,
            execution_location=observation.execution_location,
            affected_task_ids=("task:build",),
            verification_checks=("project-test",),
            policy_fingerprint="0" * 64,
            rationale="The accountable agent cannot yet select a compatible outcome.",
        )
        resolve_command = ApplicationCommand(
            command_type="environment.resolve",
            actor_id=token.principal_id,
            target_type="environment_binding_request",
            target_id=str(binding_request.request_id),
            payload={"request": binding_request.model_dump(mode="json")},
        )
        binding_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=resolve_command.model_dump(mode="json"),
        )
        assert binding_response.status_code == 200
        binding = EnvironmentBinding.model_validate(binding_response.json()["payload"])
        assert binding.state is EnvironmentBindingState.UNRESOLVED
        assert binding.request.policy_fingerprint != "0" * 64

        observed_query = await client.get(
            f"/v1/environment/observations/{observation.observation_id}",
            headers=headers,
        )
        binding_query = await client.get(
            f"/v1/environment/bindings/{binding.binding_id}",
            headers=headers,
        )
        events = await client.get("/v1/events", headers=headers)

    assert EnvironmentObservation.model_validate(observed_query.json()) == observation
    assert EnvironmentBinding.model_validate(binding_query.json()) == binding
    event_types = {event["event_type"] for event in events.json()["events"]}
    assert "environment.observed" in event_types
    assert "environment.command_planned" in event_types
    assert "environment.binding_unresolved" in event_types


@pytest.mark.anyio
async def test_environment_command_refuses_actor_impersonation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    request = EnvironmentObservationRequest(
        actor_identity="another-identity",
        context_id="mission:test",
        execution_location="local:test",
    )
    command = ApplicationCommand(
        command_type="environment.observe",
        actor_id=token.principal_id,
        target_type="environment_observation",
        target_id=str(request.observation_id),
        payload={"request": request.model_dump(mode="json")},
    )
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/commands",
            headers={"Authorization": f"Bearer {token.token}"},
            json=command.model_dump(mode="json"),
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_descriptor_validation_and_operation_plan_feed_the_job_supervisor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    podman = binaries / "podman"
    podman.write_text("#!/bin/sh\nprintf 'fake-podman:%s\\n' \"$*\"\n", encoding="utf-8")
    podman.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}")
    config = _config(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        observation_request = EnvironmentObservationRequest(
            actor_identity=token.principal_id,
            context_id="mission:oci",
            repository_revision="abc123",
            execution_location="local:test",
        )
        observed_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.observe",
                actor_id=token.principal_id,
                target_type="environment_observation",
                target_id=str(observation_request.observation_id),
                payload={"request": observation_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        observation = EnvironmentObservation.model_validate(observed_response.json()["payload"])
        binding_request = EnvironmentBindingRequest(
            mission_id="mission:test",
            plan_fingerprint="a" * 64,
            owner_identity=token.principal_id,
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
            verification_checks=("build",),
            policy_fingerprint="0" * 64,
            rationale="Use the observed fake Podman adapter in this transport test.",
        )
        binding_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.resolve",
                actor_id=token.principal_id,
                target_type="environment_binding_request",
                target_id=str(binding_request.request_id),
                payload={"request": binding_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        binding = EnvironmentBinding.model_validate(binding_response.json()["payload"])

        content = b"FROM scratch\n"
        artifact_config = config.artifacts
        persistence_config = config.persistence
        assert artifact_config is not None and persistence_config is not None
        artifact_service = DurableArtifactService(
            paths.database,
            paths.artifacts,
            max_artifact_bytes=artifact_config.max_artifact_bytes,
            max_chunk_bytes=artifact_config.chunk_bytes,
            busy_timeout_ms=persistence_config.busy_timeout_ms,
        )
        upload = artifact_service.open_upload(
            expected_size=len(content),
            expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            media_type="text/plain",
            provenance=ArtifactProvenance(
                producer_identity=token.principal_id,
                run_id="run:test",
                task_attempt_id="task:image",
                call_id="descriptor:test",
                capability="environment.descriptor",
                channel="environment.descriptor",
            ),
        )
        artifact_service.append_chunk(upload.upload_id, offset=0, content=content)
        manifest = artifact_service.commit_upload(upload.upload_id)
        descriptor_path = tmp_path / "Containerfile.generated"
        descriptor_set = EnvironmentDescriptorSet(
            binding_id=binding.binding_id,
            context_fingerprint=observation.fingerprint,
            target_platform=observation.platform,
            target_architecture=observation.architecture,
            members=(
                EnvironmentDescriptorMember(
                    format="containerfile",
                    logical_path=descriptor_path.name,
                    artifact_reference=manifest.reference,
                    base_revision="abc123",
                ),
            ),
        )
        validation_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.descriptor.validate",
                actor_id=token.principal_id,
                target_type="environment_descriptor_set",
                target_id=str(descriptor_set.descriptor_set_id),
                payload={"descriptor_set": descriptor_set.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert validation_response.status_code == 200
        assert validation_response.json()["payload"]["valid"] is True

        descriptor_change_request = EnvironmentDescriptorChangeRequest(
            descriptor_set_id=descriptor_set.descriptor_set_id,
            owner_identity=token.principal_id,
        )
        descriptor_change_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.descriptor.change.plan",
                actor_id=token.principal_id,
                target_type="environment_descriptor_change",
                target_id=str(descriptor_change_request.request_id),
                payload={"request": descriptor_change_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        descriptor_change = EnvironmentDescriptorChangePlan.model_validate(
            descriptor_change_response.json()["payload"]
        )
        assert descriptor_change.change_set is not None
        change_set = descriptor_change.change_set
        planned_change = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="change.plan",
                actor_id=token.principal_id,
                target_type="change_set",
                target_id=str(change_set.id),
                payload={"change_set": change_set.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert planned_change.status_code == 200
        applied_change = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="change.apply",
                actor_id=token.principal_id,
                target_type="change_set",
                target_id=str(change_set.id),
                expected_revision=planned_change.json()["revision"],
                payload={},
            ).model_dump(mode="json"),
        )
        assert applied_change.status_code == 200, applied_change.json()
        assert applied_change.json()["payload"]["state"] == "verified"
        assert descriptor_path.read_bytes() == content

        operation_request = EnvironmentOperationRequest(
            binding_id=binding.binding_id,
            descriptor_set_id=descriptor_set.descriptor_set_id,
            adapter_id="podman.cli",
            operation=EnvironmentOperation.BUILD,
            descriptor_path=descriptor_path.name,
            parameters={"image": "example.test/app:abc123"},
            owner_identity=token.principal_id,
            run_id="run:test",
            task_id="task:image",
            session_profile="standard",
            deadline=utc_now() + timedelta(minutes=5),
            timeout_seconds=60,
        )
        plan_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.operation.plan",
                actor_id=token.principal_id,
                target_type="environment_operation",
                target_id=str(operation_request.operation_id),
                payload={"request": operation_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert plan_response.status_code == 200
        plan = EnvironmentOperationPlan.model_validate(plan_response.json()["payload"])
        start_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="session.start",
                actor_id=token.principal_id,
                target_type="session_service",
                payload={"request": plan.execution.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert start_response.status_code == 200
        assert start_response.json()["status"] == "accepted", json.dumps(
            start_response.json().get("error"), indent=2
        )
        session_id = start_response.json()["payload"]["execution_id"]
        build_session = await _wait_for_session(client, headers, session_id)
        assert build_session["state"] == "uncertain"
        build_attempt_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.attempt.settle",
                actor_id=token.principal_id,
                target_type="environment_operation",
                target_id=str(plan.request.operation_id),
                payload={
                    "operation_plan": plan.model_dump(mode="json"),
                    "session_id": session_id,
                },
            ).model_dump(mode="json"),
        )
        assert build_attempt_response.json()["payload"]["settlement"] == "uncertain"
        build_attempt_id = build_attempt_response.json()["payload"]["attempt_id"]

        probe_request = EnvironmentOperationRequest(
            binding_id=binding.binding_id,
            adapter_id="podman.cli",
            operation=EnvironmentOperation.READINESS,
            parameters={"resource": "example.test/app:abc123"},
            owner_identity=token.principal_id,
            run_id="run:test",
            task_id="task:image",
            session_profile="standard",
            deadline=utc_now() + timedelta(minutes=5),
            timeout_seconds=60,
        )
        probe_plan_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.operation.plan",
                actor_id=token.principal_id,
                target_type="environment_operation",
                target_id=str(probe_request.operation_id),
                payload={"request": probe_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        probe_plan = EnvironmentOperationPlan.model_validate(probe_plan_response.json()["payload"])
        probe_start_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="session.start",
                actor_id=token.principal_id,
                target_type="session_service",
                payload={"request": probe_plan.execution.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        probe_session_id = probe_start_response.json()["payload"]["execution_id"]
        probe_session = await _wait_for_session(client, headers, probe_session_id)
        assert probe_session["state"] == "settled"
        probe_attempt_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.attempt.settle",
                actor_id=token.principal_id,
                target_type="environment_operation",
                target_id=str(probe_plan.request.operation_id),
                payload={
                    "operation_plan": probe_plan.model_dump(mode="json"),
                    "session_id": probe_session_id,
                },
            ).model_dump(mode="json"),
        )
        assert probe_attempt_response.json()["payload"]["settlement"] == "verified"
        probe_attempt_id = probe_attempt_response.json()["payload"]["attempt_id"]

        verification_request = EnvironmentVerificationRequest(
            binding_id=binding.binding_id,
            owner_identity=token.principal_id,
            context_fingerprint=observation.fingerprint,
            engine_id="podman",
            check_attempt_ids={"build": (build_attempt_id, probe_attempt_id)},
        )
        verification_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.verification.record",
                actor_id=token.principal_id,
                target_type="environment_verification",
                target_id=str(verification_request.verification_id),
                payload={"request": verification_request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        verification = EnvironmentVerification.model_validate(
            verification_response.json()["payload"]
        )
        assert verification.settlement is EnvironmentSettlement.VERIFIED

        invalidation = EnvironmentInvalidation(
            binding_id=binding.binding_id,
            expected_binding_revision=binding.revision,
            owner_identity=token.principal_id,
            cause=EnvironmentInvalidationCause.CONTEXT,
            observed_context_fingerprint="c" * 64,
            affected_task_ids=binding.request.affected_task_ids,
            policy_fingerprint="0" * 64,
            reason="The execution context changed after verification.",
        )
        invalidation_response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.binding.invalidate",
                actor_id=token.principal_id,
                target_type="environment_binding",
                target_id=str(binding.binding_id),
                payload={"invalidation": invalidation.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        assert invalidation_response.json()["status"] == "accepted"
        stale_response = await client.get(
            f"/v1/environment/bindings/{binding.binding_id}",
            headers=headers,
        )
        assert stale_response.json()["state"] == "stale"


async def _wait_for_session(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
) -> dict[str, object]:
    import asyncio

    for _ in range(100):
        response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        payload = dict(response.json())
        if payload.get("state") in {"settled", "failed", "lost", "uncertain"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("environment operation did not settle")
