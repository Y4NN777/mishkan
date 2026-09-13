from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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


def _require_devcontainer() -> tuple[str, str]:
    devcontainer = shutil.which("devcontainer")
    docker = shutil.which("docker")
    if devcontainer is None or docker is None:
        pytest.skip("real Dev Container gate requires devcontainer and docker")
    probe = subprocess.run(
        (docker, "info", "--format", "{{.ServerVersion}}"),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"Docker daemon is unavailable: {probe.stderr.strip()}")
    return devcontainer, docker


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


async def _settle(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    plan: EnvironmentOperationPlan,
) -> tuple[EnvironmentAttempt, str]:
    import asyncio

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
    session_id = str(started["payload"]["execution_id"])  # type: ignore[index]
    session: dict[str, object] = {}
    for _ in range(1_000):
        response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        assert response.status_code == 200, response.text
        session = dict(response.json())
        if session.get("state") in {"settled", "failed", "lost", "uncertain"}:
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError("governed Dev Container operation did not settle")
    assert session["result"] is not None
    assert session["result"]["exit_code"] == 0  # type: ignore[index]
    outputs: list[str] = []
    for channel in ("stdout", "stderr"):
        response = await client.get(
            f"/v1/sessions/{session_id}/output",
            headers=headers,
            params={"channel": channel, "offset": 0, "limit": 1_000_000},
        )
        assert response.status_code == 200, response.text
        outputs.append(str(response.json()["data"]))
    recorded = await _command(
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
    return EnvironmentAttempt.model_validate(recorded["payload"]), "".join(outputs)


async def _plan(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    binding: EnvironmentBinding,
    descriptor_set: EnvironmentDescriptorSet,
    operation: EnvironmentOperation,
) -> EnvironmentOperationPlan:
    request = EnvironmentOperationRequest(
        binding_id=binding.binding_id,
        descriptor_set_id=descriptor_set.descriptor_set_id,
        adapter_id="devcontainer.cli",
        operation=operation,
        descriptor_path=".devcontainer/devcontainer.json",
        owner_identity=binding.request.owner_identity,
        run_id="run:devcontainer-gate",
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
            actor_id=binding.request.owner_identity,
            target_type="environment_operation",
            target_id=str(request.operation_id),
            payload={"request": request.model_dump(mode="json")},
        ),
    )
    return EnvironmentOperationPlan.model_validate(result["payload"])


@pytest.mark.anyio
async def test_existing_devcontainer_is_materialized_and_probed_through_mishkand(
    tmp_path: Path,
) -> None:
    devcontainer, docker = _require_devcontainer()
    definition = tmp_path / ".devcontainer" / "devcontainer.json"
    definition.parent.mkdir()
    definition.write_text(
        json.dumps(
            {
                "name": "mishkan-i05-devcontainer-gate",
                "image": "alpine:3.21",
                "overrideCommand": True,
            }
        )
        + "\n",
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
                context_id="mission:devcontainer-gate",
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
            engine = next(
                item for item in observation.engines if item.engine_id == "devcontainer-cli"
            )
            assert engine.executable_path == Path(devcontainer)
            assert engine.fact("eligible").value == "true"
            binding_request = EnvironmentBindingRequest(
                mission_id="mission:devcontainer-gate",
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
                required_semantics=("devcontainer.lifecycle",),
                allowed_descriptor_formats=("devcontainer",),
                required_engine_ids=("devcontainer-cli",),
                authorized_engine_ids=("devcontainer-cli",),
                affected_task_ids=("task:devcontainer-gate",),
                verification_checks=("configuration", "materialization"),
                policy_fingerprint="0" * 64,
                rationale="Reuse and verify the existing development container definition.",
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
            artifact_config = config.artifacts
            assert persistence is not None and artifact_config is not None
            artifacts = DurableArtifactService(
                paths.database,
                paths.artifacts,
                max_artifact_bytes=artifact_config.max_artifact_bytes,
                max_chunk_bytes=artifact_config.chunk_bytes,
                busy_timeout_ms=persistence.busy_timeout_ms,
            )
            content = definition.read_bytes()
            upload = artifacts.open_upload(
                expected_size=len(content),
                expected_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
                media_type="application/json",
                provenance=ArtifactProvenance(
                    producer_identity=token.principal_id,
                    run_id="run:devcontainer-gate",
                    task_attempt_id="task:devcontainer-gate",
                    call_id="descriptor:devcontainer",
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
                        format="devcontainer",
                        logical_path=".devcontainer/devcontainer.json",
                        artifact_reference=reference,
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

            validate_plan = await _plan(
                client, headers, binding, descriptor_set, EnvironmentOperation.VALIDATE
            )
            assert Path(validate_plan.execution.executable) == Path(devcontainer)
            assert str(Path(docker).parent) in validate_plan.execution.environment["PATH"]
            validation, validation_output = await _settle(client, headers, validate_plan)
            assert validation.settlement is EnvironmentSettlement.VERIFIED
            assert "configuration" in validation_output

            for _cycle in range(2):
                materialize_plan = await _plan(
                    client,
                    headers,
                    binding,
                    descriptor_set,
                    EnvironmentOperation.MATERIALIZE,
                )
                materialized, materialization_output = await _settle(
                    client, headers, materialize_plan
                )
                assert materialized.settlement is EnvironmentSettlement.UNCERTAIN
                assert '"outcome":"success"' in materialization_output.replace(" ", "")
                readiness_plan = await _plan(
                    client,
                    headers,
                    binding,
                    descriptor_set,
                    EnvironmentOperation.READINESS,
                )
                readiness, readiness_output = await _settle(client, headers, readiness_plan)
                assert readiness.settlement is EnvironmentSettlement.VERIFIED
                assert readiness.exit_code == 0
                assert readiness_output == ""
    finally:
        listed = subprocess.run(
            (
                docker,
                "ps",
                "--all",
                "--quiet",
                "--filter",
                f"label=devcontainer.local_folder={tmp_path}",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        identifiers = tuple(item for item in listed.stdout.splitlines() if item)
        if identifiers:
            subprocess.run(
                (docker, "rm", "--force", *identifiers),
                check=False,
                capture_output=True,
                timeout=30,
            )
