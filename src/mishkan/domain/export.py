"""Export public contract schemas for non-Python consumers."""

import json
from pathlib import Path

from pydantic import BaseModel

from mishkan.application.contracts import (
    ApplicationCommand,
    CommandResult,
    RunInitializationRequest,
    SnapshotEnvelope,
)
from mishkan.artifacts.models import (
    ArtifactCollection,
    ArtifactManifest,
    ArtifactPin,
    ArtifactReconciliationPlan,
    GarbageCollectionPlan,
    UploadSession,
    WorkingReference,
)
from mishkan.artifacts.models import (
    ArtifactHold as ArtifactEvidenceHold,
)
from mishkan.browser.models import (
    BrowserActionRequest,
    BrowserActionResult,
    BrowserDiagnosticRequest,
    BrowserDiagnosticResult,
    BrowserObservation,
    BrowserObservationRequest,
    BrowserSession,
    BrowserSessionRequest,
)
from mishkan.config.models import MishkanConfig
from mishkan.domain.errors import ErrorEnvelope
from mishkan.domain.identity import DomainRecord
from mishkan.edits.git import GitEffectRequest, GitEffectResult
from mishkan.edits.models import ChangeSet, ChangeSetResult
from mishkan.events.models import (
    EventEnvelope,
    EventPage,
    EventRetentionPlan,
    EventRetentionPolicy,
)
from mishkan.events.models import (
    EventHold as EventEvidenceHold,
)
from mishkan.execution.sessions import CursorRead, ExecutionSession
from mishkan.mcp.models import (
    McpCallRequest,
    McpCallResult,
    McpConnectionRecord,
    McpDiscoverySnapshot,
    McpPrimitiveDescriptor,
    McpProgressEvent,
)
from mishkan.runtime import TaskReviewRejection
from mishkan.tools.execution import ExecutionRequest, ExecutionResult
from mishkan.web.models import (
    CitationEvidence,
    CrawlRequest,
    CrawlResult,
    ExtractionRequest,
    ExtractionResult,
    FetchRequest,
    FetchResult,
    HttpRequest,
    HttpResult,
    MapRequest,
    MapResult,
    SearchRequest,
    SearchResponse,
)

SCHEMAS: dict[str, type[BaseModel]] = {
    "application-command-v1.schema.json": ApplicationCommand,
    "artifact-collection-v1.schema.json": ArtifactCollection,
    "artifact-hold-v1.schema.json": ArtifactEvidenceHold,
    "artifact-gc-plan-v1.schema.json": GarbageCollectionPlan,
    "artifact-manifest-v1.schema.json": ArtifactManifest,
    "artifact-pin-v1.schema.json": ArtifactPin,
    "artifact-reconciliation-plan-v1.schema.json": ArtifactReconciliationPlan,
    "artifact-upload-session-v1.schema.json": UploadSession,
    "artifact-working-reference-v1.schema.json": WorkingReference,
    "browser-action-request-v1.schema.json": BrowserActionRequest,
    "browser-action-result-v1.schema.json": BrowserActionResult,
    "browser-diagnostic-request-v1.schema.json": BrowserDiagnosticRequest,
    "browser-diagnostic-result-v1.schema.json": BrowserDiagnosticResult,
    "browser-observation-request-v1.schema.json": BrowserObservationRequest,
    "browser-observation-v1.schema.json": BrowserObservation,
    "browser-session-request-v1.schema.json": BrowserSessionRequest,
    "browser-session-v1.schema.json": BrowserSession,
    "change-set-result-v1.schema.json": ChangeSetResult,
    "change-set-v1.schema.json": ChangeSet,
    "command-result-v1.schema.json": CommandResult,
    "config-v1.schema.json": MishkanConfig,
    "domain-record-v1.schema.json": DomainRecord,
    "error-envelope-v1.schema.json": ErrorEnvelope,
    "event-envelope-v1.schema.json": EventEnvelope,
    "event-hold-v1.schema.json": EventEvidenceHold,
    "event-page-v1.schema.json": EventPage,
    "event-retention-plan-v1.schema.json": EventRetentionPlan,
    "event-retention-policy-v1.schema.json": EventRetentionPolicy,
    "execution-request-v1.schema.json": ExecutionRequest,
    "execution-result-v1.schema.json": ExecutionResult,
    "git-effect-request-v1.schema.json": GitEffectRequest,
    "git-effect-result-v1.schema.json": GitEffectResult,
    "mcp-call-request-v1.schema.json": McpCallRequest,
    "mcp-call-result-v1.schema.json": McpCallResult,
    "mcp-connection-v1.schema.json": McpConnectionRecord,
    "mcp-discovery-v1.schema.json": McpDiscoverySnapshot,
    "mcp-primitive-v1.schema.json": McpPrimitiveDescriptor,
    "mcp-progress-v1.schema.json": McpProgressEvent,
    "run-initialization-request-v1.schema.json": RunInitializationRequest,
    "web-citation-evidence-v1.schema.json": CitationEvidence,
    "web-crawl-request-v1.schema.json": CrawlRequest,
    "web-crawl-result-v1.schema.json": CrawlResult,
    "web-extraction-request-v1.schema.json": ExtractionRequest,
    "web-extraction-result-v1.schema.json": ExtractionResult,
    "web-fetch-request-v1.schema.json": FetchRequest,
    "web-fetch-result-v1.schema.json": FetchResult,
    "web-http-request-v1.schema.json": HttpRequest,
    "web-http-result-v1.schema.json": HttpResult,
    "web-map-request-v1.schema.json": MapRequest,
    "web-map-result-v1.schema.json": MapResult,
    "web-search-request-v1.schema.json": SearchRequest,
    "web-search-response-v1.schema.json": SearchResponse,
    "execution-cursor-read-v1.schema.json": CursorRead,
    "execution-session-v1.schema.json": ExecutionSession,
    "snapshot-envelope-v1.schema.json": SnapshotEnvelope,
    "task-review-rejection-v1.schema.json": TaskReviewRejection,
}

_EXPORT_MANIFEST = ".mishkan-schema-export.json"


def export_schemas(output: Path) -> tuple[Path, ...]:
    target = output.expanduser().resolve()
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / _EXPORT_MANIFEST
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous_files = previous["files"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            previous_files = []
        if isinstance(previous_files, list):
            for filename in previous_files:
                if (
                    isinstance(filename, str)
                    and Path(filename).name == filename
                    and filename.endswith(".schema.json")
                    and filename not in SCHEMAS
                ):
                    (target / filename).unlink(missing_ok=True)
    written: list[Path] = []
    for filename, model in SCHEMAS.items():
        path = target / filename
        content = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        path.write_text(content, encoding="utf-8")
        written.append(path)
    manifest_path.write_text(
        json.dumps(
            {"schema_version": "1.0", "files": sorted(SCHEMAS)},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return tuple(written)
