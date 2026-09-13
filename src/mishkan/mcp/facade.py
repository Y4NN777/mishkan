"""Inbound harness facade over the same daemon command and query authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mishkan.application import ApplicationCommand, CommandResult
from mishkan.config.models import SUPPORTED_MCP_FACADE_OPERATIONS, McpConfig
from mishkan.context import ContextualRecommendationService
from mishkan.conversations import SQLiteConversationRepository
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.events import EventPage
from mishkan.missions import (
    MissionEnvironmentReadinessService,
    MissionTemplateService,
    SQLiteMissionRepository,
)
from mishkan.notifications import NotificationDelivery, NotificationService, NotificationSeverity
from mishkan.organization import (
    OrganizationRosterDefinition,
    ProfessionalEvidenceKind,
)
from mishkan.organization.evolution_repository import SQLiteProfessionalEvolutionRepository
from mishkan.persistence import SQLiteApplicationRepository

CommandExecutor = Callable[[ApplicationCommand, str], Awaitable[CommandResult]]
DependencyT = TypeVar("DependencyT")


class McpFacadePort(Protocol):
    operations: tuple[str, ...]
    resources: tuple[str, ...]

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]: ...

    async def read_resource(self, uri: str, *, principal_id: str) -> dict[str, Any]: ...


class FacadeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EventQuery(FacadeModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000)
    event_types: tuple[str, ...] = ()
    entity_type: str | None = None
    entity_id: str | None = None


class RunQuery(FacadeModel):
    run_id: str = Field(min_length=1)


class LimitQuery(FacadeModel):
    limit: int = Field(default=100, ge=1, le=1_000)


class ProfessionalCompetenceQuery(FacadeModel):
    identity_id: str = Field(min_length=1, max_length=128)
    kind: ProfessionalEvidenceKind
    subject: str = Field(min_length=1, max_length=512)


class MissionQuery(FacadeModel):
    mission_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=100, ge=1, le=1_000)


class MissionTemplateQuery(FacadeModel):
    signals: tuple[str, ...] = ()
    organization_version: str = Field(default="1", min_length=1, max_length=64)


class ConversationListQuery(FacadeModel):
    mission_id: str | None = Field(default=None, min_length=1, max_length=128)
    limit: int = Field(default=100, ge=1, le=1_000)


class ConversationQuery(FacadeModel):
    conversation_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=100, ge=1, le=1_000)


class NotificationQuery(FacadeModel):
    after: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1_000)
    severities: tuple[NotificationSeverity, ...] = ()
    deliveries: tuple[NotificationDelivery, ...] = ()


class McpFacadeRouter:
    """Expose only allowlisted operations that have an executable daemon handler."""

    _QUERY_OPERATIONS = SUPPORTED_MCP_FACADE_OPERATIONS - {"command.submit"}

    def __init__(
        self,
        config: McpConfig,
        repository: SQLiteApplicationRepository,
        command_executor: CommandExecutor,
        *,
        schema_revision: str,
        event_page_limit: int,
        organization: OrganizationRosterDefinition | None = None,
        missions: SQLiteMissionRepository | None = None,
        conversations: SQLiteConversationRepository | None = None,
        professional_evolution: SQLiteProfessionalEvolutionRepository | None = None,
        mission_templates: MissionTemplateService | None = None,
        advisory: ContextualRecommendationService | None = None,
        readiness: MissionEnvironmentReadinessService | None = None,
        notifications: NotificationService | None = None,
    ) -> None:
        profile = config.exposure_profiles[config.facade.exposure_profile]
        self.operations = profile.operations
        self.resources = profile.resources
        self._repository = repository
        self._execute = command_executor
        self._schema_revision = schema_revision
        self._event_page_limit = event_page_limit
        self._organization = organization
        self._missions = missions
        self._conversations = conversations
        self._professional_evolution = professional_evolution
        self._mission_templates = mission_templates
        self._advisory = advisory
        self._readiness = readiness
        self._notifications = notifications

    async def invoke(
        self,
        operation: str,
        arguments: dict[str, Any],
        *,
        principal_id: str,
    ) -> dict[str, Any]:
        if operation not in self.operations:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness operation is outside the active MCP exposure profile",
            )
        if operation == "system.health":
            self._require_empty(arguments)
            return {"status": "ready", "schema": self._schema_revision}
        if operation == "system.snapshot":
            self._require_empty(arguments)
            return self._repository.snapshot(limit=self._event_page_limit).model_dump(mode="json")
        if operation == "events.list":
            query = self._validate(EventQuery, arguments)
            return self._events(query).model_dump(mode="json")
        if operation == "run.get":
            query = self._validate(RunQuery, arguments)
            runs = self._repository.runs(offset=0, limit=1_000)
            found = next((item for item in runs if item.get("id") == query.run_id), None)
            if found is None:
                raise MishkanError(ErrorCode.MISSION, "requested run does not exist")
            return dict(found)
        if operation == "organization.get":
            self._require_empty(arguments)
            organization = self._require_dependency(self._organization, "organization")
            return organization.model_dump(mode="json")
        if operation == "organization.competence.get":
            query = self._validate(ProfessionalCompetenceQuery, arguments)
            evolution = self._require_dependency(
                self._professional_evolution, "professional evolution"
            )
            return evolution.competence_state(
                query.identity_id,
                kind=query.kind,
                subject=query.subject,
            ).model_dump(mode="json")
        if operation == "mission.list":
            query = self._validate(LimitQuery, arguments)
            missions = self._require_dependency(self._missions, "missions")
            return {
                "missions": [
                    item.model_dump(mode="json")
                    for item in missions.list_missions(limit=query.limit)
                ]
            }
        if operation == "mission.get":
            query = self._validate(MissionQuery, arguments)
            missions = self._require_dependency(self._missions, "missions")
            return missions.mission(query.mission_id).model_dump(mode="json")
        if operation == "mission.inspect":
            query = self._validate(MissionQuery, arguments)
            return self._mission_inspection(query)
        if operation == "mission.templates.list":
            query = self._validate(MissionTemplateQuery, arguments)
            templates = self._require_dependency(self._mission_templates, "mission templates")
            selected = (
                templates.catalogue.templates
                if not query.signals
                else templates.applicable(
                    query.signals,
                    organization_version=query.organization_version,
                )
            )
            return {"templates": [item.model_dump(mode="json") for item in selected]}
        if operation == "conversation.list":
            query = self._validate(ConversationListQuery, arguments)
            conversations = self._require_dependency(self._conversations, "conversations")
            return {
                "conversations": [
                    item.model_dump(mode="json")
                    for item in conversations.channels(
                        mission_id=query.mission_id,
                        limit=query.limit,
                    )
                ]
            }
        if operation == "conversation.get":
            query = self._validate(ConversationQuery, arguments)
            conversations = self._require_dependency(self._conversations, "conversations")
            return {
                "conversation": conversations.channel(query.conversation_id).model_dump(
                    mode="json"
                ),
                "messages": [
                    item.model_dump(mode="json")
                    for item in conversations.messages(
                        query.conversation_id,
                        limit=query.limit,
                    )
                ],
            }
        if operation == "advisory.candidates.list":
            self._require_empty(arguments)
            advisory = self._require_dependency(self._advisory, "advisory")
            candidates = advisory.candidates()
            return {
                "candidates": [item.model_dump(mode="json") for item in candidates],
                "count": len(candidates),
                "activation_authorized": False,
            }
        if operation == "notification.list":
            query = self._validate(NotificationQuery, arguments)
            notifications = self._require_dependency(self._notifications, "notifications")
            events = self._repository.events(
                after_cursor=query.after,
                limit=query.limit,
            )
            return notifications.project(
                events,
                severities=frozenset(query.severities),
                deliveries=frozenset(query.deliveries),
            ).model_dump(mode="json")
        command = self._validate(ApplicationCommand, arguments)
        if command.actor_id != principal_id:
            raise MishkanError(
                ErrorCode.AUTHORITY_NOT_GRANTED,
                "harness command actor differs from its authenticated principal",
            )
        return (await self._execute(command, principal_id)).model_dump(mode="json")

    async def read_resource(self, uri: str, *, principal_id: str) -> dict[str, Any]:
        if uri not in self.resources:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "MCP resource is not exposed")
        if uri == "mishkan://snapshot":
            return self._repository.snapshot(limit=self._event_page_limit).model_dump(mode="json")
        if uri == "mishkan://runs":
            return {"runs": list(self._repository.runs(offset=0, limit=self._event_page_limit))}
        if uri == "mishkan://events":
            return self._events(EventQuery(limit=self._event_page_limit)).model_dump(mode="json")
        operation_by_uri = {
            "mishkan://organization": "organization.get",
            "mishkan://missions": "mission.list",
            "mishkan://conversations": "conversation.list",
            "mishkan://advisory/candidates": "advisory.candidates.list",
            "mishkan://notifications": "notification.list",
        }
        operation = operation_by_uri[uri]
        arguments = (
            {"limit": self._event_page_limit}
            if operation in {"mission.list", "conversation.list", "notification.list"}
            else {}
        )
        return await self.invoke(operation, arguments, principal_id=principal_id)

    def _mission_inspection(self, query: MissionQuery) -> dict[str, Any]:
        missions = self._require_dependency(self._missions, "missions")
        conversations = self._require_dependency(self._conversations, "conversations")
        mission = missions.mission(query.mission_id)
        brief = (
            missions.brief(query.mission_id).model_dump(mode="json")
            if mission.current_brief_version is not None
            else None
        )
        crew = (
            missions.crew(query.mission_id).model_dump(mode="json")
            if mission.current_crew_version is not None
            else None
        )
        environment_plan = (
            missions.environment_plan(query.mission_id).model_dump(mode="json")
            if mission.current_environment_plan_version is not None
            else None
        )
        readiness = self._require_dependency(self._readiness, "mission readiness")
        return {
            "mission": mission.model_dump(mode="json"),
            "brief": brief,
            "crew": crew,
            "environment_plan": environment_plan,
            "readiness": readiness.inspect(query.mission_id).model_dump(mode="json"),
            "assignments": [
                item.model_dump(mode="json")
                for item in missions.assignments(query.mission_id, limit=query.limit)
            ],
            "transitions": [
                item.model_dump(mode="json")
                for item in missions.transitions(query.mission_id, limit=query.limit)
            ],
            "conversations": [
                item.model_dump(mode="json")
                for item in conversations.channels(
                    mission_id=query.mission_id,
                    limit=query.limit,
                )
            ],
            "decisions": [
                item.model_dump(mode="json")
                for item in conversations.decisions(query.mission_id, limit=query.limit)
            ],
            "escalations": [
                item.model_dump(mode="json")
                for item in conversations.escalations(query.mission_id, limit=query.limit)
            ],
            "interventions": [
                item.model_dump(mode="json")
                for item in conversations.interventions(query.mission_id, limit=query.limit)
            ],
            "events": [
                item.model_dump(mode="json")
                for item in self._repository.events(
                    after_cursor=0,
                    limit=query.limit,
                    entity_type="mission",
                    entity_id=query.mission_id,
                ).events
            ],
        }

    def _events(self, query: EventQuery) -> EventPage:
        return self._repository.events(
            after_cursor=query.after,
            limit=query.limit,
            event_types=query.event_types,
            entity_type=query.entity_type,
            entity_id=query.entity_id,
        )

    @staticmethod
    def _require_empty(arguments: dict[str, Any]) -> None:
        if arguments:
            raise MishkanError(ErrorCode.OUTPUT_CONTRACT, "operation accepts no arguments")

    @staticmethod
    def _require_dependency(value: DependencyT | None, name: str) -> DependencyT:
        if value is None:
            raise MishkanError(
                ErrorCode.REQUIRED_DEPENDENCY,
                f"MCP facade {name} query is not configured",
            )
        return value

    @staticmethod
    def _validate(model: type[BaseModel], arguments: dict[str, Any]) -> Any:
        try:
            return model.model_validate(arguments)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.OUTPUT_CONTRACT,
                "harness operation arguments are invalid",
            ) from exc
