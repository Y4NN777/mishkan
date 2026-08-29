from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
from mishkan.config.loader import ConfigLoader
from mishkan.config.models import ProjectConfig
from mishkan.config.presets import preset_text
from mishkan.daemon import DaemonBootstrap, create_app
from mishkan.daemon.auth import TokenFile
from mishkan.environment import (
    EnvironmentBinding,
    EnvironmentBindingRequest,
    EnvironmentBindingState,
    EnvironmentObservation,
    EnvironmentObservationRequest,
    EnvironmentOutcome,
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
) -> None:
    (tmp_path / "go.mod").write_text("module example.test/project\n", encoding="utf-8")
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
