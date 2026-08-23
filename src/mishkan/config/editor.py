"""Atomic edits to explicit configuration sources."""

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from mishkan.config.loader import ConfigLoader
from mishkan.domain.errors import ErrorCode, MishkanError


def parse_yaml_value(value: str) -> Any:
    try:
        return yaml.safe_load(value)
    except yaml.YAMLError as exc:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "configuration value is malformed YAML",
        ) from exc


def set_value(source: Path, dotted_path: str, encoded_value: str) -> Path:
    target = source.expanduser().resolve()
    if not dotted_path or any(not part for part in dotted_path.split(".")):
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "configuration path must contain non-empty dot-separated fields",
            details={"field": "path"},
        )
    try:
        document = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "configuration source cannot be edited",
            details={"source": str(target), "reason": type(exc).__name__},
        ) from exc
    if not isinstance(document, dict):
        raise MishkanError(
            ErrorCode.CONFIGURATION,
            "configuration source must contain a mapping",
            details={"source": str(target)},
        )

    cursor: dict[str, Any] = document
    parts = dotted_path.split(".")
    for part in parts[:-1]:
        child = cursor.get(part)
        if child is None:
            child = {}
            cursor[part] = child
        if not isinstance(child, dict):
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "configuration path crosses a non-mapping value",
                details={"field": ".".join(parts[:-1])},
            )
        cursor = child
    cursor[parts[-1]] = parse_yaml_value(encoded_value)

    rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
        ConfigLoader().load([temp])
        temp.replace(target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return target
