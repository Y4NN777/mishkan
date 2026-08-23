"""Layered YAML loader with effective-value provenance."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mishkan.config.models import MishkanConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry

JsonObject = dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConfigLayer:
    source: Path
    precedence: int
    sha256: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "source": str(self.source),
            "precedence": self.precedence,
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class EffectiveConfig:
    value: MishkanConfig
    layers: tuple[ConfigLayer, ...]
    field_sources: Mapping[str, str]
    fingerprint: str

    def public_view(self) -> dict[str, Any]:
        return {
            "configuration": self.value.model_dump(mode="json"),
            "provenance": {
                "layers": [layer.as_dict() for layer in self.layers],
                "field_sources": dict(sorted(self.field_sources.items())),
                "fingerprint": self.fingerprint,
            },
        }


class ConfigLoader:
    """Load explicit low-to-high-precedence YAML layers."""

    def load(self, sources: Sequence[Path]) -> EffectiveConfig:
        if not sources:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "no configuration source was provided",
                details={"field": "config"},
            )

        merged: JsonObject = {}
        reports: list[ConfigLayer] = []
        field_sources: dict[str, str] = {}
        expected_version: str | None = None

        for precedence, source in enumerate(sources):
            resolved = source.expanduser().resolve()
            raw_bytes = self._read(resolved)
            document = self._parse(resolved, raw_bytes)
            version = SchemaRegistry.require_supported(
                "mishkan.config", document.get("schema_version")
            )
            if expected_version is not None and version != expected_version:
                raise MishkanError(
                    ErrorCode.VERSION,
                    "configuration layers use different schema versions",
                    details={
                        "source": str(resolved),
                        "received": version,
                        "expected": expected_version,
                        "automatic_migration": False,
                    },
                )
            expected_version = version
            self._deep_merge(merged, document)
            for field in self._leaf_paths(document):
                field_sources[field] = str(resolved)
            reports.append(
                ConfigLayer(
                    source=resolved,
                    precedence=precedence,
                    sha256=hashlib.sha256(raw_bytes).hexdigest(),
                )
            )

        try:
            config = MishkanConfig.model_validate(merged)
        except ValidationError as exc:
            violations = [
                {
                    "field": ".".join(str(part) for part in error["loc"]),
                    "type": error["type"],
                    "message": error["msg"],
                }
                for error in exc.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )
            ]
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "effective configuration is invalid",
                details={"violations": violations},
            ) from exc

        canonical = json.dumps(
            config.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return EffectiveConfig(
            value=config,
            layers=tuple(reports),
            field_sources=field_sources,
            fingerprint=hashlib.sha256(canonical.encode()).hexdigest(),
        )

    @staticmethod
    def _read(source: Path) -> bytes:
        try:
            return source.read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "configuration source cannot be read",
                details={"source": str(source), "reason": type(exc).__name__},
            ) from exc

    @staticmethod
    def _parse(source: Path, raw_bytes: bytes) -> JsonObject:
        try:
            parsed = yaml.safe_load(raw_bytes)
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "configuration source is malformed YAML",
                details={"source": str(source)},
            ) from exc
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "configuration source must contain a string-keyed mapping",
                details={"source": str(source)},
            )
        return parsed

    @classmethod
    def _deep_merge(cls, target: MutableMapping[str, Any], overlay: Mapping[str, Any]) -> None:
        for key, value in overlay.items():
            current = target.get(key)
            if isinstance(current, MutableMapping) and isinstance(value, Mapping):
                cls._deep_merge(current, value)
            else:
                target[key] = value

    @classmethod
    def _leaf_paths(cls, value: object, prefix: str = "") -> set[str]:
        if isinstance(value, Mapping):
            leaves: set[str] = set()
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                child_leaves = cls._leaf_paths(child, path)
                leaves.update(child_leaves or {path})
            return leaves
        return {prefix} if prefix else set()
