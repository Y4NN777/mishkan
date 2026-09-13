"""Config-driven notification classification without hiding source events."""

from __future__ import annotations

from fnmatch import fnmatchcase
from uuid import UUID, uuid5

from mishkan.events import EventPage
from mishkan.notifications.models import (
    NotificationConfig,
    NotificationDelivery,
    NotificationPage,
    NotificationRecord,
    NotificationRuleConfig,
    NotificationSeverity,
)

_NOTIFICATION_NAMESPACE = UUID("a397a1fc-ff31-4fa5-9224-a9a50d9446d4")


class NotificationService:
    def __init__(self, config: NotificationConfig) -> None:
        self._config = config

    def project(
        self,
        events: EventPage,
        *,
        severities: frozenset[NotificationSeverity] = frozenset(),
        deliveries: frozenset[NotificationDelivery] = frozenset(),
    ) -> NotificationPage:
        projected = tuple(self._record(event) for event in events.events)
        filtered = tuple(
            item
            for item in projected
            if (not severities or item.severity in severities)
            and (not deliveries or item.delivery in deliveries)
        )
        return NotificationPage(
            after_cursor=events.after_cursor,
            next_cursor=events.next_cursor,
            retained_from_cursor=events.retained_from_cursor,
            notifications=filtered,
        )

    def _record(self, event: object) -> NotificationRecord:
        from mishkan.events import EventEnvelope

        assert isinstance(event, EventEnvelope)
        rule = self._match(event.event_type)
        severity = rule.severity if rule is not None else self._config.default_severity
        delivery = rule.delivery if rule is not None else self._config.default_delivery
        summary_field = event.payload.get("message") or event.payload.get("reason")
        summary = (
            str(summary_field)
            if summary_field
            else f"{event.event_type} for {event.entity_type}:{event.entity_id}"
        )
        return NotificationRecord(
            notification_id=uuid5(
                _NOTIFICATION_NAMESPACE,
                f"{event.event_id}:{rule.rule_id if rule is not None else 'default'}",
            ),
            event_id=event.event_id,
            event_cursor=event.cursor,
            event_type=event.event_type,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            severity=severity,
            delivery=delivery,
            rule_id=rule.rule_id if rule is not None else None,
            summary=summary,
            action_reference=self._action_reference(event, severity),
        )

    def _match(self, event_type: str) -> NotificationRuleConfig | None:
        return next(
            (
                rule
                for rule in self._config.rules
                if any(fnmatchcase(event_type, pattern) for pattern in rule.event_types)
            ),
            None,
        )

    @staticmethod
    def _action_reference(event: object, severity: NotificationSeverity) -> str | None:
        from mishkan.events import EventEnvelope

        assert isinstance(event, EventEnvelope)
        if severity not in {NotificationSeverity.ACTION_REQUIRED, NotificationSeverity.URGENT}:
            return None
        return f"event:{event.event_id}"
