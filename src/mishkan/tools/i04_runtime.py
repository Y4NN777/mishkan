"""Truthful assembly of I04 capability adapters for the production Gateway."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mishkan.artifacts.service import DurableArtifactService
from mishkan.browser import (
    BrowserSupervisor,
    LazyPlaywrightChromiumDriver,
    build_browser_tool_adapters,
)
from mishkan.config.models import MishkanConfig
from mishkan.domain.errors import ErrorCode, MishkanError
from mishkan.tools.adapters import CapabilityAdapter
from mishkan.tools.inspection import ContentInspector
from mishkan.web.adapters import (
    BraveSearchAdapter,
    ExtractionAdapter,
    SearchAdapter,
    SearxngSearchAdapter,
    TrafilaturaExtractionAdapter,
)
from mishkan.web.cache import SQLiteWebCache
from mishkan.web.network import HttpxWebTransport
from mishkan.web.service import WebService
from mishkan.web.tools import build_web_tool_adapters


@dataclass(slots=True)
class I04CapabilityRuntime:
    adapters: dict[str, CapabilityAdapter]
    dependencies: frozenset[str]
    _browser: LazyPlaywrightChromiumDriver | None = None

    @property
    def adapter_ids(self) -> frozenset[str]:
        return frozenset(self.adapters)

    @property
    def browser_started(self) -> bool:
        return self._browser.started if self._browser is not None else False

    def close(self) -> None:
        if self._browser is not None:
            self._browser.shutdown()


def build_i04_capability_runtime(
    config: MishkanConfig,
    database: Path,
    workspace: Path,
    artifacts: DurableArtifactService,
    inspector: ContentInspector,
) -> I04CapabilityRuntime:
    """Build configured adapters without performing network or browser effects."""
    web_config = config.web
    browser_config = config.browser
    if web_config is None or browser_config is None:
        raise MishkanError(ErrorCode.CONFIGURATION, "I04 capability configuration is incomplete")

    transport = HttpxWebTransport()
    search_adapters: dict[str, SearchAdapter] = {}
    for source in web_config.sources.values():
        if source.adapter == BraveSearchAdapter.adapter_id:
            search_adapters.setdefault(source.adapter, BraveSearchAdapter(transport))
        elif source.adapter == SearxngSearchAdapter.adapter_id:
            search_adapters.setdefault(source.adapter, SearxngSearchAdapter(transport))

    extraction_adapters: dict[str, ExtractionAdapter] = {}
    for extractor in web_config.extractors.values():
        if extractor.adapter == TrafilaturaExtractionAdapter.adapter_id:
            extraction_adapters.setdefault(
                extractor.adapter,
                TrafilaturaExtractionAdapter(),
            )

    web_service = WebService(
        web_config,
        artifacts,
        search_adapters=search_adapters,
        extraction_adapters=extraction_adapters,
        transport=transport,
        cache=SQLiteWebCache(database),
    )
    adapters = build_web_tool_adapters(web_config, web_service)

    configured_drivers = {profile.adapter for profile in browser_config.profiles.values()}
    browser: LazyPlaywrightChromiumDriver | None = None
    if LazyPlaywrightChromiumDriver.adapter_id in configured_drivers:
        browser = LazyPlaywrightChromiumDriver(web_config.network_profiles)
        supervisor = BrowserSupervisor(
            database,
            workspace,
            browser_config,
            artifacts,
            {browser.adapter_id: browser},
            inspector,
        )
        adapters.update(build_browser_tool_adapters(supervisor))
    dependencies = {"trafilatura"} if extraction_adapters else set()
    if browser is not None:
        dependencies.add("playwright")
    return I04CapabilityRuntime(adapters, frozenset(dependencies), browser)
