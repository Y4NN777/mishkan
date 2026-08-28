from pathlib import Path

from mishkan.config.loader import ConfigLoader
from mishkan.config.migration import migrate_to_1_2


def test_explicit_config_migration_adds_public_durability_surfaces(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    fixture = Path("tests/fixtures/config/local-valid.yaml").read_text(encoding="utf-8")
    source.write_text(fixture, encoding="utf-8")

    migrate_to_1_2(source)
    config = ConfigLoader().load([source]).value

    assert config.schema_version == "1.2"
    assert config.daemon is not None and config.daemon.host == "127.0.0.1"
    assert config.persistence is not None
    assert config.artifacts is not None
    assert config.sessions is not None
