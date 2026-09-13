from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

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
    EnvironmentAttempt,
    EnvironmentBinding,
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentObservation,
    EnvironmentObservationRequest,
    EnvironmentOperation,
    EnvironmentOperationPlan,
    EnvironmentOperationRequest,
    EnvironmentOutcome,
    EnvironmentSettlement,
)

pytestmark = pytest.mark.container


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _require_docker() -> tuple[str, str]:
    docker = shutil.which("docker")
    compiler = shutil.which("cc")
    if sys.platform != "linux" or docker is None or compiler is None:
        pytest.skip("real governed Docker gate requires Linux, docker, and a C compiler")
    probe = subprocess.run(
        (docker, "info", "--format", "{{.ServerVersion}}"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"Docker daemon is unavailable: {probe.stderr.strip()}")
    return docker, compiler


def _configuration(workspace: Path):  # type: ignore[no-untyped-def]
    source = workspace / "mishkan-config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=workspace)})


def _store_descriptor(
    artifacts: DurableArtifactService,
    *,
    content: bytes,
    principal_id: str,
    logical_path: str,
) -> str:
    upload = artifacts.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="text/plain",
        provenance=ArtifactProvenance(
            producer_identity=principal_id,
            run_id="run:docker-compose-gate",
            task_attempt_id="task:docker-compose-gate",
            call_id=f"descriptor:{logical_path}",
            capability="environment.descriptor",
            channel="environment.descriptor",
        ),
    )
    artifacts.append_chunk(upload.upload_id, offset=0, content=content)
    return artifacts.commit_upload(upload.upload_id).reference


async def _command(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    command: ApplicationCommand,
) -> dict[str, object]:
    response = await client.post(
        "/v1/commands",
        headers=headers,
        json=command.model_dump(mode="json"),
    )
    assert response.status_code == 200, response.text
    payload = dict(response.json())
    assert payload["status"] == "accepted", payload.get("error")
    return payload


async def _wait_for_session(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
) -> dict[str, object]:
    import asyncio

    for _ in range(500):
        response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 200, response.text
        payload = dict(response.json())
        if payload.get("state") in {"settled", "failed", "lost", "uncertain"}:
            return payload
        await asyncio.sleep(0.02)
    raise AssertionError("governed Docker operation did not settle")


async def _run_operation(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    binding: EnvironmentBinding,
    descriptor_set: EnvironmentDescriptorSet,
    operation: EnvironmentOperation,
    descriptor_path: str,
    parameters: dict[str, str] | None = None,
) -> tuple[EnvironmentAttempt, str]:
    request = EnvironmentOperationRequest(
        binding_id=binding.binding_id,
        descriptor_set_id=descriptor_set.descriptor_set_id,
        adapter_id="docker.cli",
        operation=operation,
        descriptor_path=descriptor_path,
        parameters=parameters or {},
        owner_identity=binding.request.owner_identity,
        run_id="run:docker-compose-gate",
        task_id=f"task:{operation.value}",
        session_profile="standard",
        deadline=utc_now() + timedelta(minutes=5),
        timeout_seconds=120,
    )
    planned = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="environment.operation.plan",
            actor_id=binding.request.owner_identity,
            target_type="environment_operation",
            target_id=str(request.operation_id),
            payload={"request": request.model_dump(mode="json")},
        ),
    )
    plan = EnvironmentOperationPlan.model_validate(planned["payload"])
    started = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="session.start",
            actor_id=binding.request.owner_identity,
            target_type="session_service",
            payload={"request": plan.execution.model_dump(mode="json")},
        ),
    )
    session_id = str(started["payload"]["execution_id"])  # type: ignore[index]
    session = await _wait_for_session(client, headers, session_id)
    assert session["result"] is not None
    assert session["result"]["exit_code"] == 0  # type: ignore[index]
    outputs: list[str] = []
    for channel in ("stdout", "stderr"):
        output_response = await client.get(
            f"/v1/sessions/{session_id}/output",
            headers=headers,
            params={"channel": channel, "offset": 0, "limit": 1_000_000},
        )
        assert output_response.status_code == 200, output_response.text
        outputs.append(str(output_response.json()["data"]))
    settled = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="environment.attempt.settle",
            actor_id=binding.request.owner_identity,
            target_type="environment_operation",
            target_id=str(request.operation_id),
            payload={
                "operation_plan": plan.model_dump(mode="json"),
                "session_id": session_id,
            },
        ),
    )
    return EnvironmentAttempt.model_validate(settled["payload"]), "".join(outputs)


