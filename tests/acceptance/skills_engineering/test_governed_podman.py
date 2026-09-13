from __future__ import annotations

import asyncio
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
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentDescriptorMember,
    EnvironmentDescriptorSet,
    EnvironmentDescriptorValidator,
    EnvironmentObservationRequest,
    EnvironmentOperation,
    EnvironmentOperationPlan,
    EnvironmentOperationRequest,
    EnvironmentOutcome,
    EnvironmentResolver,
    EnvironmentSettlement,
    load_environment_profile,
)
from mishkan.environment.observer import EnvironmentObserver
from mishkan.environment.repository import SQLiteEnvironmentRepository

pytestmark = pytest.mark.container


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _require_podman() -> tuple[str, str]:
    podman = shutil.which("podman")
    compiler = shutil.which("cc")
    if sys.platform != "linux" or podman is None or compiler is None:
        pytest.skip("real governed Podman gate requires Linux, podman, and a C compiler")
    probe = subprocess.run(
        (podman, "info", "--format", "{{.Version.Version}}"),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        pytest.skip(f"Podman runtime is unavailable: {probe.stderr.strip()}")
    return podman, compiler


def _configuration(workspace: Path):  # type: ignore[no-untyped-def]
    source = workspace / "mishkan-config.yaml"
    source.write_text(preset_text("local"), encoding="utf-8")
    loaded = ConfigLoader().load([source]).value
    return loaded.model_copy(update={"project": ProjectConfig(workspace=workspace)})


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


async def _wait_for_state(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
    states: set[str],
) -> dict[str, object]:
    for _ in range(1_000):
        response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 200, response.text
        session = dict(response.json())
        if session.get("state") in states:
            return session
        await asyncio.sleep(0.02)
    raise AssertionError(f"Podman session did not reach one of {sorted(states)}")


async def _plan(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    binding_id: str,
    descriptor_set_id: str,
    principal_id: str,
    operation: EnvironmentOperation,
    descriptor_path: str | None = None,
    parameters: dict[str, str] | None = None,
) -> EnvironmentOperationPlan:
    request = EnvironmentOperationRequest(
        binding_id=binding_id,
        descriptor_set_id=descriptor_set_id,
        adapter_id="podman.cli",
        operation=operation,
        descriptor_path=descriptor_path,
        parameters=parameters or {},
        owner_identity=principal_id,
        run_id="run:podman-gate",
        task_id=f"task:{operation.value}",
        session_profile="standard",
        deadline=utc_now() + timedelta(minutes=10),
        timeout_seconds=300,
    )
    result = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="environment.operation.plan",
            actor_id=principal_id,
            target_type="environment_operation",
            target_id=str(request.operation_id),
            payload={"request": request.model_dump(mode="json")},
        ),
    )
    return EnvironmentOperationPlan.model_validate(result["payload"])


async def _start(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    plan: EnvironmentOperationPlan,
) -> str:
    started = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="session.start",
            actor_id=plan.request.owner_identity,
            target_type="session_service",
            payload={"request": plan.execution.model_dump(mode="json")},
        ),
    )
    return str(started["payload"]["execution_id"])  # type: ignore[index]


async def _settle_attempt(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    plan: EnvironmentOperationPlan,
    session_id: str,
) -> EnvironmentAttempt:
    result = await _command(
        client,
        headers,
        ApplicationCommand(
            command_type="environment.attempt.settle",
            actor_id=plan.request.owner_identity,
            target_type="environment_operation",
            target_id=str(plan.request.operation_id),
            payload={
                "operation_plan": plan.model_dump(mode="json"),
                "session_id": session_id,
            },
        ),
    )
    return EnvironmentAttempt.model_validate(result["payload"])


async def _run_terminal(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    plan: EnvironmentOperationPlan,
) -> EnvironmentAttempt:
    session_id = await _start(client, headers, plan)
    session = await _wait_for_state(
        client,
        headers,
        session_id,
        {"settled", "failed", "lost", "uncertain"},
    )
    assert session["result"] is not None
    assert session["result"]["exit_code"] == 0  # type: ignore[index]
    return await _settle_attempt(client, headers, plan, session_id)


