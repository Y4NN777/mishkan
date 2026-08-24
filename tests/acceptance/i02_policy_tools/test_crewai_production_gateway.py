from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crewai import Crew

from mishkan.application.initialize import MishkanInitializer
from mishkan.config.loader import ConfigLoader
from mishkan.crewai.flow import CrewAIInitializationFlow
from mishkan.persistence import LocalRunRepository
from mishkan.planning.models import InitializationResult, PlanCandidate, PlanTask, ReviewDecision
from mishkan.repository import RepositoryInspector
from mishkan.tools.crewai_gateway import GatewayCrewAITool


def _repository(root: Path) -> Path:
    root.mkdir()
    (root / "README.md").write_text("# Governed repository\n", encoding="utf-8")
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Fixture"),
        ("config", "user.email", "fixture@example.invalid"),
        ("add", "."),
        ("commit", "-m", "fixture"),
    ):
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)
    return root


def test_production_crewai_task_uses_accepted_gateway_binding_and_durable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path / "repository")

    def kickoff(crew: Crew, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        task = crew.tasks[0]
        output_model = task.output_pydantic
        value: PlanCandidate | InitializationResult | ReviewDecision
        if output_model is PlanCandidate:
            revision = task.description.split("Repository revision: ", 1)[1].splitlines()[0]
            value = PlanCandidate(
                objective="Initialize governed repository",
                outcome_id="mishkan.init",
                repository_revision=revision,
                tasks=(
                    PlanTask(
                        task_id="inspect-governed-readme",
                        title="Inspect governed repository overview",
                        purpose="Ground initialization in the accepted repository evidence.",
                        assigned_role="Repository_Investigator",
                        tools=("repository.read_file",),
                        evidence_paths=("README.md",),
                    ),
                ),
            )
        else:
            tools = crew.agents[0].tools
            assert tools is not None
            tool = tools[0]
            assert isinstance(tool, GatewayCrewAITool)
            evidence = json.loads(tool.run(path="README.md"))
            assert evidence["content"] == "# Governed repository\n"
            if output_model is InitializationResult:
                value = InitializationResult(
                    repository_revision=task.description.split("Repository revision: ", 1)[
                        1
                    ].splitlines()[0],
                    task_id="inspect-governed-readme",
                    summary="The repository overview was inspected through its governed binding.",
                    cited_paths=("README.md",),
                    findings=("The README identifies a governed repository.",),
                )
            else:
                assert output_model is ReviewDecision
                value = ReviewDecision(
                    task_id="inspect-governed-readme",
                    verdict="accepted",
                    summary="Independent evidence review passed through its own binding.",
                    checked_citations=("README.md",),
                )
        return SimpleNamespace(pydantic=value, raw=value.model_dump_json())

    monkeypatch.setattr(Crew, "kickoff", kickoff)
    monkeypatch.setattr(
        CrewAIInitializationFlow,
        "kickoff",
        lambda flow: flow.execute_plan(flow.establish_plan()),
    )
    config = ConfigLoader().load([Path("tests/fixtures/config/local-valid.yaml")]).value

    report = MishkanInitializer().run(
        config,
        repository,
        "Initialize governed repository",
    )

    state = LocalRunRepository(repository / ".mishkan" / "mishkan.db").start_or_resume(
        RepositoryInspector().inspect(repository),
        "Initialize governed repository",
        "mishkan.init",
    )
    assert report.completed_task_ids == ("inspect-governed-readme",)
    assert state.plan is not None and state.plan.schema_version == "1.1"
    assert state.plan.registry is not None
    assert len(state.plan.tool_bindings) == 2
    assert len(state.plan.authorizations) == 2
    events = LocalRunRepository(repository / ".mishkan" / "mishkan.db").outbox_events()
    capability_events = [
        event["event_type"] for event in events if event["event_type"].startswith("tool.call_")
    ]
    assert capability_events == [
        "tool.call_authorized",
        "tool.call_completed",
        "tool.call_authorized",
        "tool.call_completed",
    ]
