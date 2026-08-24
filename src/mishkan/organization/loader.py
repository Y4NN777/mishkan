"""Load bundled or project-supplied versioned definitions."""

from importlib.resources import files
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from mishkan.domain.schema import SchemaRegistry
from mishkan.organization.models import OrganizationDefinition, OutcomeDefinition

DefinitionT = TypeVar("DefinitionT", bound=BaseModel)


def _load(source: Path | None, resource_name: str, model: type[DefinitionT]) -> DefinitionT:
    if source is None:
        resource = files("mishkan.resources.organization").joinpath(resource_name)
        document = yaml.safe_load(resource.read_text(encoding="utf-8"))
    else:
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
    return model.model_validate(document)


def load_initialization_definitions(
    organization_source: Path | None = None,
    outcome_source: Path | None = None,
) -> tuple[OrganizationDefinition, OutcomeDefinition]:
    organization = _load(organization_source, "i01-organization.yaml", OrganizationDefinition)
    outcome = _load(outcome_source, "mishkan-init.yaml", OutcomeDefinition)
    SchemaRegistry.require_supported("mishkan.organization", organization.schema_version)
    SchemaRegistry.require_supported("mishkan.outcome", outcome.schema_version)
    role_names = {role.name for role in organization.roles}
    referenced_roles = (
        set(outcome.allowed_roles) | set(outcome.task_roles) | set(outcome.review_roles)
    )
    missing = sorted(referenced_roles - role_names)
    if missing:
        raise ValueError(f"outcome references unknown roles: {missing}")
    return organization, outcome
