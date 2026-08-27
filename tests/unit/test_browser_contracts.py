from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import AnyHttpUrl, ValidationError

from mishkan.browser import (
    BrowserActionKind,
    BrowserActionRequest,
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
