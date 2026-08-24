from __future__ import annotations

from pathlib import Path

import pytest

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.catalog import ToolCatalog
from mishkan.tools.models import AvailabilityState

CATALOG_URI = "package://mishkan.resources.tools/i02-catalog.yaml"


def test_level_zero_search_does_not_load_full_tool_schema(tmp_path: Path) -> None:
    index = tmp_path / "catalog.yaml"
    index.write_text(
        """schema_version: '1.0'
source_id: project
source_kind: project
revision: '1'
adoption_authority: Engineer
tools:
  - tool_id: project.missing_contract
    version: 1.0.0
    summary: Searchable metadata without eager schema loading.
    effect_class: read
    source_id: project
    source_kind: project
    contract_uri: project:does-not-exist.yaml
""",
        encoding="utf-8",
    )

    catalog = ToolCatalog((str(index),), tmp_path)

    assert catalog.search("searchable")[0].tool_id == "project.missing_contract"
    with pytest.raises(MishkanError) as caught:
        catalog.snapshot(("project.missing_contract",))
    assert caught.value.envelope.code is ErrorCode.TOOL_CONTRACT


def test_nested_toolsets_resolve_to_exact_immutable_snapshot_and_binding(tmp_path: Path) -> None:
    catalog = ToolCatalog((CATALOG_URI,), tmp_path)
    snapshot = catalog.snapshot(("engineering.standard",))
    binding = catalog.bind(
        snapshot,
        task_id="delivery-task",
        role="Engineer",
        tool_id="git.push",
        allowed_targets=("origin", "develop"),
    )

    assert tuple(tool.tool_id for tool in snapshot.tools) == (
        "repository.write_file",
        "git.commit",
        "repository.read_file",
        "git.push",
        "deployment.apply",
        "release.publish",
    )
    assert "migration.apply" not in {tool.tool_id for tool in snapshot.tools}
    assert binding.registry_fingerprint == snapshot.fingerprint
    assert binding.contract_fingerprint == snapshot.require("git.push").provenance_fingerprint


def test_availability_is_visible_and_not_an_authorization_decision(tmp_path: Path) -> None:
    catalog = ToolCatalog((CATALOG_URI,), tmp_path, runtime="python")
    command = next(tool for tool in catalog.list_metadata() if tool.tool_id == "command.run")

    availability = catalog.availability(command)

    assert availability.state is AvailabilityState.UNAVAILABLE
    assert availability.missing_conditions == ("runtime:python",)
    with pytest.raises(MishkanError) as caught:
        catalog.snapshot(("command.run",))
    assert caught.value.envelope.code is ErrorCode.TOOL_UNAVAILABLE


def test_identity_collision_blocks_registry_snapshot(tmp_path: Path) -> None:
    template = """schema_version: '1.0'
source_id: {source}
source_kind: project
revision: '1'
adoption_authority: Engineer
tools:
  - tool_id: project.same
    version: 1.0.0
    summary: A colliding tool identity.
    effect_class: read
    source_id: {source}
    source_kind: project
    contract_uri: project:unused.yaml
"""
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(template.format(source="first"), encoding="utf-8")
    second.write_text(template.format(source="second"), encoding="utf-8")

    with pytest.raises(MishkanError) as caught:
        ToolCatalog((str(first), str(second)), tmp_path)

    assert caught.value.envelope.code is ErrorCode.TOOL_DRIFT
    assert caught.value.envelope.details["collisions"] == {"project.same": ["first", "second"]}


def test_toolset_cycle_is_rejected_with_public_nesting_context(tmp_path: Path) -> None:
    index = tmp_path / "cycle.yaml"
    index.write_text(
        """schema_version: '1.0'
source_id: project
source_kind: project
revision: '1'
adoption_authority: Engineer
toolsets:
  - toolset_id: project.a
    version: 1.0.0
    summary: First cyclic set.
    toolsets: [project.b]
  - toolset_id: project.b
    version: 1.0.0
    summary: Second cyclic set.
    toolsets: [project.a]
""",
        encoding="utf-8",
    )

    with pytest.raises(MishkanError) as caught:
        ToolCatalog((str(index),), tmp_path).snapshot(("project.a",))

    assert caught.value.envelope.code is ErrorCode.TOOL_CONTRACT
    assert "cycle" in caught.value.envelope.message


def test_metadata_contract_drift_blocks_binding(tmp_path: Path) -> None:
    index = tmp_path / "catalog.yaml"
    contract = tmp_path / "contract.yaml"
    index.write_text(
        """schema_version: '1.0'
source_id: project
source_kind: project
revision: '1'
adoption_authority: Engineer
tools:
  - tool_id: project.read
    version: 1.0.0
    summary: Metadata version one.
    effect_class: read
    source_id: project
    source_kind: project
    contract_uri: project:contract.yaml
""",
        encoding="utf-8",
    )
    contract.write_text(
        """schema_version: '1.0'
tool_id: project.read
version: 2.0.0
crewai_name: project_read
summary: Contract version two.
effect_class: read
source_id: project
source_kind: project
adapter: project.read
input_schema: {type: object}
result_schema: {type: object}
timeout_behavior: cancel_local
idempotency: idempotent
target_scopes: [path]
""",
        encoding="utf-8",
    )

    with pytest.raises(MishkanError) as caught:
        ToolCatalog((str(index),), tmp_path).snapshot(("project.read",))

    assert caught.value.envelope.code is ErrorCode.TOOL_DRIFT
