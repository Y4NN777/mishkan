"""Unambiguous time creation and explicit timezone rendering."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now() -> datetime:
    """Return an aware UTC instant."""

    return datetime.now(UTC)


def require_aware(value: datetime) -> datetime:
    """Normalize an aware datetime to UTC and reject ambiguous naive values."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return value.astimezone(UTC)


def validate_timezone(name: str) -> str:
    """Validate an IANA timezone name while preserving its configured spelling."""

    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {name}") from exc
    return name


def render_timestamp(value: datetime, timezone: str) -> str:
    """Render an instant with both its numeric offset and applied IANA timezone."""

    zone = ZoneInfo(validate_timezone(timezone))
    rendered = require_aware(value).astimezone(zone).isoformat()
    return f"{rendered} [{timezone}]"
