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
    asking_price: float | None = None,
) -> CompStats:
    """Aggregate fresh observations within the TTL window.

    If `asking_price` is provided, the comps are first checked for
    bimodal price distributions (the "boat motor" problem — comps for
    "boat motor" return a mix of $100 trolling motors and $5000
    outboards, so the median is meaningless). When a clear bimodal
    split is detected, we keep the cluster closest to the asking price
    and aggregate stats over just that cluster. The full original
    sample is still recorded in `sample_size`; the active cluster is
    in `trimmed_sample_size`.
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
        asking_price=asking_price,
    )


def _compute_stats(
    *,
    prices: list[float],
    search_term: str,
    source: str,
    fetched_at: datetime | None,
    asking_price: float | None = None,
) -> CompStats:
    """Build a fully-populated CompStats from a price list.

    Pipeline:
      1. Bimodal cluster detection (if `asking_price` is given) — split
         the comp set on the largest log-gap when one cluster's median
         is more than 2x another's. Keep the cluster nearest the asking
         price.
      2. Tukey-fence outlier trim on the remaining prices for
         `trimmed_*` fields.
      3. Compute median, mean, IQR, percentiles, etc.

    The full pre-cluster `sample_size` is preserved so the user can
    still see "12 comps total, 4 in your price band". `trimmed_sample_size`
    reflects the cluster that was actually used to derive `trimmed_median`.
    """
    n_total = len(prices)
    sorted_p = sorted(prices)

    # Step 1: bimodal split
    cluster, split_info = _maybe_split_bimodal(sorted_p, asking_price)
    n_cluster = len(cluster)

    # Step 2: stats over the active cluster (which may equal the full set)
    median = statistics.median(cluster)
    mean = statistics.fmean(cluster)
    stdev = statistics.pstdev(cluster) if n_cluster >= 2 else 0.0

    q1 = q3 = iqr = p10 = p90 = None
    trimmed_median = median
    trimmed_mean = mean
    trimmed_n = n_cluster
    outliers = split_info["dropped_other_cluster"]

    if n_cluster >= 4:
        try:
            qs = statistics.quantiles(cluster, n=4, method="exclusive")
            q1, _, q3 = qs[0], qs[1], qs[2]
        except statistics.StatisticsError:
            q1 = q3 = None

        if q1 is not None and q3 is not None:
            iqr = q3 - q1
            lo = q1 - 1.5 * iqr
            hi = q3 + 1.5 * iqr
            kept = [p for p in cluster if lo <= p <= hi]
            tukey_outliers = n_cluster - len(kept)
            outliers += tukey_outliers
            if kept:
                trimmed_median = statistics.median(kept)
                trimmed_mean = statistics.fmean(kept)
                trimmed_n = len(kept)

    if n_cluster >= 10:
        try:
            deciles = statistics.quantiles(cluster, n=10, method="exclusive")
            p10 = deciles[0]
            p90 = deciles[8]
        except statistics.StatisticsError:
            p10 = p90 = None

    return CompStats(
        search_term=search_term,
        source=source,
        sample_size=n_total,
        median=median,
        mean=mean,
        minimum=cluster[0],
        maximum=cluster[-1],
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


# --- Bimodal split detection ----------------------------------------------

def _maybe_split_bimodal(
    sorted_prices: list[float], asking_price: float | None,
) -> tuple[list[float], dict]:
    """Detect a bimodal price distribution and pick the cluster closest
    to `asking_price`.

    Heuristic: walk the sorted prices, find the largest *ratio* gap
    between consecutive entries (price[i+1] / price[i]). If that gap
    is large enough AND splits the data into two non-trivial groups
    (each with >=2 entries) AND the medians of the two groups differ
    by more than 2x, treat it as bimodal. Then return the cluster
    whose median is closest to the asking price.

    If no split is triggered (no asking_price, too few comps, gap too
    small, or one cluster too tiny), return the full sorted list.

    Tuned conservatively — false positives (over-splitting) are worse
    than false negatives (missing a real split).
    """
    n = len(sorted_prices)
    info = {
        "split_triggered": False,
        "split_at": None,
        "left_size": 0,
        "right_size": 0,
        "left_median": None,
        "right_median": None,
        "chose": None,
        "dropped_other_cluster": 0,
    }
    if asking_price is None or n < 6:
        return sorted_prices, info

    # Find largest ratio gap. Skip prices <= 0 to avoid div-by-zero.
    best_gap_ratio = 1.0
    best_idx = -1
    for i in range(n - 1):
        if sorted_prices[i] <= 0:
            continue
        ratio = sorted_prices[i + 1] / sorted_prices[i]
        if ratio > best_gap_ratio:
            best_gap_ratio = ratio
            best_idx = i

    # Need both halves >= 3 to consider them meaningful clusters.
    if best_idx < 2 or (n - 1 - best_idx) < 3:
        return sorted_prices, info

    # Need at least 2.5x gap to call it bimodal.
    if best_gap_ratio < 2.5:
        return sorted_prices, info

    left = sorted_prices[: best_idx + 1]
    right = sorted_prices[best_idx + 1:]
    left_med = statistics.median(left)
    right_med = statistics.median(right)

    # Need the two medians to differ by >2x to confirm clusters are
    # genuinely different price tiers.
    if right_med < left_med * 2:
        return sorted_prices, info

    # Pick the cluster whose median is closer to asking (in log space —
    # asking $80 closer to $100 than $400 even though absolute distances
    # are similar).
    import math
    log_ask = math.log(max(asking_price, 1.0))
    log_left = math.log(max(left_med, 1.0))
    log_right = math.log(max(right_med, 1.0))
    chose_left = abs(log_ask - log_left) <= abs(log_ask - log_right)
    chosen = left if chose_left else right
    other_n = len(right) if chose_left else len(left)

    info.update({
        "split_triggered": True,
        "split_at": sorted_prices[best_idx],
        "left_size": len(left),
        "right_size": len(right),
        "left_median": left_med,
        "right_median": right_med,
        "chose": "left" if chose_left else "right",
        "dropped_other_cluster": other_n,
    })
    return chosen, info


def _seconds_since(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()
