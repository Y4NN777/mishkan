from pathlib import Path

import pytest

from mishkan.config.editor import set_value
from mishkan.config.loader import ConfigLoader
from mishkan.config.presets import PRESET_NAMES, write_preset
from mishkan.config.probe import probe_connections
from mishkan.domain.errors import ErrorCode, MishkanError


def test_every_public_preset_is_valid(tmp_path: Path) -> None:
    for preset in PRESET_NAMES:
        target = write_preset(preset, tmp_path / f"{preset}.yaml")
        effective = ConfigLoader().load([target])
        assert effective.value.mode == preset


def test_preset_does_not_overwrite_without_explicit_permission(tmp_path: Path) -> None:
    target = write_preset("local", tmp_path / "config.yaml")
    with pytest.raises(MishkanError) as caught:
        write_preset("cloud", target)
    assert caught.value.envelope.code is ErrorCode.CONFIGURATION


def test_set_value_is_atomic_when_new_value_is_invalid(tmp_path: Path) -> None:
    target = write_preset("local", tmp_path / "config.yaml")
    before = target.read_bytes()
    with pytest.raises(MishkanError):
        set_value(target, "timezone", "Mars/Olympus_Mons")
    assert target.read_bytes() == before


def test_set_value_updates_and_validates_complete_configuration(tmp_path: Path) -> None:
    target = write_preset("local", tmp_path / "config.yaml")
    set_value(target, "timezone", "Africa/Ouagadougou")
    assert ConfigLoader().load([target]).value.timezone == "Africa/Ouagadougou"


def test_connection_failures_are_warning_only(tmp_path: Path) -> None:
    target = write_preset("local", tmp_path / "config.yaml")
    config = ConfigLoader().load([target]).value

    def unavailable(_endpoint: str, _timeout: float) -> None:
        raise ConnectionError("offline")

    results = probe_connections(config, probe=unavailable)
    assert results
    assert all(not result.reachable for result in results)
    assert all(result.warning for result in results)
