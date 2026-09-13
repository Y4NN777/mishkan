import json
from types import SimpleNamespace

from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
from mishkan.organization.models import (
    OrganizationDefinition,
    OutcomeDefinition,
    RoleDefinition,
)
from mishkan.planning.models import AcceptedPlan, PlanTask, ReviewDecision
from mishkan.repository.models import DiscoverySnapshot, RepositoryBinding


def test_crewai_stamps_trusted_result_and_review_lineage(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
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
    task = PlanTask(
        task_id="produce-result",
        title="Produce result",
        purpose="Produce a result from accepted evidence",
        assigned_role="Producer",
        tools=("file.read",),
        evidence_paths=("README.md",),
    )
    discovery = DiscoverySnapshot(
        binding=RepositoryBinding(
            repository_id="repository-fixture",
            root=tmp_path,
            base_revision="a" * 40,
            working_tree_dirty=False,
            working_tree_fingerprint="0" * 64,
        ),
        facts=(),
        unknowns=(),
        fingerprint="b" * 64,
    )
    plan = AcceptedPlan(
        objective="Produce one evidence-grounded result",
        outcome_id="attributed-review",
        repository_revision="a" * 40,
        tasks=(task,),
        fingerprint="c" * 64,
        discovery_fingerprint="b" * 64,
    )
    model_synthesis = SimpleNamespace(
        summary="The repository contains a project overview.",
        cited_paths=("README.md",),
        findings=("The README contains the project overview.",),
    )
    synthesis_capture = {}

    def synthesize(**kwargs):  # type: ignore[no-untyped-def]
        synthesis_capture.update(kwargs)
        return model_synthesis

    monkeypatch.setattr(coordinator, "_kickoff_structured", synthesize)
    result = coordinator.execute_task(plan, discovery, task, '{"evidence":"artifact:task"}')

    assert result.schema_version == "1.1"
    assert result.repository_revision == "a" * 40
    assert result.execution_context is not None
    assert result.execution_context.context_id == "repository-fixture"
    assert result.task_id == "produce-result"
    assert set(synthesis_capture["output_model"].model_fields) == {
        "summary",
        "cited_paths",
        "findings",
    }

    model_review = ReviewDecision(
        task_id="produce-result",
        verdict="accepted",
        summary="The supplied evidence supports the result.",
        checked_citations=("README.md",),
    )
    captured = {}

    def kickoff(**kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return model_review

    monkeypatch.setattr(coordinator, "_kickoff_structured", kickoff)

    attributed = coordinator.review_task(
        task,
        result,
        json.dumps(
            [
                {
                    "output": {
                        "operation_evidence": {
                            "scope": ["README.md"],
                        }
                    }
                }
            ]
        ),
    )

    assert attributed.schema_version == "1.1"
    assert attributed.producer_identity == "Producer"
    assert attributed.evaluator_identity == "Independent_Evaluator"
    assert attributed.summary == model_review.summary
    assert set(captured["output_model"].model_fields) == {
        "verdict",
        "summary",
        "issues",
    }
    assert attributed.checked_citations == ("README.md",)
