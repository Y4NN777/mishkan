"""Export public contract schemas for non-Python consumers."""

import json
from pathlib import Path

from pydantic import BaseModel

from mishkan.config.models import MishkanConfig
from mishkan.domain.errors import ErrorEnvelope
from mishkan.domain.identity import DomainRecord

SCHEMAS: dict[str, type[BaseModel]] = {
    "config-v1.schema.json": MishkanConfig,
    "domain-record-v1.schema.json": DomainRecord,
    "error-envelope-v1.schema.json": ErrorEnvelope,
}


def export_schemas(output: Path) -> tuple[Path, ...]:
    target = output.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in SCHEMAS.items():
        path = target / filename
        content = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return tuple(written)
