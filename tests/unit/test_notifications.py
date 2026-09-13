from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from mishkan.events import EventEnvelope, EventPage
from mishkan.notifications import (
    NotificationConfig,
    NotificationDelivery,
    NotificationRuleConfig,
    NotificationService,
    NotificationSeverity,
)


def _event(cursor: int, event_type: str, payload: dict[str, object]) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        cursor=cursor,
        event_type=event_type,
        source="test",
        entity_type="mission",
        entity_id="mission-1",
        occurred_at=datetime.now(UTC),
        sensitivity="internal",
        payload=payload,
    )


def test_notifications_are_configured_projections_without_event_loss() -> None:
    escalation = _event(4, "mission.escalation.opened", {"reason": "CEO decision needed"})
    telemetry = _event(5, "telemetry.exported", {})
    page = EventPage(
        after_cursor=3,
        next_cursor=5,
        retained_from_cursor=1,
        events=(escalation, telemetry),
    )
    service = NotificationService(
        NotificationConfig(
            rules=(
                NotificationRuleConfig(
                    rule_id="mission.escalation",
                    event_types=("mission.escalation.*",),
                    sources=("test",),
                    severity=NotificationSeverity.URGENT,
                    delivery=NotificationDelivery.FEED,
                ),
                NotificationRuleConfig(
                    rule_id="telemetry.silent",
                    event_types=("telemetry.*",),
                    severity=NotificationSeverity.INFORMATION,
                    delivery=NotificationDelivery.SILENT,
                ),
            )
        )
    )

    projected = service.project(page)
    urgent = service.project(page, severities=frozenset({NotificationSeverity.URGENT}))

    assert projected.after_cursor == 3
    assert projected.next_cursor == 5
    assert [item.delivery for item in projected.notifications] == [
        NotificationDelivery.FEED,
        NotificationDelivery.SILENT,
    ]
    assert urgent.notifications[0].summary == "CEO decision needed"
    assert urgent.notifications[0].action_reference == f"event:{escalation.event_id}"
    assert len(urgent.notifications) == 1
    assert page.events == (escalation, telemetry)


def test_notification_rules_can_distinguish_domain_and_command_event_sources() -> None:
    domain = _event(1, "mission.escalation_opened", {"reason": "CEO decision needed"})
    command = domain.model_copy(update={"event_id": uuid4(), "cursor": 2, "source": "mishkand"})
    service = NotificationService(
        NotificationConfig(
            rules=(
                NotificationRuleConfig(
                    rule_id="domain-escalation",
                    event_types=("mission.escalation_opened",),
                    sources=("test",),
                    severity=NotificationSeverity.URGENT,
                    delivery=NotificationDelivery.FEED,
                ),
                NotificationRuleConfig(
                    rule_id="command-fact",
                    event_types=("mission.*",),
                    sources=("mishkand",),
                    severity=NotificationSeverity.INFORMATION,
                    delivery=NotificationDelivery.SILENT,
                ),
            )
        )
    )
    page = EventPage(
        after_cursor=0,
        next_cursor=2,
        retained_from_cursor=1,
        events=(domain, command),
    )

    urgent = service.project(page, severities=frozenset({NotificationSeverity.URGENT}))

    assert [item.event_id for item in urgent.notifications] == [domain.event_id]


@pytest.mark.parametrize(
    "config",
    [
        {
            "rules": [
                {
                    "rule_id": "duplicate.rule",
                    "event_types": ["mission.*"],
                    "severity": "attention",
                    "delivery": "feed",
                },
                {
                    "rule_id": "duplicate.rule",
                    "event_types": ["run.*"],
                    "severity": "information",
                    "delivery": "silent",
                },
            ]
        },
        {
            "rules": [
                {
                    "rule_id": "duplicate.pattern",
                    "event_types": ["mission.*", "mission.*"],
                    "severity": "attention",
                    "delivery": "feed",
                }
            ]
        },
    ],
)
def test_notification_configuration_rejects_ambiguous_rules(config: object) -> None:
    with pytest.raises(ValidationError):
        NotificationConfig.model_validate(config)
