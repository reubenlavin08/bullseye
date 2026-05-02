"""Scheduler jobs.

Two jobs run on a timer:

  poll_search(search_id)
      Hits Marketplace for one saved search. For every listing ID we
      haven't seen before, runs the FULL pipeline inline (detail fetch
      + price extract + reject + persist + comp lookup + score). This
      is the "tight polling" pattern — new listings get scored within
      seconds of being detected, not minutes.

  drain_appraisal_safety_net()
      Picks up any listings that somehow ended up persisted but not
      appraised (e.g. crash mid-job, manual DB edits). Cheap to run;
      a no-op when the queue is empty.

Per-listing processing happens INSIDE poll_search rather than via a
separate worker queue. For a single-machine setup with one user, this
is simpler and faster than a producer/consumer split.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..appraisal.condition_signals import extract_condition_signals
from ..appraisal.formula import compute_score
from ..appraisal.normalizer import normalize_title
from ..appraisal.worker import _recover_price, drain_queue
from ..comps.marketplace import get_comps
from ..db.connection import get_conn
from ..db.listings import (
    existing_ids,
    update_appraisal,
    update_comps_resolution,
    upsert_processed,
)
from ..scraper.facebook import (
    SearchListing,
    SearchParams,
    get_default_client as get_search_client,
)
from ..scraper.facebook_detail import (
    get_default_client as get_detail_client,
)
from ..scraper.pipeline import _combine

logger = logging.getLogger(__name__)


@dataclass
class PollResult:
    """One scrape-poll outcome."""
    search_id: int
    keyword: str
    raw_count: int           # listings returned by FB
    new_count: int           # not yet in our DB
    appraised_count: int     # successfully scored
    rejected_count: int      # dropped by rejection filter
    elapsed_s: float


def poll_search(search_id: int) -> PollResult:
    """Run one scrape cycle for a saved search and process any new listings.

    Designed to be safe to call repeatedly; deduplication via the DB
    keeps work to listings we've never seen before. If the search row
    has been disabled or deleted between scheduler firings, this is a
    cheap no-op.
    """
    t0 = time.perf_counter()
    search = _load_search(search_id)
    if search is None:
        logger.info("poll_search %d: search no longer active, skipping", search_id)
        return PollResult(search_id, "", 0, 0, 0, 0, 0.0)

    keyword = search["keyword"]

    # 1) Hit Marketplace
    page = get_search_client().search(SearchParams(
        keyword=keyword,
        lat=search["latitude"],
        lng=search["longitude"],
        radius_km=search["radius_km"],
        price_min=search["price_min"],
        price_max=search["price_max"],
    ))

    # 2) Filter out already-seen listing IDs (cheap PK lookup)
    raw_ids = [sl.id for sl in page.listings]
    if not raw_ids:
        return PollResult(search_id, keyword, 0, 0, 0, 0, time.perf_counter() - t0)

    with get_conn() as conn:
        seen = existing_ids(conn, raw_ids)

    new_listings = [sl for sl in page.listings if sl.id not in seen]
    if not new_listings:
        return PollResult(
            search_id, keyword, len(page.listings), 0, 0, 0,
            time.perf_counter() - t0,
        )

    logger.info(
        "poll_search %d (%r): %d new of %d returned",
        search_id, keyword, len(new_listings), len(page.listings),
    )

    # 3) For each new listing: full pipeline inline
    appraised = 0
    rejected = 0
    for sl in new_listings:
        try:
            outcome = _process_new_listing(sl, search_id=search_id)
        except Exception as e:  # noqa: BLE001 — never let one bad listing kill the loop
            logger.exception("processing %s failed: %s", sl.id, e)
            continue
        if outcome == "appraised":
            appraised += 1
        elif outcome == "rejected":
            rejected += 1

    return PollResult(
        search_id, keyword, len(page.listings), len(new_listings),
        appraised, rejected, time.perf_counter() - t0,
    )


def _load_search(search_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT keyword, latitude, longitude, radius_km,
                          price_min, price_max
                   FROM user_searches
                   WHERE id = %s AND active = TRUE""",
                (search_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "keyword": row[0], "latitude": row[1], "longitude": row[2],
        "radius_km": row[3], "price_min": row[4], "price_max": row[5],
    }


