"""Repository initialization application service."""

from pathlib import Path

from mishkan.config.models import MishkanConfig
from mishkan.crewai.environment import configure_crewai_environment
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.organization import load_initialization_definitions
from mishkan.persistence import LocalRunRepository
from mishkan.planning import PlanValidator
from mishkan.planning.models import InitializationReport
from mishkan.policy import PolicyAuthority, PolicyLoader
from mishkan.repository import RepositoryInspector
from mishkan.tools.adapters import ReadFileAdapter
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver
from mishkan.tools.inspection import ContentInspector, InspectionProfileLoader
from mishkan.tools.isolation import IsolationProfileLoader


class MishkanInitializer:
    def run(
        self,
        config: MishkanConfig,
        repository_path: Path,
        objective: str,
    ) -> InitializationReport:
        if config.schema_version != "1.1":
            raise MishkanError(
                ErrorCode.VERSION,
                "governed initialization requires configuration schema 1.1",
                details={"received": config.schema_version, "automatic_migration": False},
            )
        discovery = RepositoryInspector().inspect(repository_path)
        configure_crewai_environment(
            config.crewai,
            discovery.binding.root / ".mishkan" / "crewai-runtime",
        )
        from mishkan.crewai.coordinator import CrewAIInitializationCoordinator
        from mishkan.crewai.flow import CrewAIInitializationFlow, InitializationFlowState

        organization, outcome = load_initialization_definitions()
        state_repository = LocalRunRepository(discovery.binding.root / ".mishkan" / "mishkan.db")
        catalog = ToolCatalog(
            config.tool_sources,
            discovery.binding.root,
            available_adapters=frozenset({ReadFileAdapter.adapter_id}),
        )
        policy = PolicyLoader().load(config.policy_sources, discovery.binding.root)
        inspection_source = config.inspection_profile
        if inspection_source is None:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "governed initialization requires an inspection profile",
            )
        inspector = ContentInspector(
            InspectionProfileLoader().load(inspection_source, discovery.binding.root)
        )
        isolation_loader = IsolationProfileLoader()
        isolation_profiles = tuple(
            isolation_loader.load(source, discovery.binding.root)
            for source in config.isolation_profiles
        )
        profile_ids = [profile.profile_id for profile in isolation_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "configured isolation profile identities must be unique",
            )
        authority = PolicyAuthority()
        init_registry = catalog.snapshot(outcome.allowed_tools)
        read_contract = init_registry.require("repository.read_file")
        gateway = CapabilityGateway(
            discovery.binding.root,
            authority,
            MappingCredentialResolver({}),
            inspector,
            {ReadFileAdapter.adapter_id: ReadFileAdapter(read_contract.max_bytes)},
            state_repository,
        )
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
            gateway,
            policy,
        )
        flow = CrewAIInitializationFlow(
            state,
            coordinator,
            state_repository,
            organization,
            outcome,
            PlanValidator(catalog, policy, authority),
            tracing=config.crewai.tracing,
        )
        output = flow.kickoff()
        if not isinstance(output, InitializationReport):
            return InitializationReport.model_validate(output)
        return output
