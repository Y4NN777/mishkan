"""Repository initialization application service."""

from pathlib import Path

from mishkan.config.models import MishkanConfig
from mishkan.crewai.environment import configure_crewai_environment
from mishkan.organization import load_initialization_definitions
from mishkan.persistence import LocalRunRepository
from mishkan.planning.models import InitializationReport
from mishkan.repository import RepositoryInspector
from mishkan.tools import load_tool_registry


class MishkanInitializer:
    def run(
        self,
        config: MishkanConfig,
        repository_path: Path,
        objective: str,
    ) -> InitializationReport:
        configure_crewai_environment(config.crewai)
        from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
        from mishkan.crewai.flow import CrewAIInitializationFlow, InitializationFlowState

        discovery = RepositoryInspector().inspect(repository_path)
        organization, outcome = load_initialization_definitions()
        state_repository = LocalRunRepository(discovery.binding.root / ".mishkan" / "mishkan.db")
        snapshot = state_repository.start_or_resume(discovery, objective, outcome.outcome_id)
        state = InitializationFlowState(
            run_id=snapshot.run_id,
            objective=objective,
            discovery=discovery,
            resumed=snapshot.resumed,
            accepted_plan=snapshot.plan,
            accepted_results=list(snapshot.results),
            accepted_reviews=list(snapshot.reviews),
        )
        coordinator = CrewAIInitializationCoordinator(
            config,
            organization,
            outcome,
            load_tool_registry(),
        )
        flow = CrewAIInitializationFlow(
            state,
            coordinator,
            state_repository,
            organization,
            outcome,
            tracing=config.crewai.tracing,
        )
        output = flow.kickoff()
        if not isinstance(output, InitializationReport):
            return InitializationReport.model_validate(output)
        return output
