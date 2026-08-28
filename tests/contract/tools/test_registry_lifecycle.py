from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from sqlalchemy.orm import Session

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.persistence import SchemaManager
from mishkan.persistence.sqlite import create_local_engine
from mishkan.tools import ToolCatalog
from mishkan.tools.lifecycle import ToolRegistryLifecycle
from mishkan.tools.models import (
    RegistryEntryKind,
    RegistryLifecycleAction,
    RegistryMutation,
)

CATALOG_URI = "package://mishkan.resources.tools/core-catalog.yaml"
READ_ADAPTER = "native.repository.read_file"


def _dynamic_contract() -> dict[str, object]:
    source = files("mishkan.resources.tools.contracts").joinpath("read-file.yaml")
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return {
        **document,
        "tool_id": "project.dynamic_read",
        "crewai_name": "project_dynamic_read",
        "summary": "Read one project file through a dynamically governed registry entry.",
        "source_id": "operator.dynamic",
        "source_kind": "operator",
    }


def _mutate(
    database: Path,
    lifecycle: ToolRegistryLifecycle,
    mutation: RegistryMutation,
    revision: int,
) -> None:
    engine = create_local_engine(database)
    try:
        with Session(engine) as session, session.begin():
            lifecycle.mutate(session, mutation, revision=revision)
    finally:
        engine.dispose()


def test_lifecycle_changes_only_new_snapshots_and_never_fabricates_an_adapter(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    lifecycle = ToolRegistryLifecycle(database)
    original_catalog = ToolCatalog(
        (CATALOG_URI,), tmp_path, available_adapters=frozenset({READ_ADAPTER})
    )
    original_snapshot = original_catalog.snapshot(("repository.read_file",))

    _mutate(
        database,
        lifecycle,
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.ADD,
            definition=_dynamic_contract(),
        ),
        1,
    )
    projection = lifecycle.projection()
    unavailable_catalog = ToolCatalog((CATALOG_URI,), tmp_path, lifecycle=projection)
    with pytest.raises(MishkanError) as unavailable:
        unavailable_catalog.snapshot(("project.dynamic_read",))
    assert unavailable.value.envelope.code is ErrorCode.TOOL_UNAVAILABLE

    effective_catalog = ToolCatalog(
        (CATALOG_URI,),
        tmp_path,
        available_adapters=frozenset({READ_ADAPTER}),
        lifecycle=projection,
    )
    effective = effective_catalog.snapshot(("project.dynamic_read",))
    assert effective.require("project.dynamic_read").adapter == READ_ADAPTER

    _mutate(
        database,
        lifecycle,
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.DISABLE,
        ),
        2,
    )
    disabled_catalog = ToolCatalog(
        (CATALOG_URI,),
        tmp_path,
        available_adapters=frozenset({READ_ADAPTER}),
        lifecycle=lifecycle.projection(),
    )
    with pytest.raises(MishkanError) as disabled:
        disabled_catalog.snapshot(("project.dynamic_read",))
    assert disabled.value.envelope.code is ErrorCode.TOOL_CONTRACT
    assert original_snapshot.require("repository.read_file").tool_id == "repository.read_file"
    assert effective.require("project.dynamic_read").tool_id == "project.dynamic_read"


def test_lifecycle_rejects_invalid_definition_before_state_changes(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    lifecycle = ToolRegistryLifecycle(database)

    with pytest.raises(MishkanError) as caught:
        _mutate(
            database,
            lifecycle,
            RegistryMutation(
                entry_kind=RegistryEntryKind.TOOL,
                identity="project.wrong_identity",
                action=RegistryLifecycleAction.ADD,
                definition=_dynamic_contract(),
            ),
            1,
        )

    assert caught.value.envelope.code is ErrorCode.TOOL_CONTRACT
    assert lifecycle.entries() == ()


def test_complete_lifecycle_preserves_definition_and_uses_one_versioned_entry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    lifecycle = ToolRegistryLifecycle(database)
    definition = _dynamic_contract()
    actions = (
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.ADD,
            definition=definition,
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.SET_PRECEDENCE,
            precedence=42,
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.DISABLE,
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.ENABLE,
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.UPDATE,
            definition={**definition, "summary": "Updated dynamic read implementation."},
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.REMOVE,
        ),
        RegistryMutation(
            entry_kind=RegistryEntryKind.TOOL,
            identity="project.dynamic_read",
            action=RegistryLifecycleAction.ADD,
            definition=definition,
        ),
    )
    for revision, mutation in enumerate(actions, start=1):
        _mutate(database, lifecycle, mutation, revision)

    entries = lifecycle.entries()
    assert len(entries) == 1
    assert entries[0].revision == 7
    assert entries[0].precedence == 42
    assert entries[0].enabled is True
    assert entries[0].removed is False
    assert entries[0].definition is not None
    assert entries[0].definition["tool_id"] == definition["tool_id"]
    assert entries[0].definition["summary"] == definition["summary"]


def test_adapter_disable_is_an_authority_neutral_availability_override(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    lifecycle = ToolRegistryLifecycle(database)
    _mutate(
        database,
        lifecycle,
        RegistryMutation(
            entry_kind=RegistryEntryKind.ADAPTER,
            identity=READ_ADAPTER,
            action=RegistryLifecycleAction.DISABLE,
        ),
        1,
    )

    catalog = ToolCatalog(
        (CATALOG_URI,),
        tmp_path,
        available_adapters=frozenset({READ_ADAPTER}),
        lifecycle=lifecycle.projection(),
    )
    with pytest.raises(MishkanError) as caught:
        catalog.snapshot(("repository.read_file",))

    assert caught.value.envelope.code is ErrorCode.TOOL_UNAVAILABLE
    assert caught.value.envelope.details["missing_conditions"] == (f"adapter:{READ_ADAPTER}",)


def test_source_precedence_selects_new_metadata_without_rewriting_old_snapshot(
    tmp_path: Path,
) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    lifecycle = ToolRegistryLifecycle(database)
    base = tmp_path / "base.yaml"
    base.write_text(
        """schema_version: '1.0'
source_id: project.base
source_kind: project
revision: '1'
adoption_authority: Engineer
tools:
  - tool_id: project.shared
    version: 1.0.0
    summary: Metadata from the configured base source.
    effect_class: read
    source_id: project.base
    source_kind: project
    contract_uri: project:base-contract.yaml
""",
        encoding="utf-8",
    )
    overlay = {
        "schema_version": "1.0",
        "source_id": "operator.overlay",
        "source_kind": "operator",
        "revision": "1",
        "adoption_authority": "Operator",
        "tools": [
            {
                "tool_id": "project.shared",
                "version": "1.0.0",
                "summary": "Metadata from the higher precedence operator source.",
                "effect_class": "read",
                "source_id": "operator.overlay",
                "source_kind": "operator",
                "contract_uri": "project:overlay-contract.yaml",
            }
        ],
    }
    _mutate(
        database,
        lifecycle,
        RegistryMutation(
            entry_kind=RegistryEntryKind.SOURCE,
            identity="operator.overlay",
            action=RegistryLifecycleAction.ADD,
            definition=overlay,
        ),
        1,
    )
    _mutate(
        database,
        lifecycle,
        RegistryMutation(
            entry_kind=RegistryEntryKind.SOURCE,
            identity="operator.overlay",
            action=RegistryLifecycleAction.SET_PRECEDENCE,
            precedence=10,
        ),
        2,
    )

    catalog = ToolCatalog((str(base),), tmp_path, lifecycle=lifecycle.projection())

    assert (
        catalog.search("project.shared")[0].summary
        == "Metadata from the higher precedence operator source."
    )
