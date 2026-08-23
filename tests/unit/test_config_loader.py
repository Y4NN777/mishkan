from pathlib import Path

import pytest

from mishkan.config.loader import ConfigLoader
from mishkan.domain.errors import ErrorCode, MishkanError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "config"


def test_loads_a_complete_explicit_configuration() -> None:
    effective = ConfigLoader().load([FIXTURES / "local-valid.yaml"])
    assert effective.value.mode == "local"
    assert effective.value.model_routes["planning"].candidates[0].provider == "local-models"
    assert len(effective.fingerprint) == 64
    assert effective.layers[0].source.is_absolute()


def test_later_layer_wins_and_records_field_source(tmp_path: Path) -> None:
    base = tmp_path / "base.yaml"
    base.write_text((FIXTURES / "local-valid.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        'schema_version: "1.0"\ntimezone: Africa/Ouagadougou\n',
        encoding="utf-8",
    )

    effective = ConfigLoader().load([base, overlay])

    assert effective.value.timezone == "Africa/Ouagadougou"
    assert effective.field_sources["timezone"] == str(overlay.resolve())
    assert effective.field_sources["mode"] == str(base.resolve())
    assert [layer.precedence for layer in effective.layers] == [0, 1]


def test_missing_config_is_a_stable_configuration_error() -> None:
    with pytest.raises(MishkanError) as caught:
        ConfigLoader().load([])
    assert caught.value.envelope.code is ErrorCode.CONFIGURATION


def test_unknown_provider_reference_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "invalid.yaml"
    source.write_text(
        (FIXTURES / "local-valid.yaml")
        .read_text(encoding="utf-8")
        .replace("provider: local-models", "provider: absent-provider"),
        encoding="utf-8",
    )
    with pytest.raises(MishkanError) as caught:
        ConfigLoader().load([source])
    assert caught.value.envelope.code is ErrorCode.CONFIGURATION
    assert "unknown providers" in str(caught.value.envelope.details)


def test_validation_error_does_not_echo_secret_input(tmp_path: Path) -> None:
    canary = "sk-super-secret-canary"
    source = tmp_path / "secret.yaml"
    source.write_text(
        (FIXTURES / "local-valid.yaml")
        .read_text(encoding="utf-8")
        .replace("kind: ollama", f"kind: ollama\n    api_key: {canary}"),
        encoding="utf-8",
    )
    with pytest.raises(MishkanError) as caught:
        ConfigLoader().load([source])
    serialized = caught.value.envelope.model_dump_json()
    assert caught.value.envelope.code is ErrorCode.CONFIGURATION
    assert canary not in serialized


def test_unsupported_schema_version_is_not_a_config_error() -> None:
    with pytest.raises(MishkanError) as caught:
        ConfigLoader().load([FIXTURES / "unsupported-schema.yaml"])
    assert caught.value.envelope.code is ErrorCode.VERSION
