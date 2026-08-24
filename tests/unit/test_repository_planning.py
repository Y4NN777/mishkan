from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from support.i02 import plan_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization import load_initialization_definitions
from mishkan.planning import PlanCandidate, PlanTask
from mishkan.repository import RepositoryInspector


def _git(cwd: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "sample"
    repository.mkdir()
    (repository / "README.md").write_text("# Sample\n", encoding="utf-8")
    (repository / "pyproject.toml").write_text("[project]\nname='sample'\n", encoding="utf-8")
    (repository / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Fixture")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    return repository


def _candidate(revision: str, *, dependency: tuple[str, ...] = ()) -> PlanCandidate:
    return PlanCandidate(
        objective="Understand this repository",
        outcome_id="mishkan.init",
        repository_revision=revision,
        tasks=(
            PlanTask(
                task_id="inspect-python",
                title="Inspect Python structure",
                purpose="Establish the Python project structure from cited evidence.",
                assigned_role="Repository_Investigator",
                tools=("repository.read_file",),
                evidence_paths=("pyproject.toml",),
                depends_on=dependency,
            ),
        ),
    )


def test_inspector_binds_revision_and_cites_configured_evidence(tmp_path: Path) -> None:
    discovery = RepositoryInspector().inspect(_repository(tmp_path))

    assert len(discovery.binding.base_revision) == 40
    assert {fact.value for fact in discovery.facts} >= {"pyproject.toml", "Python"}
    assert Path("pyproject.toml") in discovery.cited_paths
    assert discovery.unknowns == ("test framework",)


def test_inspector_refuses_a_non_repository(tmp_path: Path) -> None:
    with pytest.raises(MishkanError) as caught:
        RepositoryInspector().inspect(tmp_path)
    assert caught.value.envelope.code is ErrorCode.PROJECT


def test_plan_acceptance_binds_discovery_and_authority(tmp_path: Path) -> None:
    discovery = RepositoryInspector().inspect(_repository(tmp_path))
    organization, outcome = load_initialization_definitions()

    accepted = plan_validator(discovery.binding.root).accept(
        _candidate(discovery.binding.base_revision),
        discovery,
        organization,
        outcome,
    )

    assert accepted.discovery_fingerprint == discovery.fingerprint
    assert len(accepted.fingerprint) == 64


@pytest.mark.parametrize(
    ("candidate_change", "violation"),
    [
        ({"repository_revision": "different-revision"}, "revision"),
        ({"outcome_id": "another.outcome"}, "outcome"),
    ],
)
def test_plan_acceptance_refuses_lineage_changes(
    tmp_path: Path,
    candidate_change: dict[str, str],
    violation: str,
) -> None:
    discovery = RepositoryInspector().inspect(_repository(tmp_path))
    organization, outcome = load_initialization_definitions()
    candidate = _candidate(discovery.binding.base_revision).model_copy(update=candidate_change)

    with pytest.raises(MishkanError, match="refused") as caught:
        plan_validator(discovery.binding.root).accept(candidate, discovery, organization, outcome)
    assert violation in str(caught.value.envelope.details["violations"])


def test_plan_acceptance_refuses_dependency_cycles(tmp_path: Path) -> None:
    discovery = RepositoryInspector().inspect(_repository(tmp_path))
    organization, outcome = load_initialization_definitions()

    with pytest.raises(MishkanError) as caught:
        plan_validator(discovery.binding.root).accept(
            _candidate(discovery.binding.base_revision, dependency=("inspect-python",)),
            discovery,
            organization,
            outcome,
        )
    assert "cycle" in str(caught.value.envelope.details["violations"])
