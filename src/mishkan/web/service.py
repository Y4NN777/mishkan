"""Contextual Web routing with bounded transport, artifacts, and attributable evidence."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Protocol
from urllib.parse import urljoin

from pydantic import AnyHttpUrl, ValidationError

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import CredentialReference, SearchStrategy, WebConfig
from mishkan.crewai.credentials import CredentialPoolResolver
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.adapters import ExtractionAdapter, SearchAdapter
from mishkan.web.cache import CacheHit, SQLiteWebCache
from mishkan.web.crawl import NativeWebCrawler
from mishkan.web.models import (
    CacheDisposition,
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
    RedirectEvidence,
    RedirectPolicy,
    RouteStatus,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SearchRoute,
    SourceSpan,
    WebOperationContext,
)
from mishkan.web.network import HttpxWebTransport, NetworkGuard


class WebCredentialResolver(Protocol):
    def resolve(self, references: tuple[CredentialReference, ...]) -> tuple[str | None, ...]: ...


class WebService:
    def __init__(
        self,
        config: WebConfig,
        artifacts: DurableArtifactService,
        *,
        search_adapters: Mapping[str, SearchAdapter],
        extraction_adapters: Mapping[str, ExtractionAdapter],
        transport: HttpxWebTransport | None = None,
        credential_resolver: CredentialPoolResolver | None = None,
        cache: SQLiteWebCache | None = None,
    ) -> None:
        self._config = config
        self._artifacts = artifacts
        self._search_adapters = dict(search_adapters)
        self._extraction_adapters = dict(extraction_adapters)
        self._transport = transport or HttpxWebTransport()
        self._credentials = credential_resolver or CredentialPoolResolver()
        self._cache = cache

    def search(
        self,
        request: SearchRequest,
        *,
        credential_resolver: WebCredentialResolver | None = None,
    ) -> SearchResponse:
        strategy = request.strategy or self._config.default_search_strategy
        effective_request = request.model_copy(update={"strategy": strategy})
        source_ids = self._search_sources(effective_request, strategy)
        cache_key = self._cache_key(
            "search",
            effective_request.model_dump(
                mode="json",
                exclude={"cache", "cache_max_age_seconds", "allow_stale_seconds"},
            ),
            {
                source_id: self._config.sources[source_id].model_dump(mode="json")
                for source_id in source_ids
            },
        )
        cached = self._cache_get(
            cache_key,
            kind="search",
            enabled=request.cache,
            allow_stale_seconds=request.allow_stale_seconds,
        )
        if cached is not None:
            try:
                response = SearchResponse.model_validate_json(cached.payload)
            except ValidationError:
                assert self._cache is not None
                self._cache.delete(cache_key)
            else:
                return response.model_copy(
                    update={
                        "cache": cached.disposition,
                        "cached_at": cached.stored_at,
                        "fresh_until": cached.fresh_until,
                    }
                )
        hits: list[SearchHit] = []
        routes: list[SearchRoute] = []
        lost: list[str] = []
        for source_id in source_ids:
            source = self._config.sources[source_id]
            route_started = time.monotonic()
            endpoint_origin = (
                NetworkGuard(self._config.network_profiles[source.network_profile])
                .validate_url(str(source.endpoint))
                .origin
            )
            adapter = self._search_adapters.get(source.adapter)
            if not source.enabled or adapter is None:
                limitation = (
                    "source is disabled"
                    if not source.enabled
                    else f"adapter {source.adapter} is unavailable"
                )
                routes.append(
                    SearchRoute(
                        source_id=source_id,
                        role=source.role,
                        endpoint_origin=endpoint_origin,
                        upstreams=source.reported_upstreams,
                        status=RouteStatus.UNAVAILABLE,
                        elapsed_ms=(time.monotonic() - route_started) * 1_000,
                        result_count=0,
                        error_code=ErrorCode.REQUIRED_DEPENDENCY,
                        limitation=limitation,
                    )
                )
                lost.append(source_id)
                if strategy is SearchStrategy.DIRECT:
                    break
                continue
            try:
                resolver = credential_resolver or self._credentials
                result = adapter.search(
                    effective_request,
                    source_id=source_id,
                    source=source,
                    profile=self._config.network_profiles[source.network_profile],
                    credentials=resolver.resolve(source.credential_refs),
                )
            except MishkanError as error:
                routes.append(
                    SearchRoute(
                        source_id=source_id,
                        role=source.role,
                        endpoint_origin=endpoint_origin,
                        upstreams=source.reported_upstreams,
                        status=RouteStatus.FAILED,
                        elapsed_ms=(time.monotonic() - route_started) * 1_000,
                        result_count=0,
                        error_code=error.envelope.code,
                        limitation=error.envelope.message,
                    )
                )
                lost.append(source_id)
                if strategy is SearchStrategy.DIRECT:
                    break
                continue
            routes.append(
                SearchRoute(
                    source_id=source_id,
                    role=source.role,
                    endpoint_origin=endpoint_origin,
                    upstreams=result.upstreams,
                    status=RouteStatus.COMPLETED,
                    elapsed_ms=(time.monotonic() - route_started) * 1_000,
                    result_count=len(result.hits),
                    limitation=result.limitation,
                )
            )
            hits.extend(result.hits)
            if strategy is SearchStrategy.AUTOMATIC:
                break
        completed_routes = sum(route.status is RouteStatus.COMPLETED for route in routes)
        verification_origins = {
            f"upstream:{upstream.casefold()}"
            for route in routes
            if route.status is RouteStatus.COMPLETED and route.upstreams
            for upstream in route.upstreams
        }
        verification_origins.update(
            f"route:{route.source_id}"
            for route in routes
            if route.status is RouteStatus.COMPLETED and not route.upstreams
        )
        enough_verification = (
            strategy is not SearchStrategy.VERIFICATION or len(verification_origins) >= 2
        )
        if completed_routes < 1 or not enough_verification:
            raise MishkanError(
                ErrorCode.WEB,
                "selected Web search strategy did not produce enough attributable evidence",
                details={
                    "routes": [
                        {"source_id": route.source_id, "status": route.status.value}
                        for route in routes
                    ],
                    "strategy": strategy.value,
                    "completed_routes": completed_routes,
                    "verification_origins": sorted(verification_origins),
                    "required_verification_origins": (
                        2 if strategy is SearchStrategy.VERIFICATION else 0
                    ),
                    "fallback_eligible": strategy is SearchStrategy.AUTOMATIC,
                },
                retryable=any(route.status is RouteStatus.FAILED for route in routes),
            )
        cache_unavailable = request.cache and self._cache is None
        response = SearchResponse(
            query=request.query,
            strategy=strategy,
            hits=tuple(hits[: request.limit]),
            routes=tuple(routes),
            degraded=bool(lost) or any(route.limitation for route in routes) or cache_unavailable,
            lost_coverage=tuple([*lost, "cache"] if cache_unavailable else lost),
            cache=(
                CacheDisposition.UNAVAILABLE
                if cache_unavailable
                else CacheDisposition.MISS
                if request.cache
                else CacheDisposition.BYPASS
            ),
        )
        self._cache_put(
            cache_key,
            kind="search",
            value=response,
            enabled=request.cache,
            max_age_seconds=request.cache_max_age_seconds,
        )
        return response

    def fetch(
        self,
        request: FetchRequest,
        context: WebOperationContext,
        *,
        credential_resolver: WebCredentialResolver | None = None,
    ) -> FetchResult:
        result = self._request(
            request,
            context,
            kind="fetch",
            result_type=FetchResult,
            credential_resolver=credential_resolver,
        )
        if not isinstance(result, FetchResult):
            raise MishkanError(ErrorCode.WEB, "Web fetch returned the wrong result type")
        return result

    def request(
        self,
        request: HttpRequest,
        context: WebOperationContext,
        *,
        credential_resolver: WebCredentialResolver | None = None,
    ) -> HttpResult:
        return self._request(
            request,
            context,
            kind="request",
            result_type=HttpResult,
            credential_resolver=credential_resolver,
        )

    def _request(
        self,
        request: HttpRequest,
        context: WebOperationContext,
        *,
        kind: str,
        result_type: type[HttpResult],
        credential_resolver: WebCredentialResolver | None,
    ) -> HttpResult:
        try:
            profile = self._config.network_profiles[request.network_profile]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "Web request references an unknown network profile",
                details={"network_profile": request.network_profile},
            ) from exc
        unsafe_headers = sorted(
            key for key in request.headers if key.casefold() in profile.credential_header_names
        )
        if unsafe_headers:
            raise MishkanError(
                ErrorCode.SECRET_CONTENT,
                "credential-bearing Web headers require late credential references",
                details={"headers": unsafe_headers},
            )
        cache_enabled = request.cache and not request.credential_refs
        cache_key = self._cache_key(
            kind,
            request.model_dump(
                mode="json",
                exclude={"cache", "cache_max_age_seconds", "allow_stale_seconds"},
            ),
        )
        cached = self._cache_get(
            cache_key,
            kind=kind,
            enabled=cache_enabled,
            allow_stale_seconds=request.allow_stale_seconds,
        )
        if cached is not None:
            try:
                result = result_type.model_validate_json(cached.payload)
                self._artifacts.manifest(result.artifact_reference)
            except (ValidationError, MishkanError):
                assert self._cache is not None
                self._cache.delete(cache_key)
            else:
                return result.model_copy(
                    update={
                        "cache": cached.disposition,
                        "cached_at": cached.stored_at,
                        "fresh_until": cached.fresh_until,
                    }
                )
        guard = NetworkGuard(profile)
        current = guard.validate_url(str(request.url))
        initial_origin = current.origin
        allowed_redirect_origins = {
            guard.validate_url(value).origin for value in request.allowed_redirect_origins
        }
        credential_origin = (
            NetworkGuard(profile).validate_url(request.credential_origin).origin
            if request.credential_origin
            else None
        )
        resolver = credential_resolver or self._credentials
        credentials = resolver.resolve(request.credential_refs)
        body = (
            self._artifacts.read_bytes(request.body_artifact_reference)
            if request.body_artifact_reference
            else None
        )
        redirects: list[RedirectEvidence] = []
        final_exchange = None
        for redirect_count in range(profile.max_redirects + 1):
            base_headers = dict(request.headers)
            credential_forwarded = current.origin == credential_origin and bool(
                request.credential_refs
            )
            if credential_forwarded:
                assert request.credential_header is not None
                credential = credentials[0]
                if credential is not None:
                    base_headers[request.credential_header] = request.credential_prefix + credential
            exchange = self._transport.request(
                request.method,
                current.value,
                profile=profile,
                headers=base_headers,
                content=body,
                timeout_seconds=request.timeout_seconds,
            )
            location = exchange.headers.get("location")
            if 300 <= exchange.status_code < 400 and location:
                if request.redirect_policy is RedirectPolicy.NONE:
                    final_exchange = exchange
                    break
                if (
                    request.redirect_policy is RedirectPolicy.SAFE_GET_HEAD
                    and request.method not in {"GET", "HEAD"}
                ):
                    raise MishkanError(
                        ErrorCode.WEB,
                        "stateful Web redirect requires an explicit same-method policy",
                    )
                if redirect_count >= profile.max_redirects:
                    raise MishkanError(
                        ErrorCode.WEB,
                        "web redirect count exceeds the configured bound",
                        details={"limit": profile.max_redirects},
                    )
                target = NetworkGuard(profile).validate_url(urljoin(current.value, location))
                if (
                    target.origin != initial_origin
                    and target.origin not in allowed_redirect_origins
                ):
                    raise MishkanError(
                        ErrorCode.WEB,
                        "web redirect origin is outside the explicit request scope",
                        details={"origin": target.origin},
                    )
                redirects.append(
                    RedirectEvidence(
                        status_code=exchange.status_code,
                        source_url=AnyHttpUrl(current.value),
                        target_url=AnyHttpUrl(target.value),
                        credential_forwarded=credential_forwarded
                        and target.origin == current.origin,
                        dns_answers=exchange.connection.dns_answers,
                        connected_address=exchange.connection.connected_address,
                    )
                )
                current = target
                continue
            final_exchange = exchange
            break
        if final_exchange is None:
            raise MishkanError(ErrorCode.WEB, "web fetch did not settle")
        media_type = final_exchange.headers.get("content-type", "").split(";", 1)[0] or None
        if media_type is not None and not any(
            fnmatchcase(media_type, pattern) for pattern in request.accepted_media
        ):
            raise MishkanError(
                ErrorCode.WEB,
                "web response media type is outside the accepted set",
                details={"media_type": media_type},
            )
        manifest = self._artifacts.put_bytes(
            final_exchange.content,
            media_type=final_exchange.headers.get("content-type", "application/octet-stream"),
            provenance=self._provenance(context, "response"),
            complete=True,
            sensitivity=context.sensitivity,
            retention=context.retention,
        )
        result = result_type(
            method=request.method,
            requested_url=request.url,
            final_url=AnyHttpUrl(current.value),
            status_code=final_exchange.status_code,
            media_type=media_type,
            artifact_reference=manifest.reference,
            content_digest=manifest.digest,
            wire_bytes=final_exchange.wire_bytes,
            decoded_bytes=final_exchange.decoded_bytes,
            redirects=tuple(redirects),
            dns_answers=final_exchange.connection.dns_answers,
            connected_address=final_exchange.connection.connected_address,
            cache=(
                CacheDisposition.UNAVAILABLE
                if cache_enabled and self._cache is None
                else CacheDisposition.MISS
                if cache_enabled
                else CacheDisposition.BYPASS
            ),
        )
        self._cache_put(
            cache_key,
            kind=kind,
            value=result,
            enabled=cache_enabled,
            max_age_seconds=request.cache_max_age_seconds,
        )
        return result

    def extract(
        self,
        request: ExtractionRequest,
        context: WebOperationContext,
    ) -> ExtractionResult:
        try:
            configured = self._config.extractors[request.extractor_id]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "extraction references an unknown configured extractor",
                details={"extractor_id": request.extractor_id},
            ) from exc
        adapter = self._extraction_adapters.get(configured.adapter)
        if not configured.enabled or adapter is None:
            raise MishkanError(
                ErrorCode.REQUIRED_DEPENDENCY,
                "configured extraction adapter is unavailable",
                details={"adapter": configured.adapter},
            )
        input_manifest = self._artifacts.manifest(request.artifact_reference)
        extracted = adapter.extract(
            self._artifacts.read_bytes(request.artifact_reference),
            source_url=str(request.source_url),
            configuration=request.configuration,
            configured=configured,
        )
        output_manifest = self._artifacts.put_bytes(
            extracted.content,
            media_type=extracted.media_type,
            provenance=self._provenance(context, "extracted"),
            complete=True,
            sensitivity=context.sensitivity,
            retention=context.retention,
        )
        span = SourceSpan(
            start=0,
            end=len(extracted.content),
            text_hash="sha256:" + hashlib.sha256(extracted.content).hexdigest(),
        )
        return ExtractionResult(
            input_artifact_reference=request.artifact_reference,
            input_digest=input_manifest.digest,
            source_url=request.source_url,
            extractor_id=request.extractor_id,
            engine_version=extracted.engine_version,
            configuration_fingerprint=extracted.configuration_fingerprint,
            output_artifact_reference=output_manifest.reference,
            output_digest=output_manifest.digest,
            canonical_url=(
                AnyHttpUrl(extracted.canonical_url) if extracted.canonical_url is not None else None
            ),
            title=extracted.title,
            warnings=extracted.warnings,
            spans=(span,),
        )

    def map(self, request: MapRequest, context: WebOperationContext) -> MapResult:
        result = self._crawl(request, context, operation="map")
        if not isinstance(result, MapResult):
            raise MishkanError(ErrorCode.WEB, "Web map adapter returned the wrong result type")
        return result

    def crawl(self, request: CrawlRequest, context: WebOperationContext) -> CrawlResult:
        return self._crawl(request, context, operation="crawl")

    def _crawl(
        self,
        request: CrawlRequest,
        context: WebOperationContext,
        *,
        operation: str,
    ) -> CrawlResult | MapResult:
        try:
            configured = self._config.crawlers[request.crawler_id]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "crawl references an unknown configured crawler",
                details={"crawler_id": request.crawler_id},
            ) from exc
        if not configured.enabled or configured.adapter != NativeWebCrawler.adapter_id:
            raise MishkanError(
                ErrorCode.REQUIRED_DEPENDENCY,
                "configured crawler adapter is unavailable",
                details={"adapter": configured.adapter},
            )
        return NativeWebCrawler(
            self,
            self._artifacts,
            configured,
            NetworkGuard(self._config.network_profiles[configured.network_profile]),
        ).run(
            request,
            context,
            operation=operation,
            default_extractor=self._config.default_extractor,
        )

    def citation(
        self,
        *,
        claim_id: str,
        source_url: str,
        retrieved_at: datetime,
        artifact_reference: str,
        span: SourceSpan,
    ) -> CitationEvidence:
        content = self._artifacts.read_bytes(artifact_reference)
        if span.end < span.start or span.end > len(content):
            raise MishkanError(ErrorCode.WEB, "citation span is outside the artifact content")
        observed = "sha256:" + hashlib.sha256(content[span.start : span.end]).hexdigest()
        if observed != span.text_hash:
            raise MishkanError(ErrorCode.WEB, "citation span hash does not match its artifact")
        return CitationEvidence(
            claim_id=claim_id,
            source_url=AnyHttpUrl(source_url),
            retrieved_at=retrieved_at,
            content_digest=self._artifacts.manifest(artifact_reference).digest,
            artifact_reference=artifact_reference,
            span=span,
        )

    def _search_sources(
        self,
        request: SearchRequest,
        strategy: SearchStrategy,
    ) -> tuple[str, ...]:
        selected = request.source_ids or self._config.default_search_sources
        unknown = sorted(set(selected) - set(self._config.sources))
        if unknown:
            raise MishkanError(
                ErrorCode.WEB,
                "search references unknown configured sources",
                details={"source_ids": unknown},
            )
        if strategy is SearchStrategy.DIRECT and len(selected) != 1:
            raise MishkanError(
                ErrorCode.WEB,
                "direct search requires exactly one explicit or configured source",
            )
        route_identities: dict[tuple[str, str], str] = {}
        duplicates: list[dict[str, str]] = []
        for source_id in selected:
            source = self._config.sources[source_id]
            identity = (source.adapter, str(source.endpoint))
            previous = route_identities.setdefault(identity, source_id)
            if previous != source_id:
                duplicates.append({"source_id": source_id, "duplicates": previous})
        if duplicates:
            raise MishkanError(
                ErrorCode.WEB,
                "search selection contains duplicate executable routes",
                details={"duplicates": duplicates},
            )
        return selected

    @staticmethod
    def _provenance(context: WebOperationContext, channel: str) -> ArtifactProvenance:
        return ArtifactProvenance(
            producer_identity=context.producer_identity,
            run_id=context.run_id,
            task_attempt_id=context.task_attempt_id,
            call_id=context.call_id,
            capability=context.capability,
            channel=channel,
        )

    @staticmethod
    def _cache_key(kind: str, *payloads: object) -> str:
        canonical = json.dumps(payloads, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(f"{kind}:{canonical}".encode()).hexdigest()

    def _cache_get(
        self,
        key: str,
        *,
        kind: str,
        enabled: bool,
        allow_stale_seconds: int,
    ) -> CacheHit | None:
        if not enabled or self._cache is None:
            return None
        return self._cache.get(
            key,
            kind=kind,
            allow_stale_seconds=allow_stale_seconds,
        )

    def _cache_put(
        self,
        key: str,
        *,
        kind: str,
        value: SearchResponse | HttpResult,
        enabled: bool,
        max_age_seconds: int | None,
    ) -> None:
        if not enabled or self._cache is None:
            return
        self._cache.put(
            key,
            kind=kind,
            payload=value.model_dump_json(),
            ttl_seconds=(
                self._config.cache_ttl_seconds if max_age_seconds is None else max_age_seconds
            ),
        )
