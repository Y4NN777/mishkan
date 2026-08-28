from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from mishkan.application.initialize import MishkanInitializer
from mishkan.config.models import MishkanConfig

OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_PLANNING_MODEL = "qwen2.5-coder-7b-16k:latest"
OLLAMA_EXECUTION_MODEL = "deepseek-coder-v2:16b"


def _require_ollama() -> None:
    try:
        with urlopen(f"{OLLAMA_ENDPOINT}/api/tags", timeout=2) as response:
            payload = json.load(response)
    except (OSError, URLError) as exc:
        pytest.skip(f"local Ollama is unavailable: {type(exc).__name__}")
    names = {model["name"] for model in payload.get("models", [])}
    required = {OLLAMA_PLANNING_MODEL, OLLAMA_EXECUTION_MODEL}
    missing = sorted(required - names)
    if missing:
        pytest.skip(f"local Ollama models are not installed: {missing}")


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


def _config(workspace: Path) -> MishkanConfig:
    return MishkanConfig.model_validate(
        {
            "schema_version": "1.1",
            "mode": "local",
            "timezone": "UTC",
            "project": {"workspace": str(workspace)},
            "providers": {
                "ollama-local": {
                    "kind": "ollama",
                    "endpoint": OLLAMA_ENDPOINT,
                }
            },
            "model_routes": {
                "planning": {
                    "candidates": [
                        {"provider": "ollama-local", "model": OLLAMA_PLANNING_MODEL},
                    ]
                },
                "execution": {
                    "candidates": [
                        {"provider": "ollama-local", "model": OLLAMA_EXECUTION_MODEL},
                    ]
                },
            },
            "policy_sources": [
                "package://mishkan.resources.policies/local-control-plane.yaml",
            ],
            "tool_sources": [
                "package://mishkan.resources.tools/core-catalog.yaml",
            ],
            "inspection_profile": ("package://mishkan.resources.inspection/default-security.yaml"),
            "isolation_profiles": [
                "package://mishkan.resources.isolation/local-no-network.yaml",
            ],
            "crewai": {
                "tracing": False,
                "telemetry": False,
                "temperature": 0,
                "model_timeout_seconds": 300,
                "model_transport_retries": 0,
                "model_max_output_tokens": 2048,
                "max_agent_iterations": 8,
                "task_execution_retries": 3,
                "plan_validation_retries": 1,
                "review_retries": 1,
                "structured_output_retries": 1,
            },
        }
    )


@pytest.mark.acceptance
@pytest.mark.ollama
def test_real_crewai_ollama_init_and_resume_without_paid_provider(tmp_path: Path) -> None:
    _require_ollama()
    repository = _repository(tmp_path / "weather-ledger")
    config = _config(repository)

    first = MishkanInitializer().run(config, repository, "Initialize this library from evidence")
    resumed = MishkanInitializer().run(config, repository, "Initialize this library from evidence")

    assert first.resumed is False
    assert len(first.results) == len(first.reviews) == 1
    assert first.reviews[0].verdict == "accepted"
    assert first.results[0].cited_paths
    assert resumed.resumed is True
    assert resumed.plan_fingerprint == first.plan_fingerprint
    assert resumed.results == first.results
    assert (repository / ".mishkan" / "mishkan.db").is_file()
    assert {provider.kind for provider in config.providers.values()} == {"ollama"}
