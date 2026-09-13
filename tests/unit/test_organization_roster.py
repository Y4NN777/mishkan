from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mishkan.organization import OrganizationRosterDefinition, load_canonical_organization

EXPECTED_IDENTITIES = {
    "PM",
    "CTO",
    "Product_Analyst",
    "UX_Researcher",
    "Product_Designer",
    "Accessibility_Specialist",
    "System_Architect",
    "Software_Architect",
    "Integration_Architect",
    "Software_EngineeringLead",
    "Web_Application_Engineer",
    "Android_Engineer",
    "Apple_Platform_Engineer",
    "Backend_Service_Engineer",
    "Desktop_TUI_Engineer",
    "Systems_Software_Engineer",
    "Integration_SDK_Engineer",
    "Data_Lead",
    "Database_Engineer",
    "Data_Engineer",
    "AI_Engineer",
    "Platform_Lead",
    "Platform_Engineer",
    "Delivery_Engineer",
    "Reliability_Engineer",
    "Security_Lead",
    "Product_Security_Engineer",
    "Platform_Security_Engineer",
    "SupplyChain_Security_Engineer",
    "Quality_Lead",
    "Product_Functional_Evaluator",
    "Software_Technical_Evaluator",
    "Mobile_Application_Evaluator",
    "Accessibility_Evaluator",
    "Performance_Resilience_Evaluator",
    "Data_AI_Evaluator",
    "Platform_Release_Evaluator",
    "Application_Security_Evaluator",
    "Platform_Infrastructure_Security_Evaluator",
    "Identity_Access_Security_Evaluator",
    "SupplyChain_Security_Evaluator",
    "Data_AI_Security_Evaluator",
    "Product_Delivery_Reporter",
    "Technical_Change_Reporter",
    "Incident_Operations_Reporter",
    "Evidence_Auditor",
    "Research_Clarificator",
    "Research_Formulator",
    "Research_Investigator",
    "Research_Synthesizer",
    "Research_Evaluator",
    "Research_Reporter",
    "Product_Documentation_Specialist",
    "Developer_Documentation_Specialist",
    "Architecture_Documentation_Specialist",
    "Operations_Documentation_Specialist",
    "Security_Documentation_Specialist",
    "Knowledge_Curator",
    "Skill_Curator",
}


def test_canonical_organization_is_the_exact_srs_roster() -> None:
    organization = load_canonical_organization()

    assert organization.organization_version == "1"
    assert {identity.identity_id for identity in organization.identities} == EXPECTED_IDENTITIES
    assert len(organization.identities) == 59
    assert len(organization.pools) == 5
    assert "Mission_Lead" not in EXPECTED_IDENTITIES
    assert all(not hasattr(identity, "allowed_tools") for identity in organization.identities)


def test_pool_membership_is_explicit_and_bidirectional() -> None:
    organization = load_canonical_organization()
    memberships = {
        pool.pool_id: {
            identity.identity_id
            for identity in organization.identities
            if pool.pool_id in identity.pool_memberships
        }
        for pool in organization.pools
    }

    assert memberships == {pool.pool_id: set(pool.members) for pool in organization.pools}


def test_version_one_source_cannot_replace_a_canonical_identity(tmp_path: Path) -> None:
    organization = load_canonical_organization()
    document = organization.model_dump(mode="json")
    document["identities"] = document["identities"][:-1]
    source = tmp_path / "organization.yaml"
    source.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(ValueError, match="exact canonical identity roster"):
        load_canonical_organization(source)


def test_pool_definition_cannot_disagree_with_identity_membership() -> None:
    document = load_canonical_organization().model_dump(mode="json")
    document["identities"][10]["pool_memberships"] = []

    with pytest.raises(ValidationError, match="pool membership mismatch"):
        OrganizationRosterDefinition.model_validate(document)
