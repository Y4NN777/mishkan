"""Versioned organization, professional-profile, and initialization definitions."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DefinitionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleDefinition(DefinitionModel):
    name: str = Field(min_length=1)
    goal: str = Field(min_length=3)
    backstory: str = Field(min_length=3)
    model_route: str = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()


class OrganizationDefinition(DefinitionModel):
    """Legacy bounded role set used only by the accepted initialization flow."""

    schema_version: str
    organization_id: str = Field(min_length=1)
    roles: tuple[RoleDefinition, ...] = Field(min_length=1)


class OutcomeDefinition(DefinitionModel):
    schema_version: str
    outcome_id: str = Field(min_length=1)
    objective_class: str = Field(min_length=1)
    intent: str = Field(min_length=3)
    allowed_roles: tuple[str, ...] = Field(min_length=1)
    task_roles: tuple[str, ...] = Field(min_length=1)
    review_roles: tuple[str, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...] = ()
    max_tasks: int = Field(default=6, ge=1, le=12)


class IndependenceClass(StrEnum):
    EXECUTIVE = "executive"
    PRODUCTION = "production"
    INDEPENDENT_ASSURANCE = "independent_assurance"
    RESEARCH = "research"
    DOCUMENTATION = "documentation"


class BranchDefinition(DefinitionModel):
    branch_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    name: str = Field(min_length=3, max_length=160)
    executive_sponsor: str | None = Field(default=None, min_length=2, max_length=128)
    independent: bool


class PoolDefinition(DefinitionModel):
    pool_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    name: str = Field(min_length=3, max_length=160)
    members: tuple[str, ...] = Field(min_length=1)


class ProfessionalIdentityDefinition(DefinitionModel):
    identity_id: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{1,127}$")
    profile_version: Literal["1.0"] = "1.0"
    branch_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    responsibility: str = Field(min_length=3, max_length=500)
    independence_class: IndependenceClass
    authority_limits: tuple[str, ...] = Field(min_length=1)
    pool_memberships: tuple[str, ...] = ()
    mission_lead_eligible: bool = True


class OrganizationRosterDefinition(DefinitionModel):
    """Persistent professional identities; never a static mission crew or tool matrix."""

    schema_version: Literal["1.0"] = "1.0"
    organization_id: str = Field(pattern=r"^[a-z][a-z0-9-]{2,127}$")
    organization_version: Literal["1"] = "1"
    branches: tuple[BranchDefinition, ...] = Field(min_length=1)
    pools: tuple[PoolDefinition, ...] = Field(min_length=1)
    identities: tuple[ProfessionalIdentityDefinition, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def references_are_complete_and_unambiguous(self) -> "OrganizationRosterDefinition":
        branch_ids = [branch.branch_id for branch in self.branches]
        pool_ids = [pool.pool_id for pool in self.pools]
        identity_ids = [identity.identity_id for identity in self.identities]
        for label, values in (
            ("branch", branch_ids),
            ("pool", pool_ids),
            ("identity", identity_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} identifiers")
        known_identities = set(identity_ids)
        known_branches = set(branch_ids)
        known_pools = set(pool_ids)
        if "Mission_Lead" in known_identities:
            raise ValueError("Mission_Lead is a temporary responsibility, not an identity")
        for branch in self.branches:
            if (
                branch.executive_sponsor is not None
                and branch.executive_sponsor not in known_identities
            ):
                raise ValueError(f"unknown executive sponsor: {branch.executive_sponsor}")
        declared_memberships: dict[str, set[str]] = {pool_id: set() for pool_id in pool_ids}
        for identity in self.identities:
            if identity.branch_id not in known_branches:
                raise ValueError(f"unknown identity branch: {identity.branch_id}")
            unknown_pools = set(identity.pool_memberships) - known_pools
            if unknown_pools:
                raise ValueError(f"unknown identity pools: {sorted(unknown_pools)}")
            if len(identity.pool_memberships) != len(set(identity.pool_memberships)):
                raise ValueError(f"duplicate pool membership for {identity.identity_id}")
            for pool_id in identity.pool_memberships:
                declared_memberships[pool_id].add(identity.identity_id)
        for pool in self.pools:
            if len(pool.members) != len(set(pool.members)):
                raise ValueError(f"duplicate members in pool: {pool.pool_id}")
            unknown_members = set(pool.members) - known_identities
            if unknown_members:
                raise ValueError(f"unknown pool members: {sorted(unknown_members)}")
            if set(pool.members) != declared_memberships[pool.pool_id]:
                raise ValueError(f"pool membership mismatch: {pool.pool_id}")
        return self
