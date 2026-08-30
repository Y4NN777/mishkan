from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from mishkan.application import ApplicationCommand
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
    EnvironmentObservation,
    EnvironmentObservationRequest,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _settled(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    session_id: str,
) -> dict[str, object]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/sessions/{session_id}", headers=headers)
        payload: dict[str, object] = response.json()
        if payload["state"] in {"settled", "failed", "lost", "uncertain"}:
            return payload
        await asyncio.sleep(0.05)
    pytest.fail(f"engineering command session {session_id} did not settle")


@pytest.mark.acceptance
@pytest.mark.anyio
async def test_polyglot_packs_execute_observed_engines_and_report_every_other_as_unavailable(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parents[2] / "fixtures" / "engineering" / "polyglot"
    shutil.copytree(fixture, tmp_path, dirs_exist_ok=True)
    config_source = tmp_path / "mishkan-config.yaml"
    config_source.write_text(preset_text("local"), encoding="utf-8")
    base = ConfigLoader().load([config_source]).value
    config = base.model_copy(update={"project": ProjectConfig(workspace=tmp_path)})
    paths = DaemonBootstrap().setup(config)
    token = TokenFile(paths.token_file).read()
    headers = {"Authorization": f"Bearer {token.token}"}
    request = EnvironmentObservationRequest(
        actor_identity=token.principal_id,
        context_id="acceptance:polyglot",
        repository_revision="fixture-v1",
        execution_location="local:acceptance",
    )
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/v1/commands",
            headers=headers,
            json=ApplicationCommand(
                command_type="environment.observe",
                actor_id=token.principal_id,
                target_type="environment_observation",
                target_id=str(request.observation_id),
                payload={"request": request.model_dump(mode="json")},
            ).model_dump(mode="json"),
        )
        observation = EnvironmentObservation.model_validate(response.json()["payload"])
        candidates_response = await client.get(
            f"/v1/environment/observations/{observation.observation_id}/command-candidates",
            headers=headers,
        )
        candidates = candidates_response.json()
        expected_packs = {
            "go",
            "javascript-typescript",
            "java-kotlin",
            "android",
            "python",
            "rust",
            "c-native",
            "swift",
            "maestro-mobile-ui",
        }
        assert expected_packs <= {item["pack_id"] for item in candidates}
        engines = {item.engine_id: item for item in observation.engines}
        for candidate in candidates:
            engine_id = candidate.get("engine_id")
            if candidate["state"] == EngineeringCommandState.READY.value:
                assert engine_id is not None
                engine = engines[engine_id]
                assert engine.executable_path is not None
                assert Path(engine.executable_path).is_file()
                assert os.access(engine.executable_path, os.X_OK)
            else:
                assert candidate["state"] == EngineeringCommandState.UNAVAILABLE.value
                assert "compatible-engine:unavailable" in candidate["evidence"]

        executable_actions = {
            "go": "test",
            "javascript-typescript": "test",
            "python": "validate",
            "rust": "test",
            "c-native": "build",
            "swift": "test",
        }
        executed = 0
        for pack_id, action in executable_actions.items():
            candidate = next(
                item
                for item in candidates
                if item["pack_id"] == pack_id and item["action"] == action
            )
            if candidate["state"] != EngineeringCommandState.READY.value:
                continue
            plan_request = EngineeringCommandRequest(
                observation_id=observation.observation_id,
                observation_fingerprint=observation.fingerprint,
                pack_id=pack_id,
                action=action,
                owner_identity=token.principal_id,
                run_id="run:polyglot",
                task_id=f"task:{pack_id}",
                session_profile="standard",
                deadline=utc_now() + timedelta(minutes=5),
                timeout_seconds=120,
            )
            planned = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="environment.command.plan",
                    actor_id=token.principal_id,
                    target_type="engineering_command",
                    target_id=str(plan_request.request_id),
                    payload={"request": plan_request.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            plan = EngineeringCommandPlan.model_validate(planned.json()["payload"])
            started = await client.post(
                "/v1/commands",
                headers=headers,
                json=ApplicationCommand(
                    command_type="session.start",
                    actor_id=token.principal_id,
                    target_type="session_service",
                    payload={"request": plan.execution.model_dump(mode="json")},
                ).model_dump(mode="json"),
            )
            session = await _settled(client, headers, started.json()["payload"]["execution_id"])
            result = session["result"]
            assert isinstance(result, dict)
            assert result["exit_code"] == 0, (pack_id, result)
            executed += 1

    assert executed >= 1
