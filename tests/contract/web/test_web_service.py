from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import AnyHttpUrl
from support.i02 import context_for, inspector, policy_for

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
from mishkan.policy import Decision, PolicyAuthority
from mishkan.tools.gateway import CapabilityGateway, MappingCredentialResolver, MemoryEvidenceSink
from mishkan.tools.gateway_models import CallStatus, DeclaredTargets
from mishkan.web.adapters import (
    ProviderSearchResult,
    SearxngSearchAdapter,
    TrafilaturaExtractionAdapter,
)
from mishkan.web.cache import SQLiteWebCache
from mishkan.web.models import (
    CrawlRequest,
    ExtractionRequest,
    FetchRequest,
    HttpRequest,
    MapRequest,
    RedirectPolicy,
    SearchHit,
    SearchRequest,
    SourceSpan,
    WebOperationContext,
)
from mishkan.web.network import ConnectionEvidence, HttpExchange
from mishkan.web.service import WebService
from mishkan.web.tools import build_web_tool_adapters


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


class RecordingSearch:
    adapter_id = "test.record"

    def __init__(self, *, failing_sources: tuple[str, ...] = ()) -> None:
        self.calls: list[str] = []
        self.failing_sources = set(failing_sources)

    def search(self, *args: object, **kwargs: object) -> ProviderSearchResult:
        del args
        source_id = str(kwargs["source_id"])
        self.calls.append(source_id)
        if source_id in self.failing_sources:
            raise MishkanError(ErrorCode.WEB, "verification source failed", retryable=True)
        return ProviderSearchResult(
            hits=(
                SearchHit(
                    source_id=source_id,
                    upstream=source_id,
                    rank=1,
                    title=f"Evidence from {source_id}",
                    url=AnyHttpUrl(f"https://{source_id}.example/evidence"),
                    snippet="Observed result",
                    score_scale=f"{source_id}.rank",
                ),
            ),
            upstreams=(source_id,),
        )


def _two_source_config(strategy: SearchStrategy) -> WebConfig:
    base = _web_config()
    template = base.sources["searxng-local"]
    first = template.model_copy(
        update={
            "adapter": "test.record",
            "endpoint": AnyHttpUrl("https://first.example/search"),
            "network_profile": "public-read",
        }
    )
    second = template.model_copy(
        update={
            "adapter": "test.record",
            "endpoint": AnyHttpUrl("https://second.example/search"),
            "network_profile": "public-read",
        }
    )
    return base.model_copy(
        update={
            "sources": {"first": first, "second": second},
            "default_search_sources": ("first", "second"),
            "default_search_strategy": strategy,
        }
    )


def test_configured_default_search_strategy_is_executed_when_request_omits_it(
    tmp_path: Path,
) -> None:
    config = _two_source_config(SearchStrategy.AUTOMATIC)
    adapter = RecordingSearch()
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={adapter.adapter_id: adapter},
        extraction_adapters={},
    )

    result = service.search(SearchRequest(query="default strategy", cache=False))

    assert result.strategy is SearchStrategy.AUTOMATIC
    assert adapter.calls == ["first"]


def test_aggregate_queries_every_selected_independent_route(tmp_path: Path) -> None:
    config = _two_source_config(SearchStrategy.AGGREGATE)
    adapter = RecordingSearch()
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={adapter.adapter_id: adapter},
        extraction_adapters={},
    )

    result = service.search(SearchRequest(query="aggregate evidence", cache=False))

    assert adapter.calls == ["first", "second"]
    assert [route.source_id for route in result.routes] == ["first", "second"]
    assert {hit.score_scale for hit in result.hits} == {"first.rank", "second.rank"}


def test_verification_requires_two_completed_independent_routes(tmp_path: Path) -> None:
    config = _two_source_config(SearchStrategy.VERIFICATION)
    adapter = RecordingSearch(failing_sources=("first",))
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={adapter.adapter_id: adapter},
        extraction_adapters={},
    )

    with pytest.raises(MishkanError) as caught:
        service.search(SearchRequest(query="verify evidence", cache=False))

    assert adapter.calls == ["first", "second"]
    assert caught.value.envelope.details["completed_routes"] == 1
    assert caught.value.envelope.details["verification_origins"] == ["upstream:second"]
    assert caught.value.envelope.details["required_verification_origins"] == 2


def test_verification_accepts_two_observed_upstreams_from_one_broker(tmp_path: Path) -> None:
    base = _web_config()
    broker = base.sources["searxng-local"].model_copy(update={"adapter": "test.broker"})
    config = base.model_copy(
        update={
            "sources": {"broker": broker},
            "default_search_sources": ("broker",),
            "default_search_strategy": SearchStrategy.VERIFICATION,
        }
    )

    class BrokerSearch:
        adapter_id = "test.broker"

        def search(self, *args: object, **kwargs: object) -> ProviderSearchResult:
            del args, kwargs
            return ProviderSearchResult(hits=(), upstreams=("duckduckgo", "brave"))

    adapter = BrokerSearch()
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={adapter.adapter_id: adapter},
        extraction_adapters={},
    )

    result = service.search(SearchRequest(query="broker verification", cache=False))

    assert result.strategy is SearchStrategy.VERIFICATION
    assert result.routes[0].upstreams == ("duckduckgo", "brave")


