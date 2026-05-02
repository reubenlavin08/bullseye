"""Comps cache data-access layer.

Holds price observations for fair-value lookup. Source-agnostic — same
table for current Marketplace asking prices and (eventually) eBay sold
prices. Differentiated by the `source` column.

TTL is enforced by the reader: a "cache hit" is rows fetched within the
last `ttl_seconds`. Older rows aren't deleted (cheap audit trail) — we
just refetch and append, then read the fresh window.
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

import psycopg2.extras

logger = logging.getLogger(__name__)


# --- Public dataclasses ---------------------------------------------------

@dataclass
class CompObservation:
    """One observed price for a search term."""
    price: float
    title: str | None = None
    listing_url: str | None = None
    location: str | None = None


@dataclass
class CompStats:
    """Aggregated stats over a set of comps."""
    search_term: str
    source: str
    sample_size: int
    median: float | None = None
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    fetched_at: datetime | None = None
    fresh: bool = False  # True if within TTL


# --- Inserts --------------------------------------------------------------

def insert_comps(
    conn,
    search_term: str,
    source: str,
    obs: Iterable[CompObservation],
) -> int:
    """Bulk-insert observations + update meta row. Returns count inserted."""
    rows = [
        (search_term, source, o.price, o.title, o.listing_url, o.location)
        for o in obs if o.price is not None and o.price >= 0
    ]
    if not rows:
        return 0

    with conn.cursor() as cur:
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO comps (search_term, source, price, title, "
            "listing_url, location) VALUES %s",
            rows,
        )
        cur.execute(
            """INSERT INTO comps_meta (search_term, source, last_fetched, sample_size)
               VALUES (%s, %s, NOW(), %s)
               ON CONFLICT (search_term, source) DO UPDATE SET
                  last_fetched = EXCLUDED.last_fetched,
                  sample_size = EXCLUDED.sample_size""",
            (search_term, source, len(rows)),
        )
    return len(rows)


# --- Reads ---------------------------------------------------------------

def fetch_stats(
    conn,
    search_term: str,
    source: str,
    *,
    ttl_seconds: int = 12 * 3600,
) -> CompStats:
    """Aggregate fresh observations within the TTL window.

    If the meta row says we last fetched within the TTL, we use those
    rows. Otherwise return CompStats(fresh=False, sample_size=0) so the
    caller knows to refetch from the source.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """SELECT last_fetched, sample_size FROM comps_meta
               WHERE search_term = %s AND source = %s""",
            (search_term, source),
        )
        meta = cur.fetchone()

    if not meta or _seconds_since(meta["last_fetched"]) > ttl_seconds:
        return CompStats(
            search_term=search_term, source=source, sample_size=0, fresh=False,
        )

    with conn.cursor() as cur:
        cur.execute(
            """SELECT price FROM comps
               WHERE search_term = %s
                 AND source = %s
                 AND fetched_at >= NOW() - INTERVAL '%s seconds'""",
            (search_term, source, ttl_seconds),
        )
        prices = [r[0] for r in cur.fetchall() if r[0] is not None]

    if not prices:
        return CompStats(
            search_term=search_term, source=source, sample_size=0,
            fetched_at=meta["last_fetched"], fresh=False,
        )

    return CompStats(
        search_term=search_term,
        source=source,
        sample_size=len(prices),
        median=statistics.median(prices),
        mean=statistics.fmean(prices),
        minimum=min(prices),
        maximum=max(prices),
        fetched_at=meta["last_fetched"],
        fresh=True,
    )


def _seconds_since(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()
