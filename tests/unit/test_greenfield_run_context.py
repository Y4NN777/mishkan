from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support.capabilities import plan_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization import load_initialization_definitions
from mishkan.persistence import LocalRunRepository, SchemaManager
from mishkan.planning import PlanCandidate, PlanExecutionContext, PlannedToolCall, PlanTask
from mishkan.planning.models import InitializationResult, ReviewDecision
from mishkan.repository import (
    ProspectiveWorkspaceInspector,
    RepositoryEstablishment,
    RepositoryInspector,
)


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _prospective_plan(workspace: Path, context: PlanExecutionContext):  # type: ignore[no-untyped-def]
    organization, outcome = load_initialization_definitions()
    candidate = PlanCandidate(
        schema_version="1.2",
        objective="Establish the greenfield service from workspace evidence",
        outcome_id=outcome.outcome_id,
        repository_revision=None,
        execution_context=context,
        tasks=(
            PlanTask(
                task_id="inspect-intent",
                title="Inspect greenfield intent",
                purpose="Ground the prospective plan in the workspace intent.",
                assigned_role="Repository_Investigator",
                tools=("repository.read_file",),
                tool_calls=(
                    PlannedToolCall(
                        call_id="read-greenfield-intent",
                        tool_id="repository.read_file",
                        arguments={"path": "README.md"},
                    ),
                ),
                evidence_paths=("README.md",),
            ),
        ),
    )
    return plan_validator(workspace).accept(candidate, _discovery(workspace), organization, outcome)


def _discovery(workspace: Path):  # type: ignore[no-untyped-def]
    return ProspectiveWorkspaceInspector().inspect(
        workspace,
        workspace_id="prospective:greenfield-service",
    )


def _review() -> ReviewDecision:
    return ReviewDecision(
        task_id="inspect-intent",
        verdict="accepted",
        summary="Independent review verified the greenfield evidence.",
        checked_citations=("README.md",),
    )


def test_prospective_run_has_no_invented_repository_and_validates_result_context(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "greenfield"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Intended service\n", encoding="utf-8")
    discovery = _discovery(workspace)
    context = PlanExecutionContext.from_binding(discovery.binding)
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    runs = LocalRunRepository(database)
    started = runs.start_or_resume(
        discovery,
        "Establish the greenfield service from workspace evidence",
        "mishkan.init",
    )
    accepted = _prospective_plan(workspace, context)
    planned = runs.accept_plan(started.run_id, accepted)

    assert planned.execution_context == context
    assert planned.execution_context.repository_id is None
    assert planned.execution_context.repository_revision is None

    runs.start_run(started.run_id)
    runs.claim_task(started.run_id, "inspect-intent")
    runs.mark_validating(started.run_id, "inspect-intent")
    wrong_context = context.model_copy(
        update={"revision": "f" * 64, "discovery_revision": "f" * 64}
    )
    wrong = InitializationResult(
        schema_version="1.1",
        repository_revision=None,
        execution_context=wrong_context,
        task_id="inspect-intent",
        summary="This result came from another discovery revision.",
        cited_paths=("README.md",),
        findings=("The workspace contains an intent document.",),
    )
    with pytest.raises(MishkanError) as caught:
        runs.accept_result(started.run_id, wrong, _review())
    assert caught.value.envelope.code is ErrorCode.REVISION_MISMATCH

    correct = wrong.model_copy(
        update={
            "execution_context": context,
            "summary": "The prospective workspace contains its service intent.",
        }
    )
    completed = runs.accept_result(started.run_id, correct, _review())
    assert completed.results == (correct,)


def test_repository_establishment_is_explicit_and_preserves_prospective_lineage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "greenfield"
    workspace.mkdir()
    (workspace / "README.md").write_text("# Intended service\n", encoding="utf-8")
    discovery = _discovery(workspace)
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    runs = LocalRunRepository(database)
    started = runs.start_or_resume(discovery, "Create the service", "greenfield")

    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.name", "Fixture")
    _git(workspace, "config", "user.email", "fixture@example.invalid")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "establish repository")
    repository = RepositoryInspector().bind(workspace)
    establishment = RepositoryEstablishment(
        run_id=started.run_id,
        prospective_workspace=discovery.binding,
        repository=repository,
        evidence_references=("artifact:repository-establishment",),
        established_by="PM",
    )

    established = runs.record_repository_establishment(establishment)
    replayed = runs.record_repository_establishment(establishment)

    assert established.execution_context.kind == "prospective_workspace"
    assert established.repository_establishment == establishment
    assert replayed.repository_establishment == establishment
    assert [
        event["event_type"]
        for event in runs.outbox_events()
        if event["event_type"] == "run.repository_established"
    ] == ["run.repository_established"]
