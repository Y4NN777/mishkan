"""Concrete search and extraction adapters behind typed Web ports."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
import trafilatura
from pydantic import ValidationError

from mishkan.config.models import WebExtractorConfig, WebSourceConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.models import SearchHit, SearchRequest
from mishkan.web.network import HttpExchange


class SingleRequestTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        profile: Any,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpExchange: ...


@dataclass(frozen=True, slots=True)
class ProviderSearchResult:
    hits: tuple[SearchHit, ...]
    upstreams: tuple[str, ...] | None
    limitation: str | None = None


class SearchAdapter(Protocol):
    adapter_id: str

    def search(
        self,
        request: SearchRequest,
        *,
        source_id: str,
        source: WebSourceConfig,
        profile: Any,
        credentials: tuple[str | None, ...],
    ) -> ProviderSearchResult: ...


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    content: bytes
    media_type: str
    engine_version: str
    canonical_url: str | None
    title: str | None
    warnings: tuple[str, ...]
    configuration_fingerprint: str


class ExtractionAdapter(Protocol):
    adapter_id: str

    def extract(
        self,
        content: bytes,
        *,
        source_url: str,
        configuration: dict[str, Any],
        configured: WebExtractorConfig,
    ) -> ExtractedDocument: ...


def _json_object(exchange: HttpExchange, source_id: str) -> dict[str, Any]:
    if exchange.status_code < 200 or exchange.status_code >= 300:
        raise MishkanError(
            ErrorCode.WEB,
            "web search source returned a non-success status",
            details={"source_id": source_id, "status_code": exchange.status_code},
            retryable=exchange.status_code == 429 or exchange.status_code >= 500,
        )
    try:
        value: Any = json.loads(exchange.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MishkanError(
            ErrorCode.WEB,
            "web search source returned invalid JSON",
            details={"source_id": source_id},
        ) from exc
    if not isinstance(value, dict):
        raise MishkanError(
            ErrorCode.WEB,
            "web search source returned a non-object envelope",
            details={"source_id": source_id},
        )
    return value


def _validated_hit(payload: dict[str, Any], source_id: str) -> SearchHit:
    try:
        return SearchHit.model_validate(payload)
    except ValidationError as exc:
        raise MishkanError(
            ErrorCode.WEB,
            "web search result does not match the normalized contract",
            details={"source_id": source_id, "violations": len(exc.errors())},
        ) from exc


class BraveSearchAdapter:
    adapter_id = "brave.search"

    def __init__(self, transport: SingleRequestTransport) -> None:
        self._transport = transport

    def search(
        self,
        request: SearchRequest,
        *,
        source_id: str,
        source: WebSourceConfig,
        profile: Any,
        credentials: tuple[str | None, ...],
    ) -> ProviderSearchResult:
        parameters: dict[str, str | int] = {
            "q": request.query,
            "count": min(request.limit, source.max_results),
            "safesearch": ("off", "moderate", "strict")[request.safe_search],
        }
        if request.language:
            parameters["search_lang"] = request.language
        if request.time_range:
            parameters["freshness"] = request.time_range
        endpoint = httpx.URL(str(source.endpoint)).copy_merge_params(parameters)
        exchange: HttpExchange | None = None
        for credential in credentials:
            if credential is None:
                continue
            exchange = self._transport.request(
                "GET",
                str(endpoint),
                profile=profile,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": credential,
                },
            )
            if exchange.status_code not in {401, 403, 429}:
                break
        if exchange is None:
            raise MishkanError(
                ErrorCode.WEB,
                "Brave search has no resolved credential",
                details={"source_id": source_id},
            )
        document = _json_object(exchange, source_id)
        web = document.get("web")
        raw_results = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw_results, list):
            raise MishkanError(
                ErrorCode.WEB,
                "Brave search response omits the web result list",
                details={"source_id": source_id},
            )
        hits = tuple(
            _validated_hit(
                {
                    "source_id": source_id,
                    "upstream": "brave",
                    "rank": rank,
                    "title": str(item.get("title", "")),
                    "url": item.get("url"),
                    "snippet": str(item.get("description", "")),
                    "score_scale": "brave.rank",
                },
                source_id,
            )
            for rank, item in enumerate(raw_results[: request.limit], start=1)
            if isinstance(item, dict)
        )
        return ProviderSearchResult(hits=hits, upstreams=("brave",))


class SearxngSearchAdapter:
    adapter_id = "searxng.search"

    def __init__(self, transport: SingleRequestTransport) -> None:
        self._transport = transport

    def search(
        self,
        request: SearchRequest,
        *,
        source_id: str,
        source: WebSourceConfig,
        profile: Any,
        credentials: tuple[str | None, ...],
    ) -> ProviderSearchResult:
        del credentials
        parameters: dict[str, str | int] = {
            "q": request.query,
            "format": "json",
            "safesearch": request.safe_search,
        }
        if request.language:
            parameters["language"] = request.language
        if request.time_range:
            parameters["time_range"] = request.time_range
        endpoint = httpx.URL(str(source.endpoint)).copy_merge_params(parameters)
        exchange = self._transport.request(
            "GET",
            str(endpoint),
            profile=profile,
            headers={"Accept": "application/json"},
        )
        document = _json_object(exchange, source_id)
        raw_results = document.get("results")
        if not isinstance(raw_results, list):
            raise MishkanError(
                ErrorCode.WEB,
                "SearXNG response omits the result list",
                details={"source_id": source_id},
            )
        upstreams: list[str] = []
        hits: list[SearchHit] = []
        for rank, item in enumerate(raw_results[: request.limit], start=1):
            if not isinstance(item, dict):
                continue
            engines = item.get("engines")
            observed = (
                tuple(str(value) for value in engines)
                if isinstance(engines, list)
                else ((str(item["engine"]),) if item.get("engine") else ())
            )
            for engine in observed:
                if engine not in upstreams:
                    upstreams.append(engine)
            score = item.get("score")
            hits.append(
                _validated_hit(
                    {
                        "source_id": source_id,
                        "upstream": observed[0] if len(observed) == 1 else None,
                        "rank": rank,
                        "title": str(item.get("title", "")),
                        "url": item.get("url"),
                        "snippet": str(item.get("content", "")),
                        "raw_score": float(score) if isinstance(score, int | float) else None,
                        "score_scale": "searxng.instance",
                    },
                    source_id,
                )
            )
        reported = tuple(upstreams) or source.reported_upstreams
        limitation = None if reported is not None else "broker upstream list was not reported"
        return ProviderSearchResult(tuple(hits), reported, limitation)


class TrafilaturaExtractionAdapter:
    adapter_id = "trafilatura.extract"
    _supported_options = frozenset(
        {
            "fast",
            "favor_precision",
            "favor_recall",
            "include_comments",
            "include_tables",
            "include_images",
            "include_formatting",
            "include_links",
            "deduplicate",
            "target_language",
        }
    )

    def extract(
        self,
        content: bytes,
        *,
        source_url: str,
        configuration: dict[str, Any],
        configured: WebExtractorConfig,
    ) -> ExtractedDocument:
        if len(content) > configured.max_input_bytes:
            raise MishkanError(
                ErrorCode.WEB,
                "extraction input exceeds the configured bound",
                details={"limit": configured.max_input_bytes},
            )
        unknown = sorted(set(configuration) - self._supported_options)
        if unknown:
            raise MishkanError(
                ErrorCode.WEB,
                "extractor configuration contains unsupported options",
                details={"options": unknown},
            )
        output = trafilatura.extract(
            content,
            url=source_url,
            output_format=configured.output_format,
            with_metadata=False,
            **configuration,
        )
        metadata = trafilatura.bare_extraction(
            content,
            url=source_url,
            with_metadata=True,
            **configuration,
        )
        if not output:
            raise MishkanError(
                ErrorCode.WEB,
                "extractor produced no attributable content",
                details={"extractor": self.adapter_id},
            )
        metadata_document = (
            metadata.as_dict() if metadata is not None and hasattr(metadata, "as_dict") else {}
        )
        payload = output.encode("utf-8")
        config_fingerprint = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        media_type = (
            "text/markdown; charset=utf-8"
            if configured.output_format == "markdown"
            else "text/plain; charset=utf-8"
        )
        return ExtractedDocument(
            content=payload,
            media_type=media_type,
            engine_version=importlib.metadata.version("trafilatura"),
            canonical_url=(
                str(metadata_document.get("url")) if metadata_document.get("url") else None
            ),
            title=(str(metadata_document.get("title")) if metadata_document.get("title") else None),
            warnings=(),
            configuration_fingerprint=config_fingerprint,
        )
