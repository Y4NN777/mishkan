from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.planning.models import InitializationResult, PlanTask, ReviewDecision
from mishkan.planning.result_validator import ResultValidator
from mishkan.repository.models import DiscoverySnapshot, RepositoryBinding


def _discovery() -> DiscoverySnapshot:
    return DiscoverySnapshot(
        binding=RepositoryBinding(
            repository_id="a" * 64,
            root=Path("/repository"),
            base_revision="b" * 40,
        ),
        facts=(),
        unknowns=(),
        fingerprint="c" * 64,
    )


def _task() -> PlanTask:
    return PlanTask(
        task_id="inspect-readme",
        title="Inspect the README",
        purpose="Verify the project overview.",
        assigned_role="Repository_Investigator",
        tools=("repository.read_file",),
        evidence_paths=("README.md",),
    )


def _result() -> InitializationResult:
    return InitializationResult(
        repository_revision="b" * 40,
        task_id="inspect-readme",
        summary="The README identifies the project.",
        cited_paths=("README.md",),
        findings=("The project has an overview.",),
    )


def test_result_requires_separate_accepting_review() -> None:
    validator = ResultValidator()
    result = validator.verify(_result(), _task(), _discovery())
    review = ReviewDecision(
        task_id="inspect-readme",
        verdict="accepted",
        summary="The evidence supports the result.",
        checked_citations=("README.md",),
    )

    assert validator.accept_review(review, result) == review


def test_rejected_review_prevents_acceptance() -> None:
    review = ReviewDecision(
        task_id="inspect-readme",
        verdict="rejected",
        summary="The evidence does not support the claim.",
        checked_citations=("README.md",),
        issues=("Unsupported claim",),
    )

    with pytest.raises(MishkanError) as caught:
        ResultValidator().accept_review(review, _result())
    assert caught.value.envelope.code is ErrorCode.OUTPUT_CONTRACT
