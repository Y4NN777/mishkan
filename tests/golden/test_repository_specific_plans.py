from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from crewai import Crew

from mishkan.config.loader import ConfigLoader
from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.organization import load_initialization_definitions
from mishkan.planning.models import PlanCandidate, PlanTask
from mishkan.repository import RepositoryInspector
from mishkan.tools import load_tool_registry


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
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
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
        description = crew.tasks[0].description
        is_python = "pyproject.toml" in description
        revision_marker = "Repository revision: "
        revision = description.split(revision_marker, 1)[1].splitlines()[0]
        candidate = PlanCandidate(
            objective="Initialize repository",
            outcome_id="mishkan.init",
            repository_revision=revision,
            tasks=(
                PlanTask(
                    task_id="inspect-python" if is_python else "inspect-go",
                    title="Inspect Python manifest" if is_python else "Inspect Go module",
                    purpose="Ground the plan in the detected project manifest.",
                    assigned_role="Repository_Investigator",
                    tools=("repository.read_file",),
                    evidence_paths=("pyproject.toml" if is_python else "go.mod",),
                ),
            ),
        )
        return SimpleNamespace(pydantic=candidate, raw=candidate.model_dump_json())

    monkeypatch.setattr(Crew, "kickoff", generated_kickoff)
    config = ConfigLoader().load([Path("tests/fixtures/config/local-valid.yaml")]).value
    organization, outcome = load_initialization_definitions()
    coordinator = CrewAIInitializationCoordinator(
        config,
        organization,
        outcome,
        load_tool_registry(),
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
