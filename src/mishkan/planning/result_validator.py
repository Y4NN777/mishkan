"""Deterministic acceptance checks around CrewAI task results."""

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.planning.models import InitializationResult, PlanTask, ReviewDecision
from mishkan.repository.models import DiscoverySnapshot


class ResultValidator:
    def verify(
        self,
        result: InitializationResult,
        task: PlanTask,
        discovery: DiscoverySnapshot,
    ) -> InitializationResult:
        violations = []
        if result.repository_revision != discovery.binding.base_revision:
            violations.append("repository revision differs from the accepted plan")
        if result.task_id != task.task_id:
            violations.append("task identifier differs from the accepted task")
        invalid_citations = sorted(set(result.cited_paths) - set(task.evidence_paths))
        if invalid_citations:
            violations.append(f"result cites unbound paths: {invalid_citations}")
        if violations:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "CrewAI task result was refused",
                details={"violations": violations},
            )
        return result

    def accept_review(
        self,
        review: ReviewDecision,
        result: InitializationResult,
    ) -> ReviewDecision:
        violations = []
        if review.task_id != result.task_id:
            violations.append("review task identifier differs from the result")
        unchecked = sorted(set(result.cited_paths) - set(review.checked_citations))
        if unchecked:
            violations.append(f"review did not check result citations: {unchecked}")
        if review.verdict != "accepted":
            violations.append(f"independent review rejected the result: {list(review.issues)}")
        if violations:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "independent CrewAI review did not authorize result acceptance",
                details={"violations": violations},
            )
        return review