def _process_new_listing(
    sl: SearchListing, *, search_id: int,
) -> str:
    """Run the full per-listing pipeline. Returns one of:
      "rejected"  — listing was filtered out
      "appraised" — listing was scored and persisted
      "unscoreable" — persisted but no score (insufficient comps)
      "skipped" — pipeline bailed out for some other reason
    """
    # Detail fetch (description, seller, photos)
    detail = get_detail_client().fetch(sl.id)
    pl = _combine(sl, detail)

    # Persist regardless — even rejected rows go in the DB so we don't
    # re-fetch them next poll cycle.
    with get_conn() as conn:
        with conn:
            upsert_processed(conn, pl, search_id=search_id)

    if pl.rejected:
        logger.info(
            "%s rejected: %s | %s",
            sl.id, pl.rejection_reason, pl.title[:60],
        )
        return "rejected"

    # If we couldn't get a description, the appraisal will mostly fail
    # for $0/$1 listings (no price recovery). Still try.
    description = pl.description or ""
    asking = pl.resolved_price
    raw_price = pl.raw_price
    price_extracted = pl.price_extracted_from_description

    # JIT recovery for placeholder prices (mirrors worker._process_one)
    if asking <= 1.0 and description:
        asking, price_extracted = _recover_price(raw_price, description)
        if asking != pl.resolved_price:
            with get_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE listings SET price = %s,
                                  price_extracted_from_description = %s
                               WHERE id = %s""",
                            (asking, price_extracted, sl.id),
                        )

    # If asking is still <= 0 after price recovery attempts, we can't
    # score it — the formula needs a positive asking price. Persist as
    # unscoreable rather than crashing the loop.
    if asking <= 0:
        logger.info(
            "%s unscoreable: no recoverable asking price | %s",
            sl.id, pl.title[:60],
        )
        with get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """UPDATE listings SET
                              appraised = TRUE,
                              appraised_at = NOW(),
                              appraisal_note = '[unscoreable] no recoverable asking price'
                           WHERE id = %s""",
                        (sl.id,),
                    )
        return "unscoreable"

    # Comps + condition signals + score
    search_term = normalize_title(pl.title) or pl.title
    comp = get_comps(
        search_term=search_term,
        lat=49.2827, lng=-123.1207, radius_km=1500,  # comp radius is wide
        exclude_listing_id=sl.id,
        asking_price=asking,
        category_id=pl.category_id,
    )
    cond = extract_condition_signals(description)
    breakdown = compute_score(
        asking_price=asking,
        comp=comp,
        condition_adjustment=cond.score_adjustment,
        condition_flags=cond.flags_fired,
        condition_note=cond.note,
        category_id=pl.category_id,
    )

    note = (
        f"[unscoreable] {breakdown.unscoreable_reason}"
        if breakdown.unscoreable
        else f"[{breakdown.confidence_label} ±{breakdown.confidence_pm}]"
    )

    with get_conn() as conn:
        with conn:
            update_comps_resolution(
                conn, sl.id,
                search_term=comp.search_term, source=comp.source,
                median=comp.median, mean=comp.mean,
                minimum=comp.minimum, maximum=comp.maximum,
                sample_size=comp.sample_size,
            )
            update_appraisal(
                conn, sl.id,
                deal_score=breakdown.deal_score,
                fair_value=breakdown.fair_value,
                appraisal_note=note,
                appraisal_model="formula-only",
                breakdown=breakdown,
            )

    if breakdown.unscoreable:
        logger.info("%s unscoreable: %s | %s",
                    sl.id, breakdown.unscoreable_reason, pl.title[:60])
        return "unscoreable"

    logger.info(
        "%s SCORED %d (conf %s±%d, n=%d) | %s",
        sl.id, breakdown.deal_score, breakdown.confidence_label,
        breakdown.confidence_pm, comp.sample_size, pl.title[:60],
    )
    return "appraised"


def drain_appraisal_safety_net() -> None:
    """Backup pass — picks up any listings that ended up persisted but
    unappraised (worker crashes, manual DB edits). Tight polling
    appraises listings inline so this should usually be a no-op."""
    stats = drain_queue(limit=20)
    if stats.seen > 0:
        logger.info(
            "safety net drained %d (appraised=%d, skipped=%d)",
            stats.seen, stats.appraised, stats.skipped_no_score,
        )


def list_active_search_ids() -> list[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM user_searches WHERE active = TRUE ORDER BY id",
            )
            return [r[0] for r in cur.fetchall()]
