"""Scheduler observability events — one row per noteworthy thing.

Used by the dashboard to render aggregates ("polls/hr", "rate-limits
today") and a live tail. Schema: db/schema.sql::scheduler_events.

Design choices:

  - Writes are best-effort. If Postgres is briefly unreachable, we log
    a warning and move on rather than crashing the scheduler. Losing
    one event row is far less bad than crashing a poll cycle.

  - `detail` is JSONB so we don't have to predict every possible field
    up front. The dashboard knows which keys to expect per event_type.

  - There's no helper-per-event-type. Callers pass the type string
    explicitly so it's grep-able and we don't have to maintain a
    parallel API per event class.

  - Cleanup is intentionally NOT scheduled. The user explicitly asked
    to skip the 14-day pruning until the table actually becomes
    annoying. When that day comes, just add a periodic job.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .connection import get_conn

logger = logging.getLogger(__name__)


def record_event(
    event_type: str,
    *,
    search_id: int | None = None,
    duration_ms: int | None = None,
    **detail: Any,
) -> None:
    """Persist one observability event. Never raises.

    Examples:
        record_event("poll", search_id=12, duration_ms=850,
                     raw_count=15, new_count=2, appraised_count=1,
                     rejected_count=1)
        record_event("fb_rate_limit", search_id=12,
                     code=1675004, keyword="scanner")
        record_event("email_sent", recipient="x@y.com",
                     backend="resend", subject="bullseye: 3 deals")
    """
    payload = json.dumps(detail) if detail else None
    try:
        with get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO scheduler_events
                           (event_type, search_id, duration_ms, detail)
                           VALUES (%s, %s, %s, %s::jsonb)""",
                        (event_type, search_id, duration_ms, payload),
                    )
    except Exception as e:  # noqa: BLE001 — never crash the caller
        logger.warning("record_event(%s) failed: %s", event_type, e)
