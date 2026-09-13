from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crewai import Crew

from mishkan.config.loader import ConfigLoader
from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.crewai.environment import configure_crewai_environment
from mishkan.organization import load_initialization_definitions
from mishkan.repository import RepositoryInspector


def _make_repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir()
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)
    return root


def test_same_outcome_generates_different_graphs_from_repository_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_repo = _make_repository(
        tmp_path / "python-project",
        {
            "README.md": "Python service",
            "pyproject.toml": "[project]\nname='service'",
            "app.py": "",
        },
    )
    go_repo = _make_repository(
        tmp_path / "go-project",
        {"README.md": "Go service", "go.mod": "module example/service", "main.go": "package main"},
    )

    def generated_kickoff(crew: Crew, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        assert crew.tasks[0].output_pydantic is not None
        assert set(crew.tasks[0].output_pydantic.model_fields) == {"tasks"}
        description = crew.tasks[0].description
        is_python = "pyproject.toml" in description
        evidence_path = "pyproject.toml" if is_python else "go.mod"
        proposal = {
            "tasks": [
                {
                    "task_id": "inspect-python" if is_python else "inspect-go",
                    "title": "Inspect Python manifest" if is_python else "Inspect Go module",
                    "purpose": "Ground the plan in the detected project manifest.",
                    "assigned_role": "Repository_Investigator",
                    "tool_calls": [
                        {
                            "call_id": "read-project-manifest",
                            "tool_id": "repository.read_file",
                            "arguments": {"path": evidence_path},
                        }
                    ],
                    "evidence_paths": [evidence_path],
                    "depends_on": [],
                }
            ]
        }
        return SimpleNamespace(pydantic=None, raw=json.dumps(proposal))

    monkeypatch.setattr(Crew, "kickoff", generated_kickoff)
    config = ConfigLoader().load([Path("tests/fixtures/config/local-valid.yaml")]).value
    configure_crewai_environment(config.crewai, tmp_path / "crewai-runtime")
    organization, outcome = load_initialization_definitions()
    coordinator = CrewAIInitializationCoordinator(
        config,
        organization,
        outcome,
    )

    python_plan = coordinator.propose_plan(
        RepositoryInspector().inspect(python_repo), "Initialize repository"
    )
    go_plan = coordinator.propose_plan(
        RepositoryInspector().inspect(go_repo), "Initialize repository"
    )

    assert python_plan.outcome_id == go_plan.outcome_id == "mishkan.init"
    assert [task.task_id for task in python_plan.tasks] == ["inspect-python"]
    assert [task.task_id for task in go_plan.tasks] == ["inspect-go"]
    assert python_plan.tasks != go_plan.tasks
    assert python_plan.schema_version == go_plan.schema_version == "1.1"
    assert python_plan.objective == go_plan.objective == "Initialize repository"
    assert python_plan.outcome_id == go_plan.outcome_id == "mishkan.init"
    assert python_plan.execution_context is go_plan.execution_context is None
    assert python_plan.tasks[0].tools == go_plan.tasks[0].tools == ("repository.read_file",)
