from types import SimpleNamespace

from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.organization.models import (
    OrganizationDefinition,
    OutcomeDefinition,
    RoleDefinition,
)
from mishkan.planning.models import InitializationResult, PlanTask, ReviewDecision


def test_crewai_review_stamps_trusted_producer_and_evaluator_lineage(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    coordinator = CrewAIInitializationCoordinator.__new__(CrewAIInitializationCoordinator)
    coordinator._config = SimpleNamespace(  # type: ignore[assignment]
        crewai=SimpleNamespace(task_execution_retries=0)
    )
    coordinator._organization = OrganizationDefinition(
        schema_version="1.0",
        organization_id="review-attribution",
        roles=(
            RoleDefinition(
                name="Producer",
                goal="Produce bounded evidence",
                backstory="Produces work under an accepted plan",
                model_route="execution",
            ),
            RoleDefinition(
                name="Independent_Evaluator",
                goal="Evaluate evidence independently",
                backstory="Reviews another identity's accepted task result",
                model_route="review",
            ),
        ),
    )
    coordinator._outcome = OutcomeDefinition(
        schema_version="1.0",
        outcome_id="attributed-review",
        objective_class="test",
        intent="Prove trusted review attribution",
        allowed_roles=("Producer", "Independent_Evaluator"),
        task_roles=("Producer",),
        review_roles=("Independent_Evaluator",),
    )
    model_review = ReviewDecision(
        task_id="produce-result",
        verdict="accepted",
        summary="The supplied evidence supports the result.",
        checked_citations=("README.md",),
    )
    monkeypatch.setattr(coordinator, "_kickoff_structured", lambda **_kwargs: model_review)

    attributed = coordinator.review_task(
        PlanTask(
            task_id="produce-result",
            title="Produce result",
            purpose="Produce a result from accepted evidence",
            assigned_role="Producer",
            tools=("file.read",),
            evidence_paths=("README.md",),
        ),
        InitializationResult(
            repository_revision="a" * 40,
            task_id="produce-result",
            summary="The repository contains a project overview.",
            cited_paths=("README.md",),
            findings=("The README contains the project overview.",),
        ),
        '{"evidence":"artifact:review"}',
    )

    assert attributed.schema_version == "1.1"
    assert attributed.producer_identity == "Producer"
    assert attributed.evaluator_identity == "Independent_Evaluator"
    assert attributed.summary == model_review.summary
