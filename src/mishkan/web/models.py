"""Typed Web discovery, retrieval, extraction, crawl, and citation contracts."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

from mishkan.config.models import CredentialReference, SearchStrategy, WebComponentRole
from mishkan.domain.time import require_aware, utc_now


class WebModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RouteStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class CacheDisposition(StrEnum):
    BYPASS = "bypass"
    MISS = "miss"
    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class RedirectPolicy(StrEnum):
    NONE = "none"
    SAFE_GET_HEAD = "safe_get_head"
    SAME_METHOD = "same_method"


class SearchRequest(WebModel):
    schema_version: str = "1.0"
    query: str = Field(min_length=1, max_length=16_384)
    strategy: SearchStrategy
    source_ids: tuple[str, ...] = ()
    limit: int = Field(default=10, ge=1, le=1_000)
    language: str | None = Field(default=None, min_length=2, max_length=35)
    time_range: str | None = Field(default=None, pattern=r"^(day|month|year)$")
    safe_search: int = Field(default=1, ge=0, le=2)
    cache: bool = True
    cache_max_age_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    allow_stale_seconds: int = Field(default=0, ge=0, le=31_536_000)

    @field_validator("source_ids")
    @classmethod
    def sources_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("search source identities must be unique")
        return value


class SearchRoute(WebModel):
    source_id: str
    role: WebComponentRole
    endpoint_origin: str
    upstreams: tuple[str, ...] | None
    status: RouteStatus
    elapsed_ms: float = Field(ge=0)
    result_count: int = Field(ge=0)
    error_code: str | None = None
    limitation: str | None = None


class SearchHit(WebModel):
    source_id: str
    upstream: str | None = None
    rank: int = Field(ge=1)
    title: str
    url: AnyHttpUrl
    snippet: str
    raw_score: float | None = None
    score_scale: str | None = None
    published_at: datetime | None = None

    @field_validator("published_at")
    @classmethod
    def published_time_is_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None


class SearchResponse(WebModel):
    schema_version: str = "1.0"
    query: str
    strategy: SearchStrategy
    hits: tuple[SearchHit, ...]
    routes: tuple[SearchRoute, ...]
    degraded: bool
    lost_coverage: tuple[str, ...] = ()
    cache: CacheDisposition = CacheDisposition.BYPASS
    cached_at: datetime | None = None
    fresh_until: datetime | None = None
    completed_at: datetime = Field(default_factory=utc_now)

    @field_validator("cached_at", "fresh_until")
    @classmethod
    def cache_times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None


class HttpRequest(WebModel):
    schema_version: str = "1.0"
    method: str = Field(pattern=r"^[A-Z]+$")
    url: AnyHttpUrl
    network_profile: str
    headers: dict[str, str] = Field(default_factory=dict)
    credential_refs: tuple[CredentialReference, ...] = ()
    credential_origin: str | None = None
    credential_header: str | None = None
    credential_prefix: str = ""
    body_artifact_reference: str | None = Field(default=None, pattern=r"^artifact:[0-9a-f-]{36}$")
    accepted_media: tuple[str, ...] = ("*/*",)
    redirect_policy: RedirectPolicy
    timeout_seconds: float | None = Field(default=None, gt=0, le=3_600)
    cache: bool = True
    cache_max_age_seconds: int | None = Field(default=None, ge=0, le=31_536_000)
    allow_stale_seconds: int = Field(default=0, ge=0, le=31_536_000)

    @field_validator("headers")
    @classmethod
    def header_values_are_safe(cls, value: dict[str, str]) -> dict[str, str]:
        if any(
            "\r" in key or "\n" in key or "\r" in item or "\n" in item
            for key, item in value.items()
        ):
            raise ValueError("HTTP headers must not contain line separators")
        return value

    @model_validator(mode="after")
    def credential_binding_is_complete(self) -> HttpRequest:
        declared = bool(self.credential_refs)
        if declared != bool(self.credential_origin and self.credential_header):
            raise ValueError(
                "web credential references require an exact origin and injection header"
            )
        return self


class FetchRequest(HttpRequest):
    method: Literal["GET", "HEAD"] = "GET"


class WebOperationContext(WebModel):
    producer_identity: str
    run_id: str
    task_attempt_id: str
    call_id: str
    capability: str
    sensitivity: str = "internal"
    retention: str = "run"


class RedirectEvidence(WebModel):
    status_code: int = Field(ge=300, le=399)
    source_url: AnyHttpUrl
    target_url: AnyHttpUrl
    credential_forwarded: bool
    dns_answers: tuple[str, ...]
    connected_address: str


class HttpResult(WebModel):
    schema_version: str = "1.0"
    method: str
    requested_url: AnyHttpUrl
    final_url: AnyHttpUrl
    status_code: int = Field(ge=100, le=599)
    media_type: str | None
    artifact_reference: str
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    wire_bytes: int = Field(ge=0)
    decoded_bytes: int = Field(ge=0)
    redirects: tuple[RedirectEvidence, ...]
    dns_answers: tuple[str, ...]
    connected_address: str
    cache: CacheDisposition
    retrieved_at: datetime = Field(default_factory=utc_now)
    cached_at: datetime | None = None
    fresh_until: datetime | None = None

    @field_validator("retrieved_at", "cached_at", "fresh_until")
    @classmethod
    def result_times_are_aware(cls, value: datetime | None) -> datetime | None:
        return require_aware(value) if value is not None else None


class FetchResult(HttpResult):
    method: Literal["GET", "HEAD"]


class ExtractionRequest(WebModel):
    schema_version: str = "1.0"
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    source_url: AnyHttpUrl
    extractor_id: str
    configuration: dict[str, Any] = Field(default_factory=dict)


class SourceSpan(WebModel):
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    text_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ExtractionResult(WebModel):
    schema_version: str = "1.0"
    input_artifact_reference: str
    input_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_url: AnyHttpUrl
    extractor_id: str
    engine_version: str
    configuration_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    output_artifact_reference: str
    output_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    canonical_url: AnyHttpUrl | None = None
    title: str | None = None
    warnings: tuple[str, ...] = ()
    spans: tuple[SourceSpan, ...]
    extracted_at: datetime = Field(default_factory=utc_now)


class CitationEvidence(WebModel):
    schema_version: str = "1.0"
    claim_id: str
    source_url: AnyHttpUrl
    retrieved_at: datetime
    content_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    artifact_reference: str = Field(pattern=r"^artifact:[0-9a-f-]{36}$")
    span: SourceSpan

    @field_validator("retrieved_at")
    @classmethod
    def retrieval_time_is_aware(cls, value: datetime) -> datetime:
        return require_aware(value)


class CrawlRequest(WebModel):
    schema_version: str = "1.0"
    root_url: AnyHttpUrl
    crawler_id: str
    allowed_origins: tuple[str, ...] = ()
    include_patterns: tuple[str, ...] = ("*",)
    exclude_patterns: tuple[str, ...] = ()
    max_depth: int = Field(ge=0, le=100)
    max_pages: int = Field(ge=1, le=1_000_000)
    max_concurrency: int = Field(ge=1, le=10_000)
    delay_seconds: float = Field(ge=0, le=3_600)
    robots_profile: str = Field(min_length=1)
    render_mode: str = Field(min_length=1)
    stop_after_errors: int = Field(default=10, ge=1, le=1_000_000)
    accepted_media: tuple[str, ...] = Field(
        default=("text/html", "application/xhtml+xml"), min_length=1
    )
    cache: bool = True
    extractor_id: str | None = None

    @field_validator("allowed_origins")
    @classmethod
    def origins_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("crawl origins must be unique")
        return value


class MapRequest(CrawlRequest):
    pass


class CrawlPage(WebModel):
    url: AnyHttpUrl
    depth: int = Field(ge=0)
    status_code: int | None = Field(default=None, ge=100, le=599)
    artifact_reference: str | None = None
    extracted_artifact_reference: str | None = None
    links: tuple[AnyHttpUrl, ...] = ()
    error_code: str | None = None
    limitation: str | None = None


class CrawlResult(WebModel):
    schema_version: str = "1.0"
    root_url: AnyHttpUrl
    crawler_id: str
    operation: Literal["crawl", "map"] = "crawl"
    pages: tuple[CrawlPage, ...]
    truncated: bool
    stop_reason: str
    degraded: bool
    lost_coverage: tuple[str, ...] = ()
    completed_at: datetime = Field(default_factory=utc_now)


class MapResult(CrawlResult):
    operation: Literal["map"] = "map"
