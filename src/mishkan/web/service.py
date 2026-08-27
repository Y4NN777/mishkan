"""Contextual Web routing with bounded transport, artifacts, and attributable evidence."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from datetime import datetime
from fnmatch import fnmatchcase
from urllib.parse import urljoin

from pydantic import AnyHttpUrl

from mishkan.artifacts import ArtifactProvenance
from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import SearchStrategy, WebConfig
from mishkan.crewai.credentials import CredentialPoolResolver
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.adapters import ExtractionAdapter, SearchAdapter
from mishkan.web.models import (
    CacheDisposition,
    CitationEvidence,
    ExtractionRequest,
    ExtractionResult,
    FetchRequest,
    FetchResult,
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
    ) -> None:
        self._config = config
        self._artifacts = artifacts
        self._search_adapters = dict(search_adapters)
        self._extraction_adapters = dict(extraction_adapters)
        self._transport = transport or HttpxWebTransport()
        self._credentials = credential_resolver or CredentialPoolResolver()

    def search(self, request: SearchRequest) -> SearchResponse:
        source_ids = self._search_sources(request)
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
                if request.strategy is SearchStrategy.DIRECT:
                    break
                continue
            try:
                result = adapter.search(
                    request,
                    source_id=source_id,
                    source=source,
                    profile=self._config.network_profiles[source.network_profile],
                    credentials=self._credentials.resolve(source.credential_refs),
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
                if request.strategy is SearchStrategy.DIRECT:
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
            if request.strategy is SearchStrategy.AUTOMATIC:
                break
        if not any(route.status is RouteStatus.COMPLETED for route in routes):
            raise MishkanError(
                ErrorCode.WEB,
                "no selected Web search route completed",
                details={
                    "routes": [
                        {"source_id": route.source_id, "status": route.status.value}
                        for route in routes
                    ],
                    "fallback_eligible": request.strategy is SearchStrategy.AUTOMATIC,
                },
                retryable=any(route.status is RouteStatus.FAILED for route in routes),
            )
        return SearchResponse(
            query=request.query,
            strategy=request.strategy,
            hits=tuple(hits[: request.limit]),
            routes=tuple(routes),
            degraded=bool(lost) or any(route.limitation for route in routes),
            lost_coverage=tuple(lost),
        )

    def fetch(self, request: FetchRequest, context: WebOperationContext) -> FetchResult:
        try:
            profile = self._config.network_profiles[request.network_profile]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.WEB,
                "fetch references an unknown network profile",
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
        current = NetworkGuard(profile).validate_url(str(request.url))
        credential_origin = (
            NetworkGuard(profile).validate_url(request.credential_origin).origin
            if request.credential_origin
            else None
        )
        credentials = self._credentials.resolve(request.credential_refs)
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
        return FetchResult(
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
            cache=CacheDisposition.MISS if request.cache else CacheDisposition.BYPASS,
        )

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

    def _search_sources(self, request: SearchRequest) -> tuple[str, ...]:
        selected = request.source_ids or self._config.default_search_sources
        unknown = sorted(set(selected) - set(self._config.sources))
        if unknown:
            raise MishkanError(
                ErrorCode.WEB,
                "search references unknown configured sources",
                details={"source_ids": unknown},
            )
        if request.strategy is SearchStrategy.DIRECT and len(selected) != 1:
            raise MishkanError(
                ErrorCode.WEB,
                "direct search requires exactly one explicit or configured source",
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