def test_verification_and_aggregate_refuse_duplicate_route_aliases_before_network(
    tmp_path: Path,
) -> None:
    base = _web_config()
    source = base.sources["searxng-local"].model_copy(update={"adapter": "test.record"})
    config = base.model_copy(
        update={
            "sources": {"first": source, "alias": source},
            "default_search_sources": ("first", "alias"),
            "default_search_strategy": SearchStrategy.VERIFICATION,
        }
    )
    adapter = RecordingSearch()
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={adapter.adapter_id: adapter},
        extraction_adapters={},
    )

    with pytest.raises(MishkanError) as caught:
        service.search(SearchRequest(query="duplicate route", cache=False))

    assert caught.value.envelope.details["duplicates"] == [
        {"source_id": "alias", "duplicates": "first"}
    ]
    assert adapter.calls == []


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
    assert result.lost_coverage == ("first", "cache")
    assert result.cache.value == "unavailable"
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
        allowed_redirect_origins=("https://other.example",),
        accepted_media=("text/*",),
    )

    result = service.fetch(request, _context())

    assert transport.calls[0][2]["Authorization"] == "Bearer secret-canary"
    assert "Authorization" not in transport.calls[1][2]
    assert result.redirects[0].credential_forwarded is False
    assert result.artifact_reference.startswith("artifact:")