@pytest.mark.anyio
async def test_real_docker_compose_lifecycle_runs_twice_through_mishkand(
    tmp_path: Path,
) -> None:
    docker, compiler = _require_docker()
    suffix = uuid4().hex[:12]
    image = f"mishkan-i05-gate:{suffix}"
    project = f"mishkan-i05-{suffix}"
    source = tmp_path / "probe.c"
    binary = tmp_path / "probe"
    dockerfile = tmp_path / "Dockerfile"
    compose = tmp_path / "compose.yaml"
    source.write_text(
        "#include <unistd.h>\nint main(void) { for (;;) pause(); }\n",
        encoding="utf-8",
    )
    compiled = subprocess.run(
        (compiler, "-static", "-Os", "-o", str(binary), str(source)),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if compiled.returncode != 0:
        pytest.skip(f"static C prerequisite is unavailable: {compiled.stderr.strip()}")
    dockerfile.write_text(
        'FROM scratch\nCOPY probe /probe\nENTRYPOINT ["/probe"]\n',
        encoding="utf-8",
    )
    compose.write_text(
        "name: "
        + project
        + "\nservices:\n"
        + f"  first:\n    image: {image}\n"
        + f"  second:\n    image: {image}\n",
        encoding="utf-8",
    )

    config = _configuration(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    transport = httpx.ASGITransport(app=create_app(config))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            observation_request = EnvironmentObservationRequest(
                actor_identity=token.principal_id,
                context_id="mission:docker-compose-gate",
                repository_revision="gate-revision",
                execution_location="local:docker-daemon",
            )
            observed = await _command(
                client,
                headers,
                ApplicationCommand(
                    command_type="environment.observe",
                    actor_id=token.principal_id,
                    target_type="environment_observation",
                    target_id=str(observation_request.observation_id),
                    payload={"request": observation_request.model_dump(mode="json")},
                ),
            )
            observation = EnvironmentObservation.model_validate(observed["payload"])
            docker_engine = next(item for item in observation.engines if item.engine_id == "docker")
            assert docker_engine.executable_path == Path(docker)
            assert docker_engine.fact("eligible").value == "true"

            binding_request = EnvironmentBindingRequest(
                mission_id="mission:docker-compose-gate",
                plan_fingerprint="a" * 64,
                owner_identity=token.principal_id,
                context_id=observation.context_id,
                observation_id=observation.observation_id,
                observation_revision=observation.revision,
                observation_fingerprint=observation.fingerprint,
                requested_outcome=EnvironmentOutcome.REUSE_EXISTING,
                target_platform=observation.platform,
                target_architecture=observation.architecture,
                execution_location=observation.execution_location,
                required_semantics=("oci.build", "compose.lifecycle"),
                allowed_descriptor_formats=("containerfile", "compose"),
                required_engine_ids=("docker",),
                authorized_engine_ids=("docker",),
                affected_task_ids=("task:docker-compose-gate",),
                verification_checks=("compose-running", "compose-cleanup"),
                policy_fingerprint="0" * 64,
                rationale="Exercise the accepted Docker and Compose lifecycle on this host.",
            )
            resolved = await _command(
                client,
                headers,
                ApplicationCommand(
                    command_type="environment.resolve",
                    actor_id=token.principal_id,
                    target_type="environment_binding_request",
                    target_id=str(binding_request.request_id),
                    payload={"request": binding_request.model_dump(mode="json")},
                ),
            )
            binding = EnvironmentBinding.model_validate(resolved["payload"])
            assert binding.state is EnvironmentBindingState.COMPATIBLE

            persistence = config.persistence
            artifacts_config = config.artifacts
            assert persistence is not None and artifacts_config is not None
            artifacts = DurableArtifactService(
                paths.database,
                paths.artifacts,
                max_artifact_bytes=artifacts_config.max_artifact_bytes,
                max_chunk_bytes=artifacts_config.chunk_bytes,
                busy_timeout_ms=persistence.busy_timeout_ms,
            )
            descriptor_set = EnvironmentDescriptorSet(
                binding_id=binding.binding_id,
                context_fingerprint=observation.fingerprint,
                target_platform=observation.platform,
                target_architecture=observation.architecture,
                members=(
                    EnvironmentDescriptorMember(
                        format="containerfile",
                        logical_path="Dockerfile",
                        artifact_reference=_store_descriptor(
                            artifacts,
                            content=dockerfile.read_bytes(),
                            principal_id=token.principal_id,
                            logical_path="Dockerfile",
                        ),
                        base_revision="gate-revision",
                    ),
                    EnvironmentDescriptorMember(
                        format="compose",
                        logical_path="compose.yaml",
                        artifact_reference=_store_descriptor(
                            artifacts,
                            content=compose.read_bytes(),
                            principal_id=token.principal_id,
                            logical_path="compose.yaml",
                        ),
                        base_revision="gate-revision",
                    ),
                ),
            )
            validated = await _command(
                client,
                headers,
                ApplicationCommand(
                    command_type="environment.descriptor.validate",
                    actor_id=token.principal_id,
                    target_type="environment_descriptor_set",
                    target_id=str(descriptor_set.descriptor_set_id),
                    payload={"descriptor_set": descriptor_set.model_dump(mode="json")},
                ),
            )
            assert validated["payload"]["valid"] is True  # type: ignore[index]

            build, build_output = await _run_operation(
                client,
                headers,
                binding=binding,
                descriptor_set=descriptor_set,
                operation=EnvironmentOperation.BUILD,
                descriptor_path="Dockerfile",
                parameters={"image": image},
            )
            assert build.settlement is EnvironmentSettlement.UNCERTAIN
            assert "external effect requires" in build.limitations[0]
            assert "naming to" in build_output.lower() or "exporting" in build_output.lower()

            for _cycle in range(2):
                materialized, _ = await _run_operation(
                    client,
                    headers,
                    binding=binding,
                    descriptor_set=descriptor_set,
                    operation=EnvironmentOperation.MATERIALIZE,
                    descriptor_path="compose.yaml",
                )
                assert materialized.settlement is EnvironmentSettlement.UNCERTAIN
                readiness, running_output = await _run_operation(
                    client,
                    headers,
                    binding=binding,
                    descriptor_set=descriptor_set,
                    operation=EnvironmentOperation.READINESS,
                    descriptor_path="compose.yaml",
                )
                assert readiness.settlement is EnvironmentSettlement.VERIFIED
                assert "first" in running_output and "second" in running_output
                cleaned, _ = await _run_operation(
                    client,
                    headers,
                    binding=binding,
                    descriptor_set=descriptor_set,
                    operation=EnvironmentOperation.CLEANUP,
                    descriptor_path="compose.yaml",
                )
                assert cleaned.settlement is EnvironmentSettlement.UNCERTAIN
                empty, cleanup_output = await _run_operation(
                    client,
                    headers,
                    binding=binding,
                    descriptor_set=descriptor_set,
                    operation=EnvironmentOperation.READINESS,
                    descriptor_path="compose.yaml",
                )
                assert empty.settlement is EnvironmentSettlement.VERIFIED
                assert "first" not in cleanup_output and "second" not in cleanup_output
    finally:
        subprocess.run(
            (docker, "compose", "--file", str(compose), "down", "--remove-orphans"),
            check=False,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            (docker, "image", "rm", "--force", image),
            check=False,
            capture_output=True,
            timeout=30,
        )
