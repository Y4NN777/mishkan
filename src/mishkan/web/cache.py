"""Persistent Web cache with explicit freshness and no hidden network fallback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, delete, event, select
from sqlalchemy.orm import Session

from mishkan.domain.time import require_aware, utc_now
from mishkan.persistence.migration import SchemaManager
from mishkan.persistence.sqlite import LocalRunRepository, WebCacheRow
from mishkan.web.models import CacheDisposition


@dataclass(frozen=True, slots=True)
class CacheHit:
    payload: str
    disposition: CacheDisposition
    stored_at: datetime
    fresh_until: datetime


class SQLiteWebCache:
    def __init__(self, database: Path) -> None:
        SchemaManager(database).require_current()
        self._engine = create_engine(f"sqlite:///{database.resolve()}")
        event.listen(self._engine, "connect", LocalRunRepository._configure_connection)

    def get(
        self,
        key: str,
        *,
        kind: str,
        allow_stale_seconds: int,
        now: datetime | None = None,
    ) -> CacheHit | None:
        observed_at = require_aware(now) if now is not None else utc_now()
        with Session(self._engine) as session:
            row = session.get(WebCacheRow, key)
            if row is None or row.kind != kind:
                return None
            stored_at = datetime.fromisoformat(row.stored_at)
            fresh_until = datetime.fromisoformat(row.fresh_until)
            if observed_at <= fresh_until:
                disposition = CacheDisposition.FRESH
            elif observed_at <= fresh_until + timedelta(seconds=allow_stale_seconds):
                disposition = CacheDisposition.STALE
            else:
                return None
            return CacheHit(row.payload, disposition, stored_at, fresh_until)

    def put(
        self,
        key: str,
        *,
        kind: str,
        payload: str,
        ttl_seconds: int,
        now: datetime | None = None,
    ) -> None:
        stored_at = require_aware(now) if now is not None else utc_now()
        fresh_until = stored_at + timedelta(seconds=ttl_seconds)
        with Session(self._engine) as session, session.begin():
            row = session.get(WebCacheRow, key)
            if row is None:
                session.add(
                    WebCacheRow(
                        key=key,
                        kind=kind,
                        payload=payload,
                        stored_at=stored_at.isoformat(),
                        fresh_until=fresh_until.isoformat(),
                    )
                )
            else:
                row.kind = kind
                row.payload = payload
                row.stored_at = stored_at.isoformat()
                row.fresh_until = fresh_until.isoformat()

    def delete(self, key: str) -> None:
        with Session(self._engine) as session, session.begin():
            session.execute(delete(WebCacheRow).where(WebCacheRow.key == key))

    def prune(self, *, before: datetime) -> int:
        cutoff = require_aware(before).isoformat()
        with Session(self._engine) as session, session.begin():
            keys = tuple(
                session.scalars(
                    select(WebCacheRow.key).where(WebCacheRow.fresh_until < cutoff)
                ).all()
            )
            if keys:
                session.execute(delete(WebCacheRow).where(WebCacheRow.key.in_(keys)))
            return len(keys)
