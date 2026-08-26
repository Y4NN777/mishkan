from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from support.i02 import plan_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization import load_initialization_definitions
from mishkan.persistence import LocalRunRepository
from mishkan.planning import PlanCandidate, PlanTask
from mishkan.planning.models import AcceptedPlan, InitializationResult, ReviewDecision
from mishkan.repository import RepositoryInspector
from mishkan.repository.models import DiscoverySnapshot


def _discovery(tmp_path: Path):  # type: ignore[no-untyped-def]
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Fixture\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Fixture"], cwd=repository, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True
    )
    return RepositoryInspector().inspect(repository)


def _accepted_plan(discovery: DiscoverySnapshot) -> AcceptedPlan:
    organization, outcome = load_initialization_definitions()
    candidate = PlanCandidate(
        objective="Initialize this repository",
        outcome_id=outcome.outcome_id,
        repository_revision=discovery.binding.base_revision,
        tasks=(
            PlanTask(
                task_id="read-readme",
                title="Read project overview",
                purpose="Ground initialization in the project overview.",
                assigned_role="Repository_Investigator",
                tools=("repository.read_file",),
                evidence_paths=("README.md",),
            ),
        ),
    )
    return plan_validator(discovery.binding.root).accept(
        candidate, discovery, organization, outcome
    )


def _review(task_id: str = "read-readme") -> ReviewDecision:
    return ReviewDecision(
        task_id=task_id,
        verdict="accepted",
        summary="An independent reviewer verified the bound evidence.",
        checked_citations=("README.md",),
    )


def test_state_and_outbox_are_durable_and_resumable(tmp_path: Path) -> None:
    discovery = _discovery(tmp_path)
    database = tmp_path / ".mishkan" / "mishkan.db"
    repository = LocalRunRepository(database)
    started = repository.start_or_resume(discovery, "Initialize this repository", "mishkan.init")
    plan = _accepted_plan(discovery)
    repository.accept_plan(started.run_id, plan)
    result = InitializationResult(
        repository_revision=discovery.binding.base_revision,
        task_id="read-readme",
        summary="The fixture identifies itself through its README.",
        cited_paths=("README.md",),
        findings=("The repository contains a project overview.",),
    )
    repository.accept_result(started.run_id, result, _review())

    resumed = LocalRunRepository(database).start_or_resume(
        discovery, "Initialize this repository", "mishkan.init"
    )

    assert resumed.resumed is True
    assert resumed.plan == plan
    assert resumed.results == (result,)
    assert resumed.reviews == (_review(),)
    assert [event["event_type"] for event in repository.outbox_events()] == [
        "run.started",
        "plan.accepted",
        "task.result_accepted",
        "run.completed",
    ]
    engine = create_engine(f"sqlite:///{database}")
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"


def test_conflicting_duplicate_result_is_refused(tmp_path: Path) -> None:
    discovery = _discovery(tmp_path)
    repository = LocalRunRepository(tmp_path / "state.db")
    run = repository.start_or_resume(discovery, "Initialize this repository", "mishkan.init")
    repository.accept_plan(run.run_id, _accepted_plan(discovery))
    first = InitializationResult(
        repository_revision=discovery.binding.base_revision,
        task_id="read-readme",
        summary="First accepted summary.",
        cited_paths=("README.md",),
        findings=("First accepted finding.",),
    )
    repository.accept_result(run.run_id, first, _review())

    with pytest.raises(MishkanError) as caught:
        repository.accept_result(
            run.run_id,
            first.model_copy(update={"summary": "A conflicting summary."}),
            _review(),
        )
    assert caught.value.envelope.code is ErrorCode.DUPLICATE_RESULT