def test_cross_origin_redirect_requires_an_explicit_origin_scope(tmp_path: Path) -> None:
    transport = RecordingTransport(
        [_exchange(b"", status=302, headers={"location": "https://other.example/final"})]
    )
    service = WebService(
        _web_config(),
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    request = FetchRequest(
        url=AnyHttpUrl("https://first.example/start"),
        network_profile="public-read",
        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
        cache=False,
    )

    with pytest.raises(MishkanError) as caught:
        service.fetch(request, _context())

    assert caught.value.envelope.code is ErrorCode.WEB
    assert caught.value.envelope.details["origin"] == "https://other.example"
    assert len(transport.calls) == 1


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


def test_stateful_http_request_is_distinct_from_fetch(tmp_path: Path) -> None:
    transport = RecordingTransport(
        [_exchange(b'{"updated": true}', headers={"content-type": "application/json"})]
    )
    service = WebService(
        _web_config(),
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    request = HttpRequest(
        method="POST",
        url=AnyHttpUrl("https://example.com/items"),
        network_profile="public-read",
        accepted_media=("application/json",),
        redirect_policy=RedirectPolicy.SAME_METHOD,
        cache=False,
    )

    result = service.request(request, _context())

    assert result.method == "POST"
    assert result.cache.value == "bypass"
    assert transport.calls[0][0] == "POST"


def test_fetch_runs_through_the_same_governed_gateway_used_by_crewai(tmp_path: Path) -> None:
    transport = RecordingTransport([_exchange(b"governed Web evidence")])
    config = _web_config()
    artifacts = _artifacts(tmp_path)
    service = WebService(
        config,
        artifacts,
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    adapters = build_web_tool_adapters(config, service)
    origin = "https://example.com"
    policy = policy_for(
        "web.fetch",
        Decision.ALLOW,
        effect_class="network",
        network_destinations=(origin,),
        allow_network=True,
    )
    context = context_for(tmp_path, "web.fetch", policy, (origin,), network=True)
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        adapters,
        MemoryEvidenceSink(),
    )
    request = FetchRequest(
        url=AnyHttpUrl("https://example.com/proof"),
        network_profile="public-read",
        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
        cache=False,
    )
    arguments = {
        "request": request.model_dump(mode="json"),
        "network_destinations": [origin],
        "credential_refs": [],
        "declared_effects": ["external_read"],
    }

    result = gateway.invoke(
        context,
        arguments,
        DeclaredTargets(network_destinations=(origin,)),
    )

    assert result.status is CallStatus.COMPLETED
    assert result.output is not None
    assert result.output["artifact_reference"].startswith("artifact:")
    assert len(transport.calls) == 1


def test_web_adapter_refuses_target_drift_before_network_dispatch(tmp_path: Path) -> None:
    transport = RecordingTransport([_exchange(b"must remain unused")])
    config = _web_config()
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    adapters = build_web_tool_adapters(config, service)
    declared_origin = "https://wrong.example"
    policy = policy_for(
        "web.fetch",
        Decision.ALLOW,
        effect_class="network",
        network_destinations=("*",),
        allow_network=True,
    )
    context = context_for(
        tmp_path,
        "web.fetch",
        policy,
        (declared_origin,),
        network=True,
    )
    gateway = CapabilityGateway(
        tmp_path,
        PolicyAuthority(),
        MappingCredentialResolver({}),
        inspector(tmp_path),
        adapters,
        MemoryEvidenceSink(),
    )
    request = FetchRequest(
        url=AnyHttpUrl("https://example.com/proof"),
        network_profile="public-read",
        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
        cache=False,
    )

    result = gateway.invoke(
        context,
        {
            "request": request.model_dump(mode="json"),
            "network_destinations": [declared_origin],
            "credential_refs": [],
            "declared_effects": ["external_read"],
        },
        DeclaredTargets(network_destinations=(declared_origin,)),
    )

    assert result.status is CallStatus.FAILED
    assert result.error_code == ErrorCode.TOOL_SCHEMA
    assert transport.calls == []


def test_fetch_cache_is_persistent_and_reports_freshness(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    artifacts = _artifacts(tmp_path)
    transport = RecordingTransport([_exchange(b"cache proof")])
    cache = SQLiteWebCache(database)
    service = WebService(
        _web_config(),
        artifacts,
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
        cache=cache,
    )
    request = FetchRequest(
        method="GET",
        url=AnyHttpUrl("https://example.com/cache"),
        network_profile="public-read",
        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
    )

    first = service.fetch(request, _context())
    second = service.fetch(request, _context())

    assert first.cache.value == "miss"
    assert second.cache.value == "fresh"
    assert second.cached_at is not None
    assert second.fresh_until is not None
    assert second.artifact_reference == first.artifact_reference
    assert len(transport.calls) == 1


def test_cache_staleness_is_explicit_and_bounded(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    cache = SQLiteWebCache(database)
    now = utc_now()
    cache.put("sha256:" + "a" * 64, kind="fixture", payload="{}", ttl_seconds=5, now=now)

    stale = cache.get(
        "sha256:" + "a" * 64,
        kind="fixture",
        allow_stale_seconds=10,
        now=now + timedelta(seconds=6),
    )
    expired = cache.get(
        "sha256:" + "a" * 64,
        kind="fixture",
        allow_stale_seconds=10,
        now=now + timedelta(seconds=16),
    )

    assert stale is not None and stale.disposition.value == "stale"
    assert expired is None


def test_cache_replacement_pruning_and_kind_separation_are_durable(tmp_path: Path) -> None:
    database = tmp_path / "mishkan.db"
    SchemaManager(database).initialize()
    cache = SQLiteWebCache(database)
    key = "sha256:" + "b" * 64
    now = utc_now()
    cache.put(key, kind="search", payload='{"value": 1}', ttl_seconds=1, now=now)
    cache.put(key, kind="search", payload='{"value": 2}', ttl_seconds=1, now=now)

    assert cache.get(key, kind="fetch", allow_stale_seconds=0, now=now) is None
    current = cache.get(key, kind="search", allow_stale_seconds=0, now=now)
    assert current is not None and current.payload == '{"value": 2}'
    assert cache.prune(before=now + timedelta(seconds=2)) == 1
    assert cache.prune(before=now + timedelta(seconds=2)) == 0
    cache.delete(key)


def test_map_is_origin_scoped_and_stops_at_the_declared_page_bound(tmp_path: Path) -> None:
    base = _web_config()
    crawler = base.crawlers["bounded-http"].model_copy(
        update={"robots_profile": "ignore", "delay_seconds": 0.0}
    )
    config = base.model_copy(update={"crawlers": {"bounded-http": crawler}})
    transport = RecordingTransport(
        [
            _exchange(
                b'<html><body><a href="/two">two</a>'
                b'<a href="https://outside.example/escape">outside</a></body></html>'
            ),
            _exchange(b"<html><body>second page</body></html>"),
        ]
    )
    service = WebService(
        config,
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
        transport=transport,  # type: ignore[arg-type]
    )
    request = MapRequest(
        root_url=AnyHttpUrl("https://example.com/root"),
        crawler_id="bounded-http",
        max_depth=2,
        max_pages=2,
        max_concurrency=1,
        delay_seconds=0,
        robots_profile="ignore",
        render_mode="http",
    )

    result = service.map(request, _context())

    assert result.operation == "map"
    assert [str(page.url) for page in result.pages] == [
        "https://example.com/root",
        "https://example.com/two",
    ]
    assert all("outside.example" not in call[1] for call in transport.calls)


def test_crawl_refuses_request_bounds_above_configured_profile(tmp_path: Path) -> None:
    service = WebService(
        _web_config(),
        _artifacts(tmp_path),
        search_adapters={},
        extraction_adapters={},
    )
    request = CrawlRequest(
        root_url=AnyHttpUrl("https://example.com/root"),
        crawler_id="bounded-http",
        max_depth=4,
        max_pages=1,
        max_concurrency=1,
        delay_seconds=0.25,
        robots_profile="respect",
        render_mode="http",
    )

    with pytest.raises(MishkanError) as caught:
        service.crawl(request, _context())

    assert caught.value.envelope.code is ErrorCode.WEB
    assert "max_depth" in caught.value.envelope.details["bounds"]


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