@pytest.mark.anyio
async def test_real_podman_build_interrupt_cleanup_and_repeatability_through_mishkand(
    tmp_path: Path,
) -> None:
    podman, compiler = _require_podman()
    suffix = uuid4().hex[:12]
    image = f"localhost/mishkan-i05-gate:{suffix}"
    resource = f"mishkan-i05-{suffix}"
    source = tmp_path / "probe.c"
    binary = tmp_path / "probe"
    containerfile = tmp_path / "Containerfile"
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
    content = b'FROM scratch\nCOPY probe /probe\nENTRYPOINT ["/probe"]\n'
    containerfile.write_bytes(content)
    config = _configuration(tmp_path)
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    assert config.engineering_profile is not None
    profile = load_environment_profile(config.engineering_profile, tmp_path)
    observation = EnvironmentObserver(profile).observe(
        tmp_path,
        request=EnvironmentObservationRequest(
            actor_identity=token.principal_id,
            context_id="mission:podman-gate",
            repository_revision="gate-revision",
            execution_location="local:podman",
        ),
    )
    persistence = config.persistence
    artifact_config = config.artifacts
    assert persistence is not None and artifact_config is not None
    artifacts = DurableArtifactService(
        paths.database,
        paths.artifacts,
        max_artifact_bytes=artifact_config.max_artifact_bytes,
        max_chunk_bytes=artifact_config.chunk_bytes,
        busy_timeout_ms=persistence.busy_timeout_ms,
    )
    repository = SQLiteEnvironmentRepository(
        paths.database,
        artifacts=artifacts,
        busy_timeout_ms=persistence.busy_timeout_ms,
    )
    repository.record_observation(observation)
    request = EnvironmentBindingRequest(
        mission_id="mission:podman-gate",
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
        required_semantics=("oci.build", "oci.run"),
        allowed_descriptor_formats=("containerfile",),
        required_engine_ids=("podman",),
        authorized_engine_ids=("podman",),
        affected_task_ids=("task:podman-gate",),
        verification_checks=("runtime", "cleanup"),
        policy_fingerprint="0" * 64,
        rationale="Exercise Podman lifecycle and interruption on a compatible worker.",
    )
    binding = EnvironmentResolver(freshness_seconds=profile.freshness_seconds).resolve(
        request, observation
    )
    assert binding.state is EnvironmentBindingState.COMPATIBLE
    repository.record_binding(binding)
    upload = artifacts.open_upload(
        expected_size=len(content),
        expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
        media_type="text/plain",
        provenance=ArtifactProvenance(
            producer_identity=token.principal_id,
            run_id="run:podman-gate",
            task_attempt_id="task:podman-gate",
            call_id="descriptor:containerfile",
            capability="environment.descriptor",
            channel="environment.descriptor",
        ),
    )
    artifacts.append_chunk(upload.upload_id, offset=0, content=content)
    reference = artifacts.commit_upload(upload.upload_id).reference
    descriptor_set = EnvironmentDescriptorSet(
        binding_id=binding.binding_id,
        context_fingerprint=observation.fingerprint,
        target_platform=observation.platform,
        target_architecture=observation.architecture,
        members=(
            EnvironmentDescriptorMember(
                format="containerfile",
                logical_path="Containerfile",
                artifact_reference=reference,
                base_revision="gate-revision",
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
    transport = httpx.ASGITransport(app=create_app(config))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            build_plan = await _plan(
                client,
                headers,
                binding_id=str(binding.binding_id),
                descriptor_set_id=str(descriptor_set.descriptor_set_id),
                principal_id=token.principal_id,
                operation=EnvironmentOperation.BUILD,
                descriptor_path="Containerfile",
                parameters={"image": image},
            )
            build = await _run_terminal(client, headers, build_plan)
            assert build.settlement is EnvironmentSettlement.UNCERTAIN

            for _cycle in range(2):
                start_plan = await _plan(
                    client,
                    headers,
                    binding_id=str(binding.binding_id),
                    descriptor_set_id=str(descriptor_set.descriptor_set_id),
                    principal_id=token.principal_id,
                    operation=EnvironmentOperation.START,
                    parameters={"resource": resource, "image": image},
                )
                session_id = await _start(client, headers, start_plan)
                running = await _wait_for_state(
                    client,
                    headers,
                    session_id,
                    {"running", "ready", "settled", "failed", "lost", "uncertain"},
                )
                assert running["state"] in {"running", "ready"}, running
                readiness_plan = await _plan(
                    client,
                    headers,
                    binding_id=str(binding.binding_id),
                    descriptor_set_id=str(descriptor_set.descriptor_set_id),
                    principal_id=token.principal_id,
                    operation=EnvironmentOperation.READINESS,
                    parameters={"resource": resource},
                )
                readiness = await _run_terminal(client, headers, readiness_plan)
                assert readiness.settlement is EnvironmentSettlement.VERIFIED
                cancelled = await _command(
                    client,
                    headers,
                    ApplicationCommand(
                        command_type="session.cancel",
                        actor_id=token.principal_id,
                        target_type="session",
                        target_id=session_id,
                        payload={},
                    ),
                )
                assert cancelled["payload"]["cancellation_requested_at"] is not None  # type: ignore[index]
                await _wait_for_state(
                    client,
                    headers,
                    session_id,
                    {"settled", "failed", "lost", "uncertain"},
                )
                interrupted = await _settle_attempt(client, headers, start_plan, session_id)
                assert interrupted.settlement in {
                    EnvironmentSettlement.CANCELLED,
                    EnvironmentSettlement.UNCERTAIN,
                }
                stop_plan = await _plan(
                    client,
                    headers,
                    binding_id=str(binding.binding_id),
                    descriptor_set_id=str(descriptor_set.descriptor_set_id),
                    principal_id=token.principal_id,
                    operation=EnvironmentOperation.STOP,
                    parameters={"resource": resource},
                )
                stopped = await _run_terminal(client, headers, stop_plan)
                assert stopped.settlement is EnvironmentSettlement.UNCERTAIN
                cleanup_plan = await _plan(
                    client,
                    headers,
                    binding_id=str(binding.binding_id),
                    descriptor_set_id=str(descriptor_set.descriptor_set_id),
                    principal_id=token.principal_id,
                    operation=EnvironmentOperation.CLEANUP,
                    parameters={"resource": resource},
                )
                cleaned = await _run_terminal(client, headers, cleanup_plan)
                assert cleaned.settlement is EnvironmentSettlement.UNCERTAIN
                exists = subprocess.run(
                    (podman, "container", "exists", resource),
                    check=False,
                    capture_output=True,
                    timeout=30,
                )
                assert exists.returncode != 0
    finally:
        subprocess.run(
            (podman, "rm", "--force", resource),
            check=False,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            (podman, "rmi", "--force", image),
            check=False,
            capture_output=True,
            timeout=30,
        )
