"""Export public contract schemas for non-Python consumers."""

import json
from pathlib import Path

from pydantic import BaseModel

from mishkan.application.contracts import ApplicationCommand, CommandResult, SnapshotEnvelope
from mishkan.artifacts.models import (
    ArtifactCollection,
    ArtifactManifest,
    GarbageCollectionPlan,
    UploadSession,
    WorkingReference,
)
from mishkan.config.models import MishkanConfig
from mishkan.domain.errors import ErrorEnvelope
from mishkan.domain.identity import DomainRecord
from mishkan.edits.models import ChangeSet, ChangeSetResult
from mishkan.events.models import EventEnvelope, EventPage
from mishkan.execution.sessions import CursorRead, SessionRecord, SessionRequest

SCHEMAS: dict[str, type[BaseModel]] = {
    "application-command-v1.schema.json": ApplicationCommand,
    "artifact-collection-v1.schema.json": ArtifactCollection,
    "artifact-gc-plan-v1.schema.json": GarbageCollectionPlan,
    "artifact-manifest-v1.schema.json": ArtifactManifest,
    "artifact-upload-session-v1.schema.json": UploadSession,
    "artifact-working-reference-v1.schema.json": WorkingReference,
    "change-set-result-v1.schema.json": ChangeSetResult,
    "change-set-v1.schema.json": ChangeSet,
    "command-result-v1.schema.json": CommandResult,
    "config-v1.schema.json": MishkanConfig,
    "domain-record-v1.schema.json": DomainRecord,
    "error-envelope-v1.schema.json": ErrorEnvelope,
    "event-envelope-v1.schema.json": EventEnvelope,
    "event-page-v1.schema.json": EventPage,
    "session-cursor-read-v1.schema.json": CursorRead,
    "session-record-v1.schema.json": SessionRecord,
    "session-request-v1.schema.json": SessionRequest,
    "snapshot-envelope-v1.schema.json": SnapshotEnvelope,
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
