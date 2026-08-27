from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import AnyHttpUrl

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import (
    CredentialReference,
    CredentialSource,
    MishkanConfig,
    SearchStrategy,
    WebConfig,
    WebSourceConfig,
)
from mishkan.config.presets import preset_text
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.domain.time import utc_now
from mishkan.persistence import SchemaManager
from mishkan.web.adapters import (
    ProviderSearchResult,
    SearxngSearchAdapter,
    TrafilaturaExtractionAdapter,
)
from mishkan.web.models import (
    ExtractionRequest,
    FetchRequest,
    RedirectPolicy,
    SearchHit,
    SearchRequest,
    SourceSpan,
    WebOperationContext,
)
from mishkan.web.network import ConnectionEvidence, HttpExchange
from mishkan.web.service import WebService


def _web_config() -> WebConfig:
    config = MishkanConfig.model_validate(yaml.safe_load(preset_text("local")))
    assert config.web is not None
    return config.web


def _artifacts(tmp_path: Path) -> DurableArtifactService:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    return DurableArtifactService(
        database,
        tmp_path / "artifacts",
        max_artifact_bytes=2_000_000,
        max_chunk_bytes=64_000,
    )


def _context() -> WebOperationContext:
    return WebOperationContext(
        producer_identity="role:Research_Engineer",
        run_id="run-1",
        task_attempt_id="attempt-1",
        call_id="call-1",
        capability="web.fetch",
    )


class FailingSearch:
    adapter_id = "test.fail"

    def search(self, *args: object, **kwargs: object) -> ProviderSearchResult:
        del args, kwargs
        raise MishkanError(ErrorCode.WEB, "source temporarily failed", retryable=True)


class SuccessfulSearch:
    adapter_id = "test.success"

    def search(self, *args: object, **kwargs: object) -> ProviderSearchResult:
        del args, kwargs
        return ProviderSearchResult(
            hits=(
                SearchHit(
                    source_id="second",
                    upstream="fixture",
                    rank=1,
                    title="Evidence",
                    url=AnyHttpUrl("https://example.com/evidence"),
                    snippet="Observed result",
                    raw_score=0.5,
                    score_scale="fixture-only",
                ),
            ),
            upstreams=("fixture",),
        )


def test_automatic_search_exposes_failed_route_before_compatible_fallback(
    tmp_path: Path,
) -> None:
    base = _web_config()
    template = base.sources["searxng-local"]
    first = template.model_copy(update={"adapter": "test.fail"})
    second = template.model_copy(update={"adapter": "test.success"})
    config = base.model_copy(
        update={
            "sources": {"first": first, "second": second},
            "default_search_sources": ("first", "second"),
        }
    )
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={"test.fail": FailingSearch(), "test.success": SuccessfulSearch()},
        extraction_adapters={},
    )

    result = service.search(
        SearchRequest(query="typed evidence", strategy=SearchStrategy.AUTOMATIC)
    )

    assert [route.status.value for route in result.routes] == ["failed", "completed"]
    assert result.lost_coverage == ("first",)
    assert result.degraded
    assert result.hits[0].source_id == "second"


class RecordingTransport:
    def __init__(self, exchanges: list[HttpExchange]) -> None:
        self.exchanges = exchanges
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        profile: object,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpExchange:
        del profile, content, timeout_seconds
        self.calls.append((method, url, dict(headers or {})))
        return self.exchanges.pop(0)


