"""Optional versioned mission guidance; never a workflow or authority grant."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.sources import resolve_source_path


class TemplateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MissionTemplateDefinition(TemplateModel):
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    version: str = Field(min_length=1, max_length=128)
    source: str = Field(min_length=1, max_length=2_048)
    provenance: tuple[str, ...] = Field(min_length=1)
    applicability_signals: tuple[str, ...] = Field(min_length=1)
    applicability_exclusions: tuple[str, ...] = ()
    objective_guidance: tuple[str, ...] = Field(min_length=1)
    constraint_guidance: tuple[str, ...] = ()
    risk_guidance: tuple[str, ...] = ()
    evidence_guidance: tuple[str, ...] = Field(min_length=1)
    completion_guidance: tuple[str, ...] = Field(min_length=1)
    compatible_organization_versions: tuple[str, ...] = Field(min_length=1)
    required_responsibility_classes: tuple[str, ...] = ()
    declared_configuration_conditions: tuple[str, ...] = ()
    declared_skill_conditions: tuple[str, ...] = ()
    declared_tool_conditions: tuple[str, ...] = ()
    declared_execution_conditions: tuple[str, ...] = ()
    validation_expectations: tuple[str, ...] = Field(min_length=1)
    reporting_expectations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def guidance_is_unique(self) -> MissionTemplateDefinition:
        for values in (
            self.applicability_signals,
            self.applicability_exclusions,
            self.required_responsibility_classes,
            self.declared_tool_conditions,
        ):
            if len(values) != len(set(values)):
                raise ValueError("mission template guidance values must be unique")
        return self


class MissionTemplateCatalogue(TemplateModel):
    schema_version: Literal["1.0"] = "1.0"
    catalogue_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,127}$")
    revision: str = Field(min_length=1, max_length=128)
    templates: tuple[MissionTemplateDefinition, ...] = ()

    @model_validator(mode="after")
    def identities_are_unique(self) -> MissionTemplateCatalogue:
        identities = [(item.template_id, item.version) for item in self.templates]
        if len(identities) != len(set(identities)):
            raise ValueError("mission template identities must be unique")
        return self


class MissionTemplateLoader:
    def load(self, sources: tuple[str, ...], project_root: Path) -> MissionTemplateCatalogue:
        templates: list[MissionTemplateDefinition] = []
        revisions: list[str] = []
        identities: set[tuple[str, str]] = set()
        for source in sources:
            catalogue = self._load(source, project_root)
            revisions.append(f"{catalogue.catalogue_id}@{catalogue.revision}")
            for template in catalogue.templates:
                identity = (template.template_id, template.version)
                if identity in identities:
                    raise MishkanError(
                        ErrorCode.CONFIGURATION,
                        "mission template identity is duplicated across configured sources",
                        details={"template_id": template.template_id, "version": template.version},
                    )
                identities.add(identity)
                templates.append(template)
        return MissionTemplateCatalogue(
            catalogue_id="configured-mission-templates",
            revision="+".join(revisions) if revisions else "empty",
            templates=tuple(templates),
        )

    @staticmethod
    def _load(source: str, project_root: Path) -> MissionTemplateCatalogue:
        try:
            if source.startswith("package://"):
                location = source.removeprefix("package://")
                package, separator, resource = location.rpartition("/")
                if not separator:
                    raise ValueError("package mission-template source is invalid")
                text = files(package).joinpath(resource).read_text(encoding="utf-8")
            else:
                path = resolve_source_path(source, project_root, "mission template catalogue")
                text = path.read_text(encoding="utf-8")
            return MissionTemplateCatalogue.model_validate(yaml.safe_load(text))
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "mission template catalogue cannot be loaded",
                details={"source": source},
            ) from exc


class MissionTemplateService:
    def __init__(self, catalogue: MissionTemplateCatalogue) -> None:
        self._catalogue = catalogue

    @property
    def catalogue(self) -> MissionTemplateCatalogue:
        return self._catalogue

    def applicable(
        self,
        signals: tuple[str, ...],
        *,
        organization_version: str,
    ) -> tuple[MissionTemplateDefinition, ...]:
        observed = set(signals)
        return tuple(
            template
            for template in self._catalogue.templates
            if organization_version in template.compatible_organization_versions
            and set(template.applicability_signals).issubset(observed)
            and not set(template.applicability_exclusions).intersection(observed)
        )
