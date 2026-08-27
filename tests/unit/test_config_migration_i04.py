from pathlib import Path

import yaml

from mishkan.config.loader import ConfigLoader
from mishkan.config.migration import migrate_to_latest
from mishkan.config.presets import preset_text


def _write_previous_schema(path: Path, version: str) -> None:
    document = yaml.safe_load(preset_text("local"))
    document["schema_version"] = version
    for field in ("web", "browser", "mcp"):
        document.pop(field)
    if version == "1.1":
        for field in ("daemon", "persistence", "artifacts", "sessions"):
            document.pop(field)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_explicit_config_migration_adds_public_i04_surfaces(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    _write_previous_schema(source, "1.2")

    migrate_to_latest(source)
    config = ConfigLoader().load([source]).value

    assert config.schema_version == "1.3"
    assert config.web is not None
    assert config.browser is not None
    assert config.mcp is not None
    assert config.web.sources["searxng-local"].role.value == "broker"
    assert config.mcp.connections == {}
    assert config.mcp.task_poll_min_seconds == 0.1
    assert config.mcp.task_poll_max_seconds == 5.0


def test_latest_migration_can_cross_both_explicit_previous_versions(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    _write_previous_schema(source, "1.1")

    migrate_to_latest(source)
    config = ConfigLoader().load([source]).value

    assert config.schema_version == "1.3"
    assert config.daemon is not None
    assert config.web is not None
