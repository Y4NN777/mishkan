from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from mishkan.application.initialize import MishkanInitializer
from mishkan.config.models import MishkanConfig

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1"
DEFAULT_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "openrouter/free"
DEFAULT_FALLBACK_MODEL = "stealth/ox-alpha"


def _setting(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _credential_environment_name() -> str:
    key_env = _setting("MISHKAN_CLOUD_KEY_ENV", DEFAULT_KEY_ENV)
    if not key_env:
        pytest.fail("MISHKAN_CLOUD_KEY_ENV must name a credential environment variable")
    return key_env


def _require_cloud_credential(key_env: str) -> None:
    if not os.environ.get(key_env):
        pytest.skip(f"cloud credential environment variable is unavailable: {key_env}")


def _models(primary_env: str) -> list[dict[str, str]]:
    primary = _setting(primary_env, DEFAULT_MODEL)
    fallback = _setting("MISHKAN_CLOUD_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)
    if not primary:
        pytest.fail(f"{primary_env} must identify a cloud model")
    return [
        {"provider": "cloud-gate", "model": model}
        for model in dict.fromkeys((primary, fallback))
        if model
    ]


def _repository(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text(
        "# Weather Ledger\nA Python library for immutable weather observations.\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'weather-ledger'\nrequires-python = '>=3.11'\n",
        encoding="utf-8",
    )
    package = root / "src" / "weather_ledger"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Weather ledger."""\n', encoding="utf-8")
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
    return root


def _config(workspace: Path, key_env: str) -> MishkanConfig:
    endpoint = _setting("MISHKAN_CLOUD_ENDPOINT", DEFAULT_ENDPOINT)
    if not endpoint:
        pytest.fail("MISHKAN_CLOUD_ENDPOINT must identify an OpenAI-compatible endpoint")
    return MishkanConfig.model_validate(
        {
            "schema_version": "1.1",
            "mode": "cloud",
            "timezone": "UTC",
            "project": {"workspace": str(workspace)},
            "providers": {
                "cloud-gate": {
                    "kind": "openai-compatible",
                    "endpoint": endpoint,
                    "credential_pool": [{"source": "env", "locator": key_env}],
                }
            },
            "model_routes": {
                "planning": {"candidates": _models("MISHKAN_CLOUD_PLANNING_MODEL")},
                "execution": {"candidates": _models("MISHKAN_CLOUD_EXECUTION_MODEL")},
            },
            "policy_sources": [
                "package://mishkan.resources.policies/i02-local.yaml",
            ],
            "tool_sources": [
                "package://mishkan.resources.tools/core-catalog.yaml",
            ],
            "inspection_profile": "package://mishkan.resources.inspection/i02-default.yaml",
            "isolation_profiles": [
                "package://mishkan.resources.isolation/local-no-network.yaml",
            ],
            "crewai": {
                "tracing": False,
                "telemetry": False,
                "temperature": 0,
                "model_timeout_seconds": 300,
                "model_transport_retries": 0,
                "model_max_output_tokens": 8192,
                "max_agent_iterations": 8,
                "task_execution_retries": 1,
                "plan_validation_retries": 1,
                "review_retries": 1,
                "structured_output_retries": 1,
            },
        }
    )


@pytest.mark.acceptance
@pytest.mark.cloud
def test_real_crewai_cloud_init_and_resume(tmp_path: Path) -> None:
    key_env = _credential_environment_name()
    repository = _repository(tmp_path / "weather-ledger")
    config = _config(repository, key_env)
    _require_cloud_credential(key_env)

    first = MishkanInitializer().run(config, repository, "Initialize this library from evidence")
    resumed = MishkanInitializer().run(
        config,
        repository,
        "Initialize this library from evidence",
    )

    assert first.resumed is False
    assert len(first.results) == len(first.reviews) == 1
    assert first.reviews[0].verdict == "accepted"
    assert first.results[0].cited_paths
    assert resumed.resumed is True
    assert resumed.plan_fingerprint == first.plan_fingerprint
    assert resumed.results == first.results
    assert (repository / ".mishkan" / "mishkan.db").is_file()
    assert {provider.kind for provider in config.providers.values()} == {"openai-compatible"}
