"""Load inspectable policy sources into an immutable effective snapshot."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.schema import SchemaRegistry
from mishkan.policy.models import EffectivePolicy, PolicyDocument


class PolicyLoader:
    def load(self, source_uris: tuple[str, ...], project_root: Path) -> EffectivePolicy:
        if not source_uris:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "at least one policy source is required",
                details={"field": "policy_sources"},
            )
        documents = tuple(self._load_one(uri, project_root) for uri in source_uris)
        claims: dict[str, list[str]] = {}
        for uri, document in zip(source_uris, documents, strict=True):
            for rule in document.rules:
                claims.setdefault(rule.rule_id, []).append(uri)
        collisions = {rule_id: uris for rule_id, uris in claims.items() if len(uris) > 1}
        if collisions:
            raise MishkanError(
                ErrorCode.POLICY_CONFLICT,
                "policy rule identifiers collide across configured sources",
                details={"collisions": collisions},
            )
        payload = {
            "schema_version": "1.0",
            "sources": [
                {"uri": uri, "fingerprint": document.fingerprint}
                for uri, document in zip(source_uris, documents, strict=True)
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return EffectivePolicy(
            documents=documents,
            source_uris=source_uris,
            fingerprint=fingerprint,
        )

    def _load_one(self, uri: str, project_root: Path) -> PolicyDocument:
        raw = self._read(uri, project_root)
        try:
            document: Any = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "policy source is malformed YAML",
                details={"source": uri},
            ) from exc
        if not isinstance(document, dict):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "policy source must contain a mapping",
                details={"source": uri},
            )
        SchemaRegistry.require_supported("mishkan.policy", document.get("schema_version"))
        try:
            return PolicyDocument.model_validate(document)
        except ValidationError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "policy source is invalid",
                details={"source": uri, "violations": len(exc.errors())},
            ) from exc

    @staticmethod
    def _read(uri: str, project_root: Path) -> bytes:
        if uri.startswith("package://"):
            location = uri.removeprefix("package://")
            module, separator, resource = location.partition("/")
            if not separator or not module or not resource:
                raise MishkanError(
                    ErrorCode.CONFIGURATION,
                    "package policy URI must identify a module and resource",
                    details={"source": uri},
                )
            try:
                return files(module).joinpath(resource).read_bytes()
            except (ModuleNotFoundError, OSError) as exc:
                raise MishkanError(
                    ErrorCode.CONFIGURATION,
                    "package policy source cannot be read",
                    details={"source": uri, "reason": type(exc).__name__},
                ) from exc
        if uri.startswith("project:"):
            path = project_root / uri.removeprefix("project:")
        else:
            path = Path(uri)
            if not path.is_absolute():
                path = project_root / path
        try:
            return path.resolve().read_bytes()
        except OSError as exc:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "policy source cannot be read",
                details={"source": uri, "reason": type(exc).__name__},
            ) from exc
