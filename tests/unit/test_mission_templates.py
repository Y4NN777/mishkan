from pathlib import Path

import pytest
from pydantic import ValidationError

from mishkan.missions import (
    MissionTemplateDefinition,
    MissionTemplateLoader,
    MissionTemplateService,
)


def test_bundled_templates_are_optional_guidance_without_crews_or_task_graphs(
    tmp_path: Path,
) -> None:
    catalogue = MissionTemplateLoader().load(
        ("package://mishkan.resources.organization/mission-templates.yaml",), tmp_path
    )
    service = MissionTemplateService(catalogue)

    selected = service.applicable(("incident", "linux"), organization_version="1")
    assert [item.template_id for item in selected] == ["incident-response"]
    assert service.applicable(("unmatched",), organization_version="1") == ()
    documents = [item.model_dump(mode="json") for item in catalogue.templates]
    assert all("crew" not in item and "tasks" not in item for item in documents)


def test_bundled_guidance_covers_every_required_mission_class_without_becoming_a_catalogue(
    tmp_path: Path,
) -> None:
    catalogue = MissionTemplateLoader().load(
        ("package://mishkan.resources.organization/mission-templates.yaml",), tmp_path
    )
    service = MissionTemplateService(catalogue)
    expected = {
        "greenfield": "greenfield",
        "existing-repository": "existing-system-change",
        "multi-repository": "multi-repository-change",
        "product": "product-delivery",
        "research": "research",
        "incident": "incident-response",
        "modernization": "system-modernization",
        "platform": "platform-capability",
        "operations": "operational-change",
    }

    assert {
        signal: tuple(
            item.template_id for item in service.applicable((signal,), organization_version="1")
        )
        for signal in expected
    } == {signal: (template_id,) for signal, template_id in expected.items()}
    assert service.applicable(("unclassified-free-form-objective",), organization_version="1") == ()


def test_template_schema_refuses_static_crew_or_task_graph_fields() -> None:
    base = {
        "template_id": "invalid-static-workflow",
        "version": "1",
        "source": "test",
        "provenance": ["test:evidence"],
        "applicability_signals": ["test"],
        "objective_guidance": ["Test the optional guidance contract."],
        "evidence_guidance": ["Require attributable evidence."],
        "completion_guidance": ["Validate the result independently."],
        "compatible_organization_versions": ["1"],
        "validation_expectations": ["Independent validation."],
        "reporting_expectations": ["Report evidence and residual risk."],
        "fixed_crew": ["Backend_Service_Engineer"],
        "tasks": ["implement"],
    }
    with pytest.raises(ValidationError):
        MissionTemplateDefinition.model_validate(base)


def test_no_configured_template_source_is_a_valid_free_form_catalogue(tmp_path: Path) -> None:
    catalogue = MissionTemplateLoader().load((), tmp_path)
    assert catalogue.templates == ()
    assert (
        MissionTemplateService(catalogue).applicable(("greenfield",), organization_version="1")
        == ()
    )
