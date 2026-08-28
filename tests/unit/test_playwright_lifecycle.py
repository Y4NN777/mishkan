from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import Error as PlaywrightError
from pydantic import AnyHttpUrl

from mishkan.browser.playwright import PlaywrightChromiumDriver, _LiveSession
from mishkan.config.models import BrowserProfileConfig, BrowserProfileKind


def _profile(kind: BrowserProfileKind) -> BrowserProfileConfig:
    return BrowserProfileConfig(
        kind=kind,
        adapter="playwright.chromium",
        engine="chromium",
        network_profile="public-read",
        allowed_origins=("*",),
        sensitivity="confidential",
        retention="session",
        headless=True,
        max_pages=4,
        max_download_bytes=1_000_000,
        action_timeout_seconds=10,
        navigation_timeout_seconds=20,
        cdp_endpoint=(
            AnyHttpUrl("http://127.0.0.1:9222")
            if kind is BrowserProfileKind.ATTACHED_EXISTING
            else None
        ),
    )


def test_attached_browser_disposal_detaches_without_closing_external_state() -> None:
    context = Mock()
    browser = Mock()
    live = _LiveSession(
        profile=_profile(BrowserProfileKind.ATTACHED_EXISTING),
        workspace=Path("/tmp"),
        browser=browser,
        context=context,
    )

    PlaywrightChromiumDriver._dispose(live)

    context.close.assert_not_called()
    browser.close.assert_not_called()


def test_owned_browser_disposal_closes_context_and_browser() -> None:
    context = Mock()
    browser = Mock()
    browser.is_connected.return_value = True
    live = _LiveSession(
        profile=_profile(BrowserProfileKind.ISOLATED),
        workspace=Path("/tmp"),
        browser=browser,
        context=context,
    )

    PlaywrightChromiumDriver._dispose(live)

    context.close.assert_called_once_with()
    browser.close.assert_called_once_with()


def test_transient_chromium_screenshot_refusal_is_retried_once() -> None:
    page = Mock()
    page.screenshot.side_effect = [
        PlaywrightError("Protocol error (Page.captureScreenshot): Unable to capture screenshot"),
        b"png",
    ]

    assert PlaywrightChromiumDriver._capture_screenshot(page) == b"png"
    assert page.screenshot.call_count == 2


def test_non_transient_screenshot_error_is_not_retried() -> None:
    page = Mock()
    page.screenshot.side_effect = PlaywrightError("Target page has been closed")

    try:
        PlaywrightChromiumDriver._capture_screenshot(page)
    except PlaywrightError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("non-transient screenshot error was hidden")
    page.screenshot.assert_called_once_with(type="png")
