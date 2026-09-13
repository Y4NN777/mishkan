"""Immutable import of external evaluations as candidate-only evidence."""

from __future__ import annotations

import hashlib
from typing import Protocol

from mishkan.artifacts import ArtifactManifest, ArtifactProvenance
from mishkan.telemetry.models import (
    LangSmithFeedbackImportRequest,
    TelemetryEvaluationCandidate,
    TelemetryEvaluationImportResult,
)


class EvaluationArtifactService(Protocol):
    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        provenance: ArtifactProvenance,
        complete: bool,
        sensitivity: str = "internal",
        retention: str = "run",
        resolved_secrets: tuple[str, ...] = (),
    ) -> ArtifactManifest: ...


class TelemetryEvaluationService:
    def __init__(self, artifacts: EvaluationArtifactService) -> None:
        self._artifacts = artifacts

    def import_langsmith(
        self,
        request: LangSmithFeedbackImportRequest,
        *,
        policy_fingerprint: str,
        resolved_secrets: tuple[str, ...] = (),
    ) -> TelemetryEvaluationImportResult:
        source = request.model_dump_json(exclude={"import_id", "owner_identity"})
        source_digest = f"sha256:{hashlib.sha256(source.encode()).hexdigest()}"
        candidate = TelemetryEvaluationCandidate(
            request=request,
            source_payload_digest=source_digest,
            policy_fingerprint=policy_fingerprint,
        )
        content = candidate.model_dump_json(indent=2).encode()
        manifest = self._artifacts.put_bytes(
            content,
            media_type="application/vnd.mishkan.telemetry-evaluation-candidate+json",
            provenance=ArtifactProvenance(
                producer_identity=request.owner_identity,
                run_id=str(request.traced_run_id),
                task_attempt_id=str(request.external_feedback_id),
                call_id=str(request.import_id),
                capability="telemetry.evaluation.import",
                channel="evaluation.candidate",
                engine="langsmith-feedback-import",
                engine_version="1.0",
                configuration_fingerprint=policy_fingerprint,
            ),
            complete=True,
            sensitivity="internal",
            retention="project",
            resolved_secrets=resolved_secrets,
        )
        return TelemetryEvaluationImportResult(
            candidate=candidate,
            artifact_reference=f"artifact:{manifest.id}",
        )
