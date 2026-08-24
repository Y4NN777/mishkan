"""Minimal I01 organization and outcome definitions."""

from pydantic import BaseModel, ConfigDict, Field


class DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleDefinition(DefinitionModel):
    name: str = Field(min_length=1)
    goal: str = Field(min_length=3)
    backstory: str = Field(min_length=3)
    model_route: str = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()


class OrganizationDefinition(DefinitionModel):
    schema_version: str
    organization_id: str = Field(min_length=1)
    roles: tuple[RoleDefinition, ...] = Field(min_length=1)


class OutcomeDefinition(DefinitionModel):
    schema_version: str
    outcome_id: str = Field(min_length=1)
    intent: str = Field(min_length=3)
    allowed_roles: tuple[str, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()
    max_tasks: int = Field(default=6, ge=1, le=12)
