from pathlib import Path

import pytest
from pydantic import ValidationError

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
            working_tree_dirty=False,
            working_tree_fingerprint="0" * 64,
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


def test_attributed_review_requires_distinct_producer_and_evaluator() -> None:
    with pytest.raises(ValidationError, match="cannot evaluate"):
        ReviewDecision(
            schema_version="1.1",
            task_id="inspect-readme",
            producer_identity="Repository_Investigator",
            evaluator_identity="Repository_Investigator",
            verdict="accepted",
            summary="The same identity attempted to accept its own result.",
            checked_citations=("README.md",),
        )

    with pytest.raises(ValidationError, match="requires producer and evaluator"):
        ReviewDecision(
            schema_version="1.1",
            task_id="inspect-readme",
            verdict="accepted",
            summary="The review omitted its trusted identity lineage.",
            checked_citations=("README.md",),
        )
