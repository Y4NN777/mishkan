"""Playwright Chromium adapter executed on one engine-owned thread."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import stat
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import uuid4

import httpx
from playwright.sync_api import (
    Browser,
    BrowserContext,
    CDPSession,
    ConsoleMessage,
    Download,
    FilePayload,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
    Route,
    WebSocketRoute,
    sync_playwright,
)
from playwright.sync_api import (
    Error as PlaywrightError,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from mishkan.browser.driver import (
    BrowserOperationCancelled,
    BrowserUncertainEffect,
    DriverActionOutcome,
    DriverArtifact,
    DriverDiagnostics,
    DriverObservation,
    DriverSession,
)
from mishkan.browser.models import BrowserActionKind, BrowserActionRequest, BrowserTarget
from mishkan.config.models import (
    BrowserProfileConfig,
    BrowserProfileKind,
    NetworkProfileConfig,
)
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.web.network import HttpxWebTransport, NetworkGuard, Resolver

_T = TypeVar("_T")
_REFERENCE = re.compile(r'^\s*-\s+([A-Za-z][\w-]*)(?:\s+("(?:[^"\\]|\\.)*"))?.*?\[ref=([^\]\s]+)\]')
_HOP_HEADERS = {
    "connection",
    "content-encoding",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(slots=True)
class _QueuedCall:
    operation: Callable[[], object]
    future: Future[object]


class _EngineThread:
    def __init__(self) -> None:
        self.calls: queue.Queue[_QueuedCall | None] = queue.Queue()
        self.ready = threading.Event()
        self.startup_error: BaseException | None = None
        self.playwright: Playwright | None = None
        self.thread = threading.Thread(
            target=self._run,
            name="mishkan-playwright",
            daemon=True,
        )
        self.thread.start()
        self.ready.wait()
        if self.startup_error is not None:
            raise MishkanError(
                ErrorCode.REQUIRED_DEPENDENCY,
                "Playwright engine failed to start",
            ) from self.startup_error

    def submit(self, operation: Callable[[], _T]) -> _T:
        future: Future[object] = Future()
        self.calls.put(_QueuedCall(cast(Callable[[], object], operation), future))
        return cast(_T, future.result())

    def shutdown(self) -> None:
        self.calls.put(None)
        self.thread.join(timeout=10)

    def _run(self) -> None:
        try:
            with sync_playwright() as engine:
                self.playwright = engine
                self.ready.set()
                while True:
                    call = self.calls.get()
                    if call is None:
                        return
                    try:
                        call.future.set_result(call.operation())
                    except BaseException as exc:
                        call.future.set_exception(exc)
        except BaseException as exc:
            self.startup_error = exc
            self.ready.set()


@dataclass(slots=True)
class _LiveSession:
    profile: BrowserProfileConfig
    workspace: Path
    browser: Browser | None
    context: BrowserContext
    pages: dict[str, Page] = field(default_factory=dict)
    page_keys: dict[int, str] = field(default_factory=dict)
    cdp: dict[str, CDPSession] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    diagnostic_base_cursor: int = 0
    downloads: list[Download] = field(default_factory=list)
    download_overflow: bool = False
    authorized_origins: tuple[str, ...] = ()


class PlaywrightChromiumDriver:
    """Chromium adapter with DNS-locked HTTP mediation and CDP evidence."""

    adapter_id = "playwright.chromium"

    def __init__(
        self,
        network_profiles: Mapping[str, NetworkProfileConfig],
        *,
        max_diagnostic_entries: int,
        max_pending_downloads: int,
        resolver: Resolver | None = None,
    ) -> None:
        if min(max_diagnostic_entries, max_pending_downloads) < 1:
            raise ValueError("browser live evidence bounds must be positive")
        self._network_profiles = dict(network_profiles)
        self._max_diagnostic_entries = max_diagnostic_entries
        self._max_pending_downloads = max_pending_downloads
        self._transport = HttpxWebTransport(resolver)
        self._worker = _EngineThread()
        self._sessions: dict[str, _LiveSession] = {}

    def open(
        self,
        profile: BrowserProfileConfig,
        *,
        workspace: str,
        initial_url: str | None,
    ) -> DriverSession:
        return self._worker.submit(lambda: self._open(profile, Path(workspace), initial_url))

    def observe(
        self,
        handle: str,
        page_id: str,
        *,
        screenshot: bool,
    ) -> DriverObservation:
        return self._worker.submit(lambda: self._observe(handle, page_id, screenshot))

    def act(
        self,
        handle: str,
        request: BrowserActionRequest,
        target: BrowserTarget | None,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> DriverActionOutcome:
        return self._worker.submit(
            lambda: self._act(
                handle,
                request,
                target,
                cancellation_requested=cancellation_requested,
            )
        )

    def diagnostics(
        self,
        handle: str,
        page_id: str,
        channels: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> DriverDiagnostics:
        return self._worker.submit(
            lambda: self._diagnostics(handle, page_id, channels, cursor, limit)
        )

    def close(self, handle: str) -> None:
        self._worker.submit(lambda: self._close(handle))

    def shutdown(self) -> None:
        for handle in tuple(self._sessions):
            with suppress(PlaywrightError):
                self.close(handle)
        self._worker.shutdown()

    def _open(
        self,
        profile: BrowserProfileConfig,
        workspace: Path,
        initial_url: str | None,
    ) -> DriverSession:
        engine = self._require_engine()
        browser: Browser | None = None
        timeout = profile.navigation_timeout_seconds * 1_000
        if profile.kind is BrowserProfileKind.ISOLATED:
            browser = engine.chromium.launch(headless=profile.headless, timeout=timeout)
            context = browser.new_context(service_workers="block")
        elif profile.kind is BrowserProfileKind.PROJECT_PERSISTENT:
            assert profile.user_data_dir is not None
            user_data = (workspace / profile.user_data_dir).resolve()
            if not user_data.is_relative_to(workspace.resolve()):
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "persistent browser profile escapes its workspace",
                )
            user_data.mkdir(parents=True, exist_ok=True)
            context = engine.chromium.launch_persistent_context(
                user_data,
                headless=profile.headless,
                timeout=timeout,
                service_workers="block",
            )
            browser = context.browser
        else:
            assert profile.cdp_endpoint is not None
            network_profile = self._network_profiles.get(profile.network_profile)
            if network_profile is None:
                raise MishkanError(
                    ErrorCode.CONFIGURATION,
                    "attached browser profile references an unavailable network profile",
                )
            cdp_endpoint = (
                NetworkGuard(network_profile).validate_url(str(profile.cdp_endpoint)).value
            )
            browser = engine.chromium.connect_over_cdp(cdp_endpoint, timeout=timeout)
            if not browser.contexts:
                raise MishkanError(
                    ErrorCode.BROWSER,
                    "attached Chromium endpoint has no selectable context",
                )
            context = browser.contexts[0]
        context.set_default_timeout(profile.action_timeout_seconds * 1_000)
        context.set_default_navigation_timeout(timeout)
        handle = str(uuid4())
        live = _LiveSession(profile, workspace.resolve(), browser, context)
        self._sessions[handle] = live
        try:
            self._install_network_mediation(live)
            live.authorized_origins = (
                (self._origin(initial_url),) if initial_url is not None else ()
            )
            page = context.pages[0] if context.pages else context.new_page()
            self._refresh_pages(live)
            if initial_url is not None:
                page.goto(initial_url, wait_until="domcontentloaded")
                self._refresh_pages(live)
        except BaseException:
            self._sessions.pop(handle, None)
            self._dispose(live)
            raise
        version = browser.version if browser is not None else "chromium"
        return DriverSession(handle, tuple(live.pages), version)

    def _observe(self, handle: str, page_id: str, screenshot: bool) -> DriverObservation:
        live = self._session(handle)
        page = self._page(live, page_id)
        tree = page.aria_snapshot(mode="ai")
        targets: list[BrowserTarget] = []
        for line in tree.splitlines():
            match = _REFERENCE.match(line)
            if match is None:
                continue
            role, encoded_name, reference = match.groups()
            locator = page.locator(f"aria-ref={reference}")
            if locator.count() != 1:
                continue
            name = cast(str, json.loads(encoded_name)) if encoded_name is not None else ""
            evidence = self._element_evidence(locator)
            targets.append(
                BrowserTarget(
                    reference=reference,
                    role=role,
                    name=name,
                    element_revision=self._fingerprint(evidence),
                    candidate_effects=self._candidate_effects(role, evidence),
                    destination_origin=self._destination_origin(evidence, page.url),
                )
            )
        image = self._capture_screenshot(page) if screenshot else None
        return DriverObservation(
            url=page.url,
            title=page.title(),
            tree=tree.encode(),
            targets=tuple(targets),
            screenshot=image,
        )

    def _act(
        self,
        handle: str,
        request: BrowserActionRequest,
        target: BrowserTarget | None,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> DriverActionOutcome:
        if cancellation_requested():
            raise BrowserOperationCancelled("browser action cancelled before dispatch")
        live = self._session(handle)
        page = self._page(live, request.page_id)
        locator: Locator | None = None
        if target is not None:
            locator = page.locator(f"aria-ref={target.reference}")
            if locator.count() != 1:
                raise MishkanError(ErrorCode.BROWSER, "browser target is no longer unique")
            if self._fingerprint(self._element_evidence(locator)) != target.element_revision:
                raise MishkanError(ErrorCode.BROWSER, "browser target changed after observation")
        download: Download | None = None
        download_count = len(live.downloads)
        live.download_overflow = False
        live.authorized_origins = request.authorized_origins
        try:
            if cancellation_requested():
                raise BrowserOperationCancelled("browser action cancelled before dispatch")
            if request.resolved_effect == "file.download":
                with page.expect_download(
                    timeout=live.profile.action_timeout_seconds * 1_000
                ) as download_info:
                    self._dispatch_action(live, page, locator, request)
                download = download_info.value
            else:
                self._dispatch_action(live, page, locator, request)
            self._refresh_pages(live)
        except (BrowserOperationCancelled, MishkanError):
            raise
        except (PlaywrightError, PlaywrightTimeoutError) as exc:
            raise BrowserUncertainEffect(
                "Playwright lost certainty after interaction dispatch"
            ) from exc
        observed_downloads = live.downloads[download_count:]
        if live.download_overflow:
            del live.downloads[download_count:]
            raise BrowserUncertainEffect("browser pending download evidence exceeded its bound")
        if request.resolved_effect != "file.download" and observed_downloads:
            for item in observed_downloads:
                with suppress(PlaywrightError):
                    item.cancel()
            del live.downloads[download_count:]
            raise BrowserUncertainEffect("browser action produced an undeclared download effect")
        if download is None:
            del live.downloads[download_count:]
            return DriverActionOutcome(tuple(live.pages))
        if len(observed_downloads) > 1:
            for item in observed_downloads:
                with suppress(PlaywrightError):
                    item.cancel()
            del live.downloads[download_count:]
            raise BrowserUncertainEffect("browser action produced multiple download effects")
        artifact = self._download_artifact(live, download)
        with suppress(PlaywrightError):
            download.delete()
        del live.downloads[download_count:]
        return DriverActionOutcome(tuple(live.pages), (artifact,))

    def _dispatch_action(
        self,
        live: _LiveSession,
        page: Page,
        locator: Locator | None,
        request: BrowserActionRequest,
    ) -> None:
        value = request.value
        if request.kind is BrowserActionKind.NAVIGATE:
            if not isinstance(value, str):
                raise MishkanError(ErrorCode.BROWSER, "navigation requires a URL string")
            page.goto(value, wait_until="domcontentloaded")
            return
        if request.kind is BrowserActionKind.COORDINATE_CLICK:
            if request.coordinates is None:
                raise MishkanError(ErrorCode.BROWSER, "coordinate click has no coordinates")
            page.mouse.click(*request.coordinates)
            return
        if locator is None:
            raise MishkanError(ErrorCode.BROWSER, "browser action requires a live target")
        if request.kind is BrowserActionKind.CLICK:
            locator.click()
        elif request.kind is BrowserActionKind.FILL:
            locator.fill(self._string_value(value, "fill"))
        elif request.kind is BrowserActionKind.PRESS:
            locator.press(self._string_value(value, "press"))
        elif request.kind is BrowserActionKind.SELECT:
            locator.select_option(self._string_sequence(value, "select"))
        elif request.kind is BrowserActionKind.CHECK:
            locator.check() if value is not False else locator.uncheck()
        elif request.kind is BrowserActionKind.UPLOAD:
            locator.set_input_files(self._upload_payloads(live, value))
        elif request.kind is BrowserActionKind.JAVASCRIPT:
            locator.evaluate(self._string_value(value, "JavaScript"))
        else:
            raise MishkanError(ErrorCode.BROWSER, "browser action kind is unsupported")

    def _diagnostics(
        self,
        handle: str,
        page_id: str,
        channels: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> DriverDiagnostics:
        live = self._session(handle)
        self._page(live, page_id)
        self._capture_cdp_snapshots(live, page_id, channels)
        if cursor < live.diagnostic_base_cursor:
            raise MishkanError(
                ErrorCode.RUN_INTERRUPTED,
                "browser diagnostic cursor was removed by the configured bound",
                details={"category": "cursor_gap", "snapshot_required": True},
            )
        selected: list[dict[str, Any]] = []
        next_cursor = min(
            cursor,
            live.diagnostic_base_cursor + len(live.diagnostics),
        )
        start = next_cursor - live.diagnostic_base_cursor
        for item in live.diagnostics[start:]:
            next_cursor = int(item["cursor"]) + 1
            if item.get("channel") in channels:
                selected.append(item)
                if len(selected) == limit:
                    break
        remaining_start = max(0, next_cursor - live.diagnostic_base_cursor)
        remaining = any(
            item.get("channel") in channels for item in live.diagnostics[remaining_start:]
        )
        return DriverDiagnostics(tuple(selected), next_cursor, remaining)

    def _close(self, handle: str) -> None:
        live = self._sessions.pop(handle, None)
        if live is None:
            return
        self._dispose(live)

    @staticmethod
    def _dispose(live: _LiveSession) -> None:
        if live.profile.kind is BrowserProfileKind.ATTACHED_EXISTING:
            # Dropping MISHKAN's CDP objects is a detach. Closing either the
            # selected context or Browser would terminate externally owned state.
            return
        live.context.close()
        if live.browser is not None and live.browser.is_connected():
            live.browser.close()

    def _install_network_mediation(self, live: _LiveSession) -> None:
        profile = self._network_profiles.get(live.profile.network_profile)
        if profile is None:
            raise MishkanError(
                ErrorCode.CONFIGURATION,
                "browser profile references an unavailable network profile",
            )

        def mediate(route: Route, request: Request) -> None:
            url = request.url
            try:
                self._require_origin(live.profile.allowed_origins, url)
                if self._origin(url) not in live.authorized_origins:
                    raise MishkanError(
                        ErrorCode.AUTHORITY_NOT_GRANTED,
                        "browser request destination was not authorized by the current action",
                    )
                exchange = self._transport.request(
                    request.method,
                    url,
                    profile=profile,
                    headers=dict(request.headers),
                    content=request.post_data_buffer,
                    timeout_seconds=live.profile.navigation_timeout_seconds,
                )
                headers = {
                    name: value
                    for name, value in exchange.headers.items()
                    if name.casefold() not in _HOP_HEADERS
                }
                route.fulfill(
                    status=exchange.status_code,
                    headers=headers,
                    body=exchange.content,
                )
            except (MishkanError, PlaywrightError) as exc:
                self._record(
                    live,
                    "network",
                    "blocked",
                    {"url": self._safe_url(url), "reason": type(exc).__name__},
                )
                route.abort("blockedbyclient")

        live.context.route("http://**/*", mediate)
        live.context.route("https://**/*", mediate)

        def refuse_unmediated_websocket(route: WebSocketRoute) -> None:
            self._record(
                live,
                "network",
                "blocked",
                {
                    "url": self._safe_url(route.url),
                    "reason": "unmediated_websocket_transport",
                },
            )
            route.close(code=1008, reason="WebSocket transport is not mediated by this profile")

        # A Chromium WebSocket would otherwise bypass the DNS-locked HTTP transport above.
        # No WebSocket adapter is advertised in I04, so the truthful behavior is refusal.
        live.context.route_web_socket("ws://**/*", refuse_unmediated_websocket)
        live.context.route_web_socket("wss://**/*", refuse_unmediated_websocket)

    def _refresh_pages(self, live: _LiveSession) -> None:
        for page in live.context.pages:
            identity = id(page)
            if identity in live.page_keys:
                continue
            if len(live.pages) >= live.profile.max_pages:
                raise BrowserUncertainEffect("browser page limit exceeded after interaction")
            page_id = str(uuid4())
            live.page_keys[identity] = page_id
            live.pages[page_id] = page
            self._attach_diagnostics(live, page_id, page)

    def _attach_diagnostics(self, live: _LiveSession, page_id: str, page: Page) -> None:
        page.on("download", lambda download: self._download_event(live, page_id, download))
        page.on(
            "console",
            lambda message: self._console_event(live, page_id, message),
        )
        page.on(
            "pageerror",
            lambda error: self._record(
                live,
                "console",
                "pageerror",
                {"page_id": page_id, "text": str(error)},
            ),
        )
        page.on(
            "request",
            lambda request: self._request_event(live, page_id, request),
        )
        page.on(
            "response",
            lambda response: self._response_event(live, page_id, response),
        )
        page.on(
            "crash",
            lambda _page: self._record(
                live,
                "console",
                "page_crash",
                {"page_id": page_id},
            ),
        )
        try:
            cdp = live.context.new_cdp_session(page)
            cdp.send("Performance.enable")
            live.cdp[page_id] = cdp
        except PlaywrightError:
            self._record(live, "performance", "cdp_unavailable", {"page_id": page_id})

    def _capture_cdp_snapshots(
        self,
        live: _LiveSession,
        page_id: str,
        channels: tuple[str, ...],
    ) -> None:
        cdp = live.cdp.get(page_id)
        if "performance" in channels and cdp is not None:
            result = cdp.send("Performance.getMetrics")
            metrics = {
                str(item.get("name")): item.get("value")
                for item in cast(list[dict[str, object]], result.get("metrics", []))
            }
            self._record(live, "performance", "metrics", {"page_id": page_id, **metrics})
        if "storage" in channels:
            cookies = live.context.cookies()
            domains = sorted({str(cookie.get("domain", "")) for cookie in cookies})
            self._record(
                live,
                "storage",
                "summary",
                {"page_id": page_id, "cookie_count": len(cookies), "domains": domains},
            )
        if "service_worker" in channels:
            self._record(
                live,
                "service_worker",
                "summary",
                {
                    "page_id": page_id,
                    "workers": [
                        self._safe_url(worker.url) for worker in live.context.service_workers
                    ],
                },
            )

    def _console_event(
        self,
        live: _LiveSession,
        page_id: str,
        message: ConsoleMessage,
    ) -> None:
        self._record(
            live,
            "console",
            message.type,
            {"page_id": page_id, "text": message.text},
        )

    def _request_event(self, live: _LiveSession, page_id: str, request: Request) -> None:
        self._record(
            live,
            "network",
            "request",
            {
                "page_id": page_id,
                "method": request.method,
                "resource_type": request.resource_type,
                "url": self._safe_url(request.url),
            },
        )

    def _response_event(self, live: _LiveSession, page_id: str, response: Response) -> None:
        self._record(
            live,
            "network",
            "response",
            {
                "page_id": page_id,
                "status": response.status,
                "url": self._safe_url(response.url),
            },
        )

    def _download_event(self, live: _LiveSession, page_id: str, download: Download) -> None:
        if len(live.downloads) >= self._max_pending_downloads:
            live.download_overflow = True
            with suppress(PlaywrightError):
                download.cancel()
            return
        live.downloads.append(download)
        self._record(
            live,
            "network",
            "download",
            {
                "page_id": page_id,
                "suggested_filename": Path(download.suggested_filename).name,
            },
        )

    @staticmethod
    def _download_artifact(live: _LiveSession, download: Download) -> DriverArtifact:
        failure = download.failure()
        if failure is not None:
            raise BrowserUncertainEffect("browser download did not settle successfully")
        path = download.path()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise BrowserUncertainEffect("browser download file is unavailable") from exc
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
                raise BrowserUncertainEffect("browser download file identity is unsafe")
            if observed.st_size > live.profile.max_download_bytes:
                raise BrowserUncertainEffect("browser download exceeds its configured bound")
            chunks: list[bytes] = []
            received = 0
            while True:
                remaining = live.profile.max_download_bytes + 1 - received
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if received > live.profile.max_download_bytes:
                    raise BrowserUncertainEffect("browser download exceeds its configured bound")
        finally:
            os.close(descriptor)
        return DriverArtifact(
            channel="browser.download",
            media_type="application/octet-stream",
            content=b"".join(chunks),
        )

    def _record(
        self,
        live: _LiveSession,
        channel: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        cursor = live.diagnostic_base_cursor + len(live.diagnostics)
        live.diagnostics.append({"cursor": cursor, "channel": channel, "kind": kind, **payload})
        if len(live.diagnostics) > self._max_diagnostic_entries:
            live.diagnostics.pop(0)
            live.diagnostic_base_cursor += 1

    @staticmethod
    def _element_evidence(locator: Locator) -> dict[str, object]:
        script = """element => ({
          tag: element.tagName.toLowerCase(),
          type: element.getAttribute('type') || '',
          role: element.getAttribute('role') || '',
          name: element.getAttribute('name') || '',
          href: element.getAttribute('href') || '',
          download: element.hasAttribute('download'),
          formAction: element.formAction || (element.form && element.form.action) || '',
          contentEditable: element.isContentEditable,
          text: (element.innerText || '').slice(0, 512)
        })"""
        value = locator.evaluate(script)
        if not isinstance(value, dict):
            raise MishkanError(ErrorCode.BROWSER, "browser target evidence is unavailable")
        return cast(dict[str, object], value)

    @staticmethod
    def _fingerprint(evidence: dict[str, object]) -> str:
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _candidate_effects(role: str, evidence: dict[str, object]) -> tuple[str, ...]:
        tag = str(evidence.get("tag", ""))
        input_type = str(evidence.get("type", "")).casefold()
        if evidence.get("href"):
            if evidence.get("download"):
                return ("file.download",)
            return ("navigation",)
        if input_type == "file":
            return ("file.upload",)
        if input_type == "submit" or (tag == "button" and input_type in {"", "submit"}):
            return ("form.submit",)
        if role in {"textbox", "checkbox", "radio", "combobox", "option"} or tag in {
            "input",
            "select",
            "textarea",
        }:
            return ("form.field.update",)
        return ("ui.interaction",)

    @classmethod
    def _destination_origin(
        cls,
        evidence: dict[str, object],
        page_url: str,
    ) -> str | None:
        candidate = str(evidence.get("href") or evidence.get("formAction") or "")
        if not candidate:
            return None
        return cls._origin(str(httpx.URL(page_url).join(candidate)))

    @staticmethod
    def _string_value(value: object, operation: str) -> str:
        if not isinstance(value, str):
            raise MishkanError(ErrorCode.BROWSER, f"browser {operation} requires a string")
        return value

    @staticmethod
    def _capture_screenshot(page: Page) -> bytes:
        for attempt in range(2):
            try:
                return page.screenshot(type="png")
            except PlaywrightError as exc:
                if attempt or "Unable to capture screenshot" not in str(exc):
                    raise
        raise AssertionError("bounded browser screenshot retry exhausted")

    @staticmethod
    def _string_sequence(value: object, operation: str) -> str | list[str]:
        if isinstance(value, str):
            return value
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            return cast(list[str], value)
        raise MishkanError(ErrorCode.BROWSER, f"browser {operation} requires string values")

    @staticmethod
    def _upload_payloads(live: _LiveSession, value: object) -> list[FilePayload]:
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise MishkanError(ErrorCode.BROWSER, "browser upload requires workspace paths")
        payloads: list[FilePayload] = []
        total = 0
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        for item in cast(list[str], values):
            relative = Path(item)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "browser upload escapes scope")
            directory = os.open(live.workspace, directory_flags)
            try:
                for part in relative.parts[:-1]:
                    child = os.open(part, directory_flags, dir_fd=directory)
                    os.close(directory)
                    directory = child
                file_flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(relative.name, file_flags, dir_fd=directory)
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise MishkanError(
                            ErrorCode.AUTHORITY_NOT_GRANTED,
                            "browser upload source is not a regular file",
                        )
                    remaining = live.profile.max_upload_bytes - total
                    chunks: list[bytes] = []
                    received = 0
                    while True:
                        chunk = os.read(descriptor, min(65_536, remaining + 1 - received))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        received += len(chunk)
                        if received > remaining:
                            raise MishkanError(
                                ErrorCode.OUTPUT_CONTRACT,
                                "browser upload exceeds its configured bound",
                            )
                    content = b"".join(chunks)
                    total += len(content)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise MishkanError(
                    ErrorCode.AUTHORITY_NOT_GRANTED,
                    "browser upload path could not be opened without following links",
                ) from exc
            finally:
                os.close(directory)
            payloads.append(
                FilePayload(
                    name=relative.name,
                    mimeType="application/octet-stream",
                    buffer=content,
                )
            )
        return payloads

    def _session(self, handle: str) -> _LiveSession:
        try:
            return self._sessions[handle]
        except KeyError as exc:
            raise MishkanError(
                ErrorCode.BROWSER, "Playwright session handle is unavailable"
            ) from exc

    @staticmethod
    def _page(live: _LiveSession, page_id: str) -> Page:
        try:
            return live.pages[page_id]
        except KeyError as exc:
            raise MishkanError(ErrorCode.BROWSER, "Playwright page handle is unavailable") from exc

    def _require_engine(self) -> Playwright:
        if self._worker.playwright is None:
            raise MishkanError(ErrorCode.REQUIRED_DEPENDENCY, "Playwright engine is unavailable")
        return self._worker.playwright

    @staticmethod
    def _require_origin(allowed: tuple[str, ...], raw_url: str) -> None:
        url = httpx.URL(raw_url)
        if url.scheme not in {"http", "https"} or url.host is None or url.userinfo:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "browser URL origin is invalid")
        port = url.port or (443 if url.scheme == "https" else 80)
        default = (url.scheme == "https" and port == 443) or (url.scheme == "http" and port == 80)
        origin = f"{url.scheme}://{url.host}" if default else f"{url.scheme}://{url.host}:{port}"
        if not any(fnmatchcase(origin, pattern) for pattern in allowed):
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "browser origin is not allowed")

    @staticmethod
    def _origin(raw_url: str) -> str:
        url = httpx.URL(raw_url)
        if url.scheme not in {"http", "https"} or url.host is None or url.userinfo:
            raise MishkanError(ErrorCode.AUTHORITY_NOT_GRANTED, "browser URL origin is invalid")
        port = url.port or (443 if url.scheme == "https" else 80)
        default = (url.scheme == "https" and port == 443) or (url.scheme == "http" and port == 80)
        return f"{url.scheme}://{url.host}" if default else f"{url.scheme}://{url.host}:{port}"

    @staticmethod
    def _safe_url(raw_url: str) -> str:
        try:
            url = httpx.URL(raw_url)
            if url.scheme not in {"http", "https", "ws", "wss"} or url.host is None:
                return "[INVALID_URL]"
            return str(url.copy_with(query=None, fragment=None, userinfo=b""))
        except (TypeError, ValueError):
            return "[INVALID_URL]"


class LazyPlaywrightChromiumDriver:
    """Start Playwright only when an accepted Browser call reaches dispatch."""

    adapter_id = PlaywrightChromiumDriver.adapter_id

    def __init__(
        self,
        network_profiles: Mapping[str, NetworkProfileConfig],
        *,
        max_diagnostic_entries: int,
        max_pending_downloads: int,
        resolver: Resolver | None = None,
    ) -> None:
        self._network_profiles = dict(network_profiles)
        self._resolver = resolver
        self._max_diagnostic_entries = max_diagnostic_entries
        self._max_pending_downloads = max_pending_downloads
        self._lock = threading.Lock()
        self._driver: PlaywrightChromiumDriver | None = None

    def open(
        self,
        profile: BrowserProfileConfig,
        *,
        workspace: str,
        initial_url: str | None,
    ) -> DriverSession:
        return self._instance().open(
            profile,
            workspace=workspace,
            initial_url=initial_url,
        )

    def observe(
        self,
        handle: str,
        page_id: str,
        *,
        screenshot: bool,
    ) -> DriverObservation:
        return self._instance().observe(handle, page_id, screenshot=screenshot)

    def act(
        self,
        handle: str,
        request: BrowserActionRequest,
        target: BrowserTarget | None,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> DriverActionOutcome:
        return self._instance().act(
            handle,
            request,
            target,
            cancellation_requested=cancellation_requested,
        )

    def diagnostics(
        self,
        handle: str,
        page_id: str,
        channels: tuple[str, ...],
        cursor: int,
        limit: int,
    ) -> DriverDiagnostics:
        return self._instance().diagnostics(handle, page_id, channels, cursor, limit)

    def close(self, handle: str) -> None:
        self._instance().close(handle)

    def shutdown(self) -> None:
        with self._lock:
            driver = self._driver
            self._driver = None
        if driver is not None:
            driver.shutdown()

    @property
    def started(self) -> bool:
        with self._lock:
            return self._driver is not None

    def _instance(self) -> PlaywrightChromiumDriver:
        with self._lock:
            if self._driver is None:
                self._driver = PlaywrightChromiumDriver(
                    self._network_profiles,
                    max_diagnostic_entries=self._max_diagnostic_entries,
                    max_pending_downloads=self._max_pending_downloads,
                    resolver=self._resolver,
                )
            return self._driver


def playwright_chromium_ready() -> bool:
    """Prove that Playwright and its Chromium executable are installed."""
    try:
        with sync_playwright() as engine:
            return Path(engine.chromium.executable_path).is_file()
    except BaseException:
        return False
