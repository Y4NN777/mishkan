"""Bounded native HTTP mapper/crawler built on the governed Web fetch surface."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from fnmatch import fnmatchcase
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import AnyHttpUrl

from mishkan.artifacts.service import DurableArtifactService
from mishkan.config.models import WebCrawlerConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.models import (
    CrawlPage,
    CrawlRequest,
    CrawlResult,
    ExtractionRequest,
    ExtractionResult,
    FetchRequest,
    FetchResult,
    MapResult,
    RedirectPolicy,
    WebOperationContext,
)
from mishkan.web.network import NetworkGuard


class WebReader(Protocol):
    def fetch(self, request: FetchRequest, context: WebOperationContext) -> FetchResult: ...

    def extract(
        self, request: ExtractionRequest, context: WebOperationContext
    ) -> ExtractionResult: ...


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() not in {"a", "area"}:
            return
        for name, value in attrs:
            if name.casefold() == "href" and value:
                self.links.append(value)


class NativeWebCrawler:
    adapter_id = "native.web.crawl"

    def __init__(
        self,
        reader: WebReader,
        artifacts: DurableArtifactService,
        profile: WebCrawlerConfig,
        guard: NetworkGuard,
        *,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._reader = reader
        self._artifacts = artifacts
        self._profile = profile
        self._guard = guard
        self._sleep = sleeper
        self._robots: dict[str, RobotFileParser | bool] = {}

    def run(
        self,
        request: CrawlRequest,
        context: WebOperationContext,
        *,
        operation: str,
        default_extractor: str,
    ) -> CrawlResult | MapResult:
        self._validate_request(request)
        root = self._guard.validate_url(str(request.root_url))
        allowed_origins = {
            self._guard.validate_url(value).origin for value in request.allowed_origins
        } or {root.origin}
        if root.origin not in allowed_origins:
            raise MishkanError(ErrorCode.WEB, "crawl root is outside its declared origin scope")
        queue: deque[tuple[str, int]] = deque([(root.value, 0)])
        queued = {root.value}
        visited: set[str] = set()
        pages: list[CrawlPage] = []
        lost: list[str] = []
        errors = 0
        stopped = "scope_exhausted"
        truncated = False
        while queue and len(pages) < request.max_pages:
            url, depth = queue.popleft()
            queued.discard(url)
            if url in visited:
                continue
            visited.add(url)
            if not self._allowed_by_patterns(url, request):
                self._record_lost(lost, "scope_pattern")
                continue
            if not self._robots_allowed(url, request, context):
                pages.append(
                    CrawlPage(
                        url=AnyHttpUrl(url),
                        depth=depth,
                        error_code=ErrorCode.WEB,
                        limitation="robots profile refused retrieval",
                    )
                )
                errors += 1
                self._record_lost(lost, "robots")
            else:
                page, links = self._visit(
                    url,
                    depth,
                    request,
                    context,
                    operation=operation,
                    extractor_id=request.extractor_id or default_extractor,
                )
                pages.append(page)
                if page.error_code is not None:
                    errors += 1
                if depth < request.max_depth:
                    for link in links:
                        normalized = self._scoped_link(link, url, allowed_origins)
                        if normalized is None or normalized in visited or normalized in queued:
                            continue
                        if len(visited) + len(queue) >= request.max_pages:
                            truncated = True
                            self._record_lost(lost, "page_limit")
                            break
                        queue.append((normalized, depth + 1))
                        queued.add(normalized)
                elif links:
                    truncated = True
                    self._record_lost(lost, "depth_limit")
            if errors >= request.stop_after_errors:
                stopped = "error_limit"
                truncated = bool(queue)
                break
            if queue and request.delay_seconds:
                self._sleep(request.delay_seconds)
        else:
            if queue:
                stopped = "page_limit"
                truncated = True
            elif lost and not pages:
                stopped = "scope_refused"
        result_type = MapResult if operation == "map" else CrawlResult
        return result_type(
            root_url=request.root_url,
            crawler_id=request.crawler_id,
            pages=tuple(pages),
            truncated=truncated,
            stop_reason=stopped,
            degraded=bool(lost) or any(page.error_code for page in pages),
            lost_coverage=tuple(lost),
        )

    def _validate_request(self, request: CrawlRequest) -> None:
        exceeded = {
            name: (requested, configured)
            for name, requested, configured in (
                ("max_depth", request.max_depth, self._profile.max_depth),
                ("max_pages", request.max_pages, self._profile.max_pages),
                ("max_concurrency", request.max_concurrency, self._profile.max_concurrency),
            )
            if requested > configured
        }
        if exceeded:
            raise MishkanError(
                ErrorCode.WEB,
                "crawl request exceeds its configured profile",
                details={"bounds": exceeded},
            )
        if request.delay_seconds < self._profile.delay_seconds:
            raise MishkanError(ErrorCode.WEB, "crawl delay is below its configured profile")
        if request.robots_profile != self._profile.robots_profile:
            raise MishkanError(ErrorCode.WEB, "crawl robots profile does not match configuration")
        if request.render_mode != self._profile.render_mode:
            raise MishkanError(ErrorCode.WEB, "crawl render mode does not match configuration")
        if request.render_mode != "http":
            raise MishkanError(
                ErrorCode.REQUIRED_DEPENDENCY,
                "native Web crawler supports only the HTTP render mode",
            )

    @staticmethod
    def _allowed_by_patterns(url: str, request: CrawlRequest) -> bool:
        return any(fnmatchcase(url, pattern) for pattern in request.include_patterns) and not any(
            fnmatchcase(url, pattern) for pattern in request.exclude_patterns
        )

    def _scoped_link(self, raw: str, base: str, allowed_origins: set[str]) -> str | None:
        try:
            joined = httpx.URL(urljoin(base, raw)).copy_with(fragment=None)
            target = self._guard.validate_url(str(joined))
        except (MishkanError, ValueError):
            return None
        return target.value if target.origin in allowed_origins else None

    def _visit(
        self,
        url: str,
        depth: int,
        request: CrawlRequest,
        context: WebOperationContext,
        *,
        operation: str,
        extractor_id: str,
    ) -> tuple[CrawlPage, tuple[str, ...]]:
        try:
            fetched = self._reader.fetch(
                FetchRequest(
                    method="GET",
                    url=AnyHttpUrl(url),
                    network_profile=self._profile.network_profile,
                    accepted_media=request.accepted_media,
                    redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
                    cache=request.cache,
                ),
                context,
            )
            content = self._artifacts.read_bytes(fetched.artifact_reference)
            links = self._links(content, fetched.final_url)
            extracted_reference = None
            limitation = None
            if operation == "crawl":
                try:
                    extracted = self._reader.extract(
                        ExtractionRequest(
                            artifact_reference=fetched.artifact_reference,
                            source_url=fetched.final_url,
                            extractor_id=extractor_id,
                        ),
                        context,
                    )
                    extracted_reference = extracted.output_artifact_reference
                except MishkanError as error:
                    limitation = error.envelope.message
            return (
                CrawlPage(
                    url=fetched.final_url,
                    depth=depth,
                    status_code=fetched.status_code,
                    artifact_reference=fetched.artifact_reference,
                    extracted_artifact_reference=extracted_reference,
                    links=tuple(AnyHttpUrl(link) for link in links),
                    limitation=limitation,
                ),
                links,
            )
        except MishkanError as error:
            return (
                CrawlPage(
                    url=AnyHttpUrl(url),
                    depth=depth,
                    error_code=error.envelope.code,
                    limitation=error.envelope.message,
                ),
                (),
            )

    @staticmethod
    def _links(content: bytes, base_url: AnyHttpUrl) -> tuple[str, ...]:
        parser = _LinkParser()
        parser.feed(content.decode("utf-8", errors="replace"))
        return tuple(dict.fromkeys(urljoin(str(base_url), value) for value in parser.links))

    def _robots_allowed(
        self,
        url: str,
        request: CrawlRequest,
        context: WebOperationContext,
    ) -> bool:
        if request.robots_profile != "respect":
            return True
        target = self._guard.validate_url(url)
        cached = self._robots.get(target.origin)
        if cached is None:
            try:
                fetched = self._reader.fetch(
                    FetchRequest(
                        method="GET",
                        url=AnyHttpUrl(target.origin + "/robots.txt"),
                        network_profile=self._profile.network_profile,
                        accepted_media=("text/*",),
                        redirect_policy=RedirectPolicy.SAFE_GET_HEAD,
                        cache=request.cache,
                    ),
                    context,
                )
                if fetched.status_code >= 500:
                    cached = False
                elif fetched.status_code >= 400:
                    cached = True
                else:
                    parser = RobotFileParser()
                    parser.set_url(target.origin + "/robots.txt")
                    parser.parse(
                        self._artifacts.read_bytes(fetched.artifact_reference)
                        .decode("utf-8", errors="replace")
                        .splitlines()
                    )
                    cached = parser
            except MishkanError:
                cached = False
            self._robots[target.origin] = cached
        return cached if isinstance(cached, bool) else cached.can_fetch("MISHKAN", url)

    @staticmethod
    def _record_lost(lost: list[str], reason: str) -> None:
        if reason not in lost:
            lost.append(reason)
