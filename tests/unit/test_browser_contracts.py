from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import AnyHttpUrl, ValidationError

from mishkan.browser import (
    BrowserActionKind,
    BrowserActionRequest,
    BrowserDiagnosticRequest,
    BrowserObservation,
    BrowserTarget,
)
from mishkan.domain.identity import new_id
from mishkan.domain.time import utc_now


def test_element_action_requires_an_observed_target_reference() -> None:
    with pytest.raises(ValidationError, match="observation target"):
        BrowserActionRequest(
            session_id=new_id(),
            page_id="page-1",
            observation_id=new_id(),
            kind=BrowserActionKind.CLICK,
            resolved_effect="ui.navigation",
            expected_session_revision=1,
        )


def test_navigation_is_explicitly_allowed_without_an_element_target() -> None:
    request = BrowserActionRequest(
        session_id=new_id(),
        page_id="page-1",
        observation_id=new_id(),
        kind=BrowserActionKind.NAVIGATE,
        value="https://example.com",
        resolved_effect="navigation",
        expected_session_revision=1,
    )

    assert request.target_reference is None


def test_coordinate_fallback_requires_bounded_coordinates_and_visual_evidence() -> None:
    with pytest.raises(ValidationError, match="coordinates and visual evidence"):
        BrowserActionRequest(
            session_id=new_id(),
            page_id="page-1",
            observation_id=new_id(),
            kind=BrowserActionKind.COORDINATE_CLICK,
            coordinates=(12, 24),
            resolved_effect="ui.interaction",
            expected_session_revision=1,
        )

    request = BrowserActionRequest(
        session_id=new_id(),
        page_id="page-1",
        observation_id=new_id(),
        kind=BrowserActionKind.COORDINATE_CLICK,
        coordinates=(12, 24),
        visual_evidence_artifact_reference="artifact:" + str(new_id()),
        resolved_effect="ui.interaction",
        expected_session_revision=1,
    )

    assert request.target_reference is None


def test_browser_credential_requires_its_exact_origin() -> None:
    with pytest.raises(ValidationError, match="exact authorized origin"):
        BrowserActionRequest(
            session_id=new_id(),
            page_id="page-1",
            observation_id=new_id(),
            target_reference="textbox:password",
            kind=BrowserActionKind.FILL,
            credential_reference="project.login",
            resolved_effect="form.field.update",
            expected_session_revision=1,
        )


def test_observation_rejects_duplicate_target_references() -> None:
    now = utc_now()
    target = BrowserTarget(
        reference="button:save",
        role="button",
        name="Save",
        element_revision="sha256:" + "a" * 64,
    )

    with pytest.raises(ValidationError, match="must be unique"):
        BrowserObservation(
            session_id=new_id(),
            page_id="page-1",
            session_revision=1,
            url=AnyHttpUrl("https://example.com"),
            title="Example",
            targets=(target, target),
            tree_artifact_reference="artifact:" + str(new_id()),
            engine="chromium",
            engine_version="fixture",
            created_at=now,
            expires_at=now + timedelta(seconds=30),
        )


def test_diagnostic_channels_are_typed_and_unique() -> None:
    with pytest.raises(ValidationError):
        BrowserDiagnosticRequest(
            session_id=new_id(),
            page_id="page-1",
            channels=("arbitrary",),
        )
    with pytest.raises(ValidationError, match="must be unique"):
        BrowserDiagnosticRequest(
            session_id=new_id(),
            page_id="page-1",
            channels=("network", "network"),
        )
