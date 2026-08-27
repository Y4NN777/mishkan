from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import pytest

from mishkan.browser import BrowserActionKind, BrowserActionRequest, PlaywrightChromiumDriver
from mishkan.config.models import BrowserProfileConfig, BrowserProfileKind, NetworkProfileConfig


class _PageHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b"""<!doctype html><html><head><title>MISHKAN Browser Gate</title></head>
        <body>
        <button type=\"button\" onclick=\"this.textContent='Saved'\">Save</button>
        <input aria-label=\"Email\">
        <select aria-label=\"Role\"><option value=\"user\">User</option>
          <option value=\"admin\">Admin</option></select>
        <input type=\"checkbox\" aria-label=\"Accept\">
        <input type=\"file\" aria-label=\"Attachment\">
        </body>
        </html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@pytest.mark.acceptance
@pytest.mark.browser
def test_real_playwright_observation_action_and_cdp_diagnostics(tmp_path: Path) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _PageHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    network = NetworkProfileConfig(
        allowed_schemes=("http",),
        allowed_ports=(port,),
        allow_public=False,
        allow_private=False,
        allow_loopback=True,
        allow_link_local=False,
        allow_multicast=False,
        max_redirects=2,
        connect_timeout_seconds=5,
        read_timeout_seconds=10,
        max_response_bytes=1_000_000,
        max_decompressed_bytes=2_000_000,
        max_concurrency=4,
        credential_header_names=("authorization", "cookie"),
    )
    profile = BrowserProfileConfig(
        kind="isolated",
        adapter="playwright.chromium",
        engine="chromium",
        network_profile="gate",
        allowed_origins=(f"http://127.0.0.1:{port}",),
        sensitivity="internal",
        retention="run",
        headless=True,
        max_pages=2,
        action_timeout_seconds=10,
        navigation_timeout_seconds=10,
    )
    driver = PlaywrightChromiumDriver({"gate": network})
    try:
        opened = driver.open(
            profile,
            workspace=str(tmp_path),
            initial_url=f"http://127.0.0.1:{port}/",
        )
        page_id = opened.page_ids[0]
        observed = driver.observe(opened.handle, page_id, screenshot=True)
        target = next(item for item in observed.targets if item.name == "Save")

        def act(
            target_name: str,
            kind: BrowserActionKind,
            value: object,
            effect: str,
        ):
            current = driver.observe(opened.handle, page_id, screenshot=False)
            current_target = next(item for item in current.targets if item.name == target_name)
            return driver.act(
                opened.handle,
                BrowserActionRequest(
                    session_id=uuid4(),
                    page_id=page_id,
                    observation_id=uuid4(),
                    target_reference=current_target.reference,
                    kind=kind,
                    value=value,
                    resolved_effect=effect,
                    expected_session_revision=1,
                ),
                current_target,
            )

        outcome = driver.act(
            opened.handle,
            BrowserActionRequest(
                session_id=uuid4(),
                page_id=page_id,
                observation_id=uuid4(),
                target_reference=target.reference,
                kind=BrowserActionKind.CLICK,
                resolved_effect="ui.interaction",
                expected_session_revision=1,
            ),
            target,
        )
        act("Email", BrowserActionKind.FILL, "engineer@example.com", "form.field.update")
        act("Email", BrowserActionKind.PRESS, "End", "form.field.update")
        act("Role", BrowserActionKind.SELECT, ["admin"], "form.field.update")
        act("Accept", BrowserActionKind.CHECK, True, "form.field.update")
        upload = tmp_path / "proof.txt"
        upload.write_text("artifact proof")
        act("Attachment", BrowserActionKind.UPLOAD, ["proof.txt"], "file.upload")
        act(
            "Saved",
            BrowserActionKind.JAVASCRIPT,
            "element => element.setAttribute('data-proof', 'yes')",
            "ui.interaction",
        )
        driver.act(
            opened.handle,
            BrowserActionRequest(
                session_id=uuid4(),
                page_id=page_id,
                observation_id=uuid4(),
                kind=BrowserActionKind.NAVIGATE,
                value=f"http://127.0.0.1:{port}/again",
                resolved_effect="navigation",
                expected_session_revision=1,
            ),
            None,
        )
        after = driver.observe(opened.handle, page_id, screenshot=False)
        diagnostics = driver.diagnostics(
            opened.handle,
            page_id,
            ("network", "performance", "storage", "service_worker"),
            0,
            100,
        )

        assert outcome.page_ids == (page_id,)
        assert b"Save" in after.tree
        assert observed.screenshot is not None
        assert diagnostics.next_cursor > 0
        assert {item["channel"] for item in diagnostics.entries} >= {
            "network",
            "performance",
            "storage",
            "service_worker",
        }
        driver.close(opened.handle)

        persistent = profile.model_copy(
            update={
                "kind": BrowserProfileKind.PROJECT_PERSISTENT,
                "user_data_dir": Path("browser-profile"),
            }
        )
        persisted = driver.open(persistent, workspace=str(tmp_path), initial_url=None)
        assert persisted.page_ids
        driver.close(persisted.handle)
    finally:
        driver.shutdown()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