def _exchange(
    body: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> HttpExchange:
    return HttpExchange(
        status_code=status,
        headers=headers or {"content-type": "text/html"},
        content=body,
        wire_bytes=len(body),
        decoded_bytes=len(body),
        connection=ConnectionEvidence(("93.184.216.34",), "93.184.216.34"),
    )


def test_cross_origin_redirect_never_forwards_late_resolved_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("I04_WEB_TOKEN", "secret-canary")
    transport = RecordingTransport(
        [
            _exchange(
                b"",
                status=302,
                headers={"location": "https://other.example/final"},
            ),
            _exchange(b"<html><body>done</body></html>"),
        ]
    )
    service = WebService(
        _web_config(),
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    request = FetchRequest(
        method="GET",
        url=AnyHttpUrl("https://first.example/start"),
        network_profile="public-read",
        credential_refs=(
            CredentialReference(source=CredentialSource.ENV, locator="I04_WEB_TOKEN"),
        ),
        credential_origin="https://first.example",
        credential_header="Authorization",
        credential_prefix="Bearer ",
        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
        accepted_media=("text/*",),
    )

    result = service.fetch(request, _context())

    assert transport.calls[0][2]["Authorization"] == "Bearer secret-canary"
    assert "Authorization" not in transport.calls[1][2]
    assert result.redirects[0].credential_forwarded is False
    assert result.artifact_reference.startswith("artifact:")


def test_plain_credential_header_is_refused_before_transport(tmp_path: Path) -> None:
    transport = RecordingTransport([])
    service = WebService(
        _web_config(),
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    request = FetchRequest(
        method="GET",
        url=AnyHttpUrl("https://example.com"),
        network_profile="public-read",
        headers={"Authorization": "secret-canary"},
        redirect_policy=RedirectPolicy.NONE,
    )

    with pytest.raises(MishkanError) as caught:
        service.fetch(request, _context())

    assert caught.value.envelope.code is ErrorCode.SECRET_CONTENT
    assert transport.calls == []


def test_searxng_broker_preserves_observed_upstreams_and_source_score() -> None:
    document = {
        "results": [
            {
                "title": "Result",
                "url": "https://example.com/result",
                "content": "Snippet",
                "score": 3.5,
                "engines": ["duckduckgo", "brave"],
            }
        ]
    }
    transport = RecordingTransport([_exchange(json.dumps(document).encode())])
    config = _web_config()
    source: WebSourceConfig = config.sources["searxng-local"]
    adapter = SearxngSearchAdapter(transport)

    result = adapter.search(
        SearchRequest(query="evidence", strategy=SearchStrategy.DIRECT),
        source_id="searxng-local",
        source=source,
        profile=config.network_profiles["local-services"],
        credentials=(None,),
    )

    assert result.upstreams == ("duckduckgo", "brave")
    assert result.hits[0].raw_score == 3.5
    assert result.hits[0].score_scale == "searxng.instance"


def test_extraction_and_citation_bind_exact_immutable_artifact_span(tmp_path: Path) -> None:
    artifacts = _artifacts(tmp_path)
    html = b"<html><head><title>Proof</title></head><body><p>Exact evidence text.</p></body></html>"
    manifest = artifacts.put_bytes(
        html,
        media_type="text/html",
        provenance=ArtifactProvenance(
            producer_identity="test",
            run_id="run-1",
            task_attempt_id="attempt-1",
            call_id="fetch-1",
            capability="web.fetch",
            channel="response",
        ),
        complete=True,
    )
    service = WebService(
        _web_config(),
        artifacts,
        search_adapters={},
        extraction_adapters={"trafilatura.extract": TrafilaturaExtractionAdapter()},
    )
    extracted = service.extract(
        ExtractionRequest(
            artifact_reference=manifest.reference,
            source_url=AnyHttpUrl("https://example.com/proof"),
            extractor_id="trafilatura",
        ),
        _context(),
    )
    output = artifacts.read_bytes(extracted.output_artifact_reference)
    span = SourceSpan(
        start=0,
        end=len(output),
        text_hash="sha256:" + hashlib.sha256(output).hexdigest(),
    )

    citation = service.citation(
        claim_id="claim-1",
        source_url="https://example.com/proof",
        retrieved_at=utc_now(),
        artifact_reference=extracted.output_artifact_reference,
        span=span,
    )

    assert citation.content_digest == extracted.output_digest
    assert citation.span == span
