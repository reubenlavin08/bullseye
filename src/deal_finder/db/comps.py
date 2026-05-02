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
    """Aggregated stats over a set of comps.

    Two flavors of central tendency are reported:
      * `median` / `mean`         -- across the full sample
      * `trimmed_median` / `trimmed_mean` -- after dropping Tukey-fence
        outliers (values outside Q1 - 1.5*IQR to Q3 + 1.5*IQR)

    The trimmed values are what the appraisal formula uses by default
    because a single $5000 scammer or $20 broken unit can otherwise
    yank the median in a small sample.
    """
    search_term: str
    source: str
    sample_size: int
    median: float | None = None
    mean: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    # Spread / quantiles
    stddev: float | None = None
    p10: float | None = None
    q1: float | None = None
    q3: float | None = None
    p90: float | None = None
    iqr: float | None = None
    # Outlier-trimmed central tendency
    trimmed_sample_size: int | None = None
    trimmed_median: float | None = None
    trimmed_mean: float | None = None
    outliers_dropped: int = 0
    # Provenance
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

    return _compute_stats(
        prices=prices,
        search_term=search_term,
        source=source,
        fetched_at=meta["last_fetched"],
    )


def _compute_stats(
    *,
    prices: list[float],
    search_term: str,
    source: str,
    fetched_at: datetime | None,
) -> CompStats:
    """Build a fully-populated CompStats from a price list.

    Outlier handling: Tukey's fences. For sample sizes >= 4 we drop
    values outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR] and report
    `trimmed_median` / `trimmed_mean` separately. The full-sample
    `median` / `mean` are still reported for transparency.
    """
    n = len(prices)
    sorted_p = sorted(prices)
    median = statistics.median(sorted_p)
    mean = statistics.fmean(sorted_p)
    stdev = statistics.pstdev(sorted_p) if n >= 2 else 0.0

    # statistics.quantiles needs n >= 2 to mean anything; for tiny
    # samples we just skip percentile-based stats.
    q1 = q3 = iqr = p10 = p90 = None
    trimmed_median = median
    trimmed_mean = mean
    trimmed_n = n
    outliers = 0

    if n >= 4:
        # 4-quantile cuts give us Q1, Q2(median), Q3
        try:
            qs = statistics.quantiles(sorted_p, n=4, method="exclusive")
            q1, _, q3 = qs[0], qs[1], qs[2]
        except statistics.StatisticsError:
            q1 = q3 = None

        if q1 is not None and q3 is not None:
            iqr = q3 - q1
            lo = q1 - 1.5 * iqr
            hi = q3 + 1.5 * iqr
            kept = [p for p in sorted_p if lo <= p <= hi]
            outliers = n - len(kept)
            if kept:
                trimmed_median = statistics.median(kept)
                trimmed_mean = statistics.fmean(kept)
                trimmed_n = len(kept)

    if n >= 10:
        try:
            deciles = statistics.quantiles(sorted_p, n=10, method="exclusive")
            p10 = deciles[0]
            p90 = deciles[8]
        except statistics.StatisticsError:
            p10 = p90 = None

    return CompStats(
        search_term=search_term,
        source=source,
        sample_size=n,
        median=median,
        mean=mean,
        minimum=sorted_p[0],
        maximum=sorted_p[-1],
        stddev=stdev,
        p10=p10,
        q1=q1,
        q3=q3,
        p90=p90,
        iqr=iqr,
        trimmed_sample_size=trimmed_n,
        trimmed_median=trimmed_median,
        trimmed_mean=trimmed_mean,
        outliers_dropped=outliers,
        fetched_at=fetched_at,
        fresh=True,
    )


def _seconds_since(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()
