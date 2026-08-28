from __future__ import annotations

import shutil
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
from mishkan.planning.models import (
    InitializationResult,
    PlanCandidate,
    PlannedToolCall,
    PlanTask,
    ReviewDecision,
)
from mishkan.repository import RepositoryInspector


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


def _project_repository(root: Path, manifest: str, content: str) -> Path:
    root.mkdir()
    (root / "README.md").write_text(f"# {root.name}\n", encoding="utf-8")
    (root / manifest).write_text(content, encoding="utf-8")
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
    git_path = shutil.which("git")
    assert git_path is not None
    executable = str(Path(git_path).resolve())
    process_arguments = {
        "mode": "process",
        "executable": executable,
        "args": ["show", "HEAD:README.md"],
        "cwd": ".",
        "environment": {},
        "credential_environment": {},
        "stdin": None,
        "timeout_seconds": 10,
        "expected_exit_codes": [0],
        "declared_effects": [],
        "output_policy": {
            "preview_bytes": 4096,
            "preserve_full_output_as_artifact": False,
        },
    }
    execution_attempts = 0
    review_attempts = 0

    def kickoff(crew: Crew, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        nonlocal execution_attempts, review_attempts
        assert crew.agents[0].max_retry_limit == 0
        task = crew.tasks[0]
        output_model = task.output_pydantic
        value: PlanCandidate | InitializationResult | ReviewDecision
        if issubclass(output_model, PlanCandidate):
            assert output_model.model_json_schema()["properties"]["tasks"]["maxItems"] == 1
            assert "Create at most 1 task(s)" in task.description
            assert "assigned_role must be Repository_Investigator" in task.description
            assert '"core.process.exec"' in task.description
            assert executable in task.description
            assert "public policy permits none unattended" not in task.description
            revision = task.description.split("Repository revision: ", 1)[1].splitlines()[0]
            value = PlanCandidate(
                schema_version="1.1",
                objective="Initialize governed repository",
                outcome_id="mishkan.init",
                repository_revision=revision,
                tasks=(
                    PlanTask(
                        task_id="inspect-governed-readme",
                        title="Inspect governed repository overview",
                        purpose="Ground initialization in the accepted repository evidence.",
                        assigned_role="Repository_Investigator",
                        tools=("repository.read_file", "core.process.exec"),
                        tool_calls=(
                            PlannedToolCall(
                                call_id="read-governed-readme",
                                tool_id="repository.read_file",
                                arguments={"path": "README.md"},
                            ),
                            PlannedToolCall(
                                call_id="probe-governed-project",
                                tool_id="core.process.exec",
                                arguments=process_arguments,
                            ),
                        ),
                        evidence_paths=("README.md",),
                    ),
                ),
            )
        else:
            tools = crew.agents[0].tools
            assert tools == []
            assert "# Governed repository\\n" in task.description
            if output_model is InitializationResult:
                assert (
                    "MISHKAN has already executed every accepted call exactly once"
                    in task.description
                )
                execution_attempts += 1
                if execution_attempts > 1:
                    assert "A previous independent review rejected" in task.description
                    assert "State the repository heading exactly" in task.description
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
                review_attempts += 1
                assert "MISHKAN independently executed" in task.description
                value = ReviewDecision(
                    task_id="inspect-governed-readme",
                    verdict="rejected" if review_attempts == 1 else "accepted",
                    summary=(
                        "The first result needs a more exact evidence statement."
                        if review_attempts == 1
                        else "Independent evidence review passed through its own binding."
                    ),
                    checked_citations=("README.md",),
                    issues=(
                        ("State the repository heading exactly from README.md.",)
                        if review_attempts == 1
                        else ()
                    ),
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
    assert execution_attempts == 2
    assert review_attempts == 2
    assert state.plan is not None and state.plan.schema_version == "1.1"
    assert state.plan.registry is not None
    assert len(state.plan.tool_bindings) == 3
    assert len(state.plan.authorizations) == 3
    events = LocalRunRepository(repository / ".mishkan" / "mishkan.db").outbox_events()
    capability_events = [
        event["event_type"] for event in events if event["event_type"].startswith("tool.call_")
    ]
    assert capability_events == [
        "tool.call_authorized",
        "tool.call_started",
        "tool.call_completed",
        "tool.call_authorized",
        "tool.call_started",
        "tool.call_completed",
        "tool.call_authorized",
        "tool.call_started",
        "tool.call_completed",
    ]


@pytest.mark.commands
def test_production_path_accepts_different_exact_native_commands_for_different_repositories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_repository = _project_repository(
        tmp_path / "python-project",
        "pyproject.toml",
        "[project]\nname='python-project'\n",
    )
    go_repository = _project_repository(
        tmp_path / "go-project",
        "go.mod",
        "module example.invalid/go-project\n",
    )
    git_path = shutil.which("git")
    bash_path = shutil.which("bash")
    assert git_path is not None
    assert bash_path is not None
    executable = str(Path(git_path).resolve())
    bash_executable = str(Path(bash_path).resolve())

    def kickoff(crew: Crew, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
        task = crew.tasks[0]
        output_model = task.output_pydantic
        is_python = "pyproject.toml" in task.description
        manifest = "pyproject.toml" if is_python else "go.mod"
        task_id = "inspect-python-manifest" if is_python else "inspect-go-module"
        execution_tool = "core.process.exec" if is_python else "core.shell.run"
        execution_arguments = (
            {
                "mode": "process",
                "executable": executable,
                "args": ["show", f"HEAD:{manifest}"],
                "cwd": ".",
                "environment": {},
                "credential_environment": {},
                "stdin": None,
                "timeout_seconds": 10,
                "expected_exit_codes": [0],
                "declared_effects": [],
                "output_policy": {
                    "preview_bytes": 4096,
                    "preserve_full_output_as_artifact": False,
                },
            }
            if is_python
            else {
                "mode": "shell",
                "shell_profile": {
                    "schema_version": "1.0",
                    "profile_id": "acceptance.fixture-bash",
                    "revision": "1",
                    "dialect": "bash",
                    "interpreter": bash_executable,
                    "startup_files": [],
                    "options": {
                        "pipefail": True,
                        "errexit": True,
                        "nounset": True,
                        "inherit_errexit": False,
                    },
                },
                "script": f"{executable} show HEAD:{manifest}",
                "cwd": ".",
                "environment": {},
                "credential_environment": {},
                "stdin": None,
                "timeout_seconds": 10,
                "expected_exit_codes": [0],
                "declared_paths": [],
                "declared_executables": [executable],
                "network_destinations": [],
                "declared_effects": [],
                "output_policy": {
                    "preview_bytes": 4096,
                    "preserve_full_output_as_artifact": False,
                },
            }
        )
        value: PlanCandidate | InitializationResult | ReviewDecision
        if issubclass(output_model, PlanCandidate):
            assert output_model.model_json_schema()["properties"]["tasks"]["maxItems"] == 1
            revision = task.description.split("Repository revision: ", 1)[1].splitlines()[0]
            value = PlanCandidate(
                schema_version="1.1",
                objective="Initialize project from native evidence",
                outcome_id="mishkan.init",
                repository_revision=revision,
                tasks=(
                    PlanTask(
                        task_id=task_id,
                        title=(
                            "Inspect Python project manifest"
                            if is_python
                            else "Inspect Go module manifest"
                        ),
                        purpose="Combine bounded file evidence with a repository-specific command.",
                        assigned_role="Repository_Investigator",
                        tools=("repository.read_file", execution_tool),
                        tool_calls=(
                            PlannedToolCall(
                                call_id=f"read-{manifest.replace('.', '-')}",
                                tool_id="repository.read_file",
                                arguments={"path": manifest},
                            ),
                            PlannedToolCall(
                                call_id=f"probe-{manifest.replace('.', '-')}",
                                tool_id=execution_tool,
                                arguments=execution_arguments,
                            ),
                        ),
                        evidence_paths=(manifest,),
                    ),
                ),
            )
        else:
            tools = crew.agents[0].tools
            assert tools == []
            assert f'"path": "{manifest}"' in task.description
            if output_model is InitializationResult:
                assert (
                    "MISHKAN has already executed every accepted call exactly once"
                    in task.description
                )
                assert manifest in task.description
                revision = task.description.split("Repository revision: ", 1)[1].splitlines()[0]
                value = InitializationResult(
                    repository_revision=revision,
                    task_id=task_id,
                    summary=f"Inspected {manifest} through exact file and process calls.",
                    cited_paths=(manifest,),
                    findings=(f"The repository declares {manifest}.",),
                )
            else:
                assert output_model is ReviewDecision
                assert "MISHKAN independently executed" in task.description
                value = ReviewDecision(
                    task_id=task_id,
                    verdict="accepted",
                    summary=f"The independent read supports the {manifest} finding.",
                    checked_citations=(manifest,),
                )
        return SimpleNamespace(pydantic=value, raw=value.model_dump_json())

    monkeypatch.setattr(Crew, "kickoff", kickoff)
    monkeypatch.setattr(
        CrewAIInitializationFlow,
        "kickoff",
        lambda flow: flow.execute_plan(flow.establish_plan()),
    )
    config = ConfigLoader().load([Path("tests/fixtures/config/local-valid.yaml")]).value
    shell_policy = Path("tests/fixtures/policies/safe-shell-probe.yaml").resolve()
    config = config.model_copy(
        update={"policy_sources": (*config.policy_sources, str(shell_policy))}
    )

    reports = tuple(
        MishkanInitializer().run(
            config,
            repository,
            "Initialize project from native evidence",
        )
        for repository in (python_repository, go_repository)
    )
    plans = tuple(
        LocalRunRepository(repository / ".mishkan" / "mishkan.db")
        .start_or_resume(
            RepositoryInspector().inspect(repository),
            "Initialize project from native evidence",
            "mishkan.init",
        )
        .plan
        for repository in (python_repository, go_repository)
    )

    assert all(plan is not None for plan in plans)
    assert reports[0].completed_task_ids != reports[1].completed_task_ids
    assert plans[0] is not None and plans[1] is not None
    python_call = plans[0].tasks[0].tool_calls[1]
    go_call = plans[1].tasks[0].tool_calls[1]
    assert python_call.tool_id == "core.process.exec"
    assert python_call.arguments["args"] == ["show", "HEAD:pyproject.toml"]
    assert go_call.tool_id == "core.shell.run"
    assert go_call.arguments["script"] == f"{executable} show HEAD:go.mod"
    assert python_call.argument_fingerprint != go_call.argument_fingerprint
