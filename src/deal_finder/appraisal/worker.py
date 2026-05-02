"""Appraisal worker.

Pulls unappraised, non-rejected listings from Postgres, normalizes each
title, fetches Marketplace comps, scores via the LLM, and writes the
result back. One listing at a time (no concurrency on local LLM).

Bad listings — LLM failures, no comp data even after fallback,
malformed output — are skipped, logged, and re-tried on the next
cycle. We never mark a listing appraised unless we actually got a
score.

Public entry points:
  drain_queue(limit=N)  -- one-shot run; returns counts
  run_forever(...)      -- loop with sleep, used by the scheduler later

Used by `scripts/run_appraisal.py` and (eventually) the APScheduler job.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..comps.marketplace import get_comps
from ..db.connection import get_conn
from ..db.listings import update_appraisal, update_comps_resolution
from ..db.comps import CompStats
from ..scraper.facebook_detail import get_default_client as get_detail_client
from ..scraper.price_extraction import resolve_price
from .formula import compute_score, llm_needed
from .normalizer import (
    DEFAULT_MODEL as NORMALIZER_MODEL,
    extract_price_llm,
    normalize_title,
)
from .ollama_client import get_default_client
from .scorer import DEFAULT_MODEL as SCORER_MODEL
from .scorer import estimate_fair_value

logger = logging.getLogger(__name__)


# Default geographic anchor for comp searches when a listing has no
# coordinates (which is most of them — search results don't carry
# lat/lng). The scheduler will pass per-search anchors later.
DEFAULT_LAT = 49.2827
DEFAULT_LNG = -123.1207
# Comp radius is intentionally HUGE — for transferable goods (electronics,
# scooters, tools, etc.) prices are uniform across western Canada, and a
# wider net means more comps = more robust median = fewer "no data"
# fallbacks. The listing-search radius (in user_searches.radius_km) stays
# tight (40km) because you actually have to drive to those.
#
# 1500 km from Vancouver covers BC + Alberta + most of Saskatchewan +
# Yukon + Washington/Oregon/Idaho. ~10M+ population pool.
#
# Caveat: large items (furniture, vehicles, appliances) won't ship across
# this radius. Comp data for those will still be national-priced but the
# matching is fine — we only use the median, not the actual items.
DEFAULT_RADIUS_KM = 1500


@dataclass
class WorkerStats:
    seen: int = 0
    appraised: int = 0
    skipped_no_score: int = 0
    skipped_bad_listing: int = 0
    elapsed_s: float = 0.0


def warmup_models() -> None:
    """Preload both models into RAM/VRAM. Costs ~30s once; saves cold-start
    on every subsequent listing."""
    cli = get_default_client()
    cli.warmup(NORMALIZER_MODEL)
    cli.warmup(SCORER_MODEL)


def drain_queue(
    *,
    limit: int = 50,
    lat: float = DEFAULT_LAT,
    lng: float = DEFAULT_LNG,
    radius_km: int = DEFAULT_RADIUS_KM,
) -> WorkerStats:
    """Process up to `limit` unappraised listings. Returns counts."""
    stats = WorkerStats()
    t0 = time.perf_counter()

    rows = _fetch_queue(limit)
    stats.seen = len(rows)
    if not rows:
        return stats

    logger.info("appraisal queue: %d listings to process", len(rows))

    for row in rows:
        try:
            ok = _process_one(row, lat=lat, lng=lng, radius_km=radius_km)
        except Exception as e:  # noqa: BLE001
            logger.exception("appraisal failed for %s: %s", row["id"], e)
            stats.skipped_bad_listing += 1
            continue
        if ok:
            stats.appraised += 1
        else:
            stats.skipped_no_score += 1

    stats.elapsed_s = time.perf_counter() - t0
    logger.info(
        "queue drained: appraised=%d skipped_no_score=%d skipped_err=%d in %.1fs",
        stats.appraised, stats.skipped_no_score, stats.skipped_bad_listing,
        stats.elapsed_s,
    )
    return stats


def _recover_price(
    raw_price: float, description: str,
) -> tuple[float, bool]:
    """Recover a real asking price from a placeholder + description.

    Tries regex first (fast, free). If regex doesn't find a price AND the
    raw value is still <= $1, falls back to the small LLM (slower, more
    robust to weird formats: 'two hundred', '$2k', 'asking 1.5K').

    Returns (resolved_price, extracted_flag). On total failure, returns
    (raw_price, False).
    """
    pr = resolve_price(raw_price, description)
    if pr.extracted:
        return pr.price, True

    # Regex missed and asking is still placeholder-low — try the LLM.
    if raw_price <= 1.0 and description:
        llm_price = extract_price_llm(description)
        if llm_price is not None:
            logger.debug(
                "LLM rescued price from description: $%.2f", llm_price,
            )
            return llm_price, True

    return pr.price, False


def _fetch_queue(limit: int) -> list[dict]:
    """Pull the next batch of unappraised, non-rejected listings."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, price, raw_price,
                          price_extracted_from_description,
                          description, seller_location
                   FROM listings
                   WHERE appraised = FALSE AND rejected = FALSE
                   ORDER BY scraped_at ASC
                   LIMIT %s""",
                (limit,),
            )
            return [
                {
                    "id": r[0], "title": r[1], "price": r[2],
                    "raw_price": r[3], "price_extracted": r[4],
                    "description": r[5], "seller_location": r[6],
                }
                for r in cur.fetchall()
            ]


def _process_one(
    row: dict,
    *,
    lat: float, lng: float, radius_km: int,
) -> bool:
    """Score one listing. Returns True iff we wrote an appraisal."""
    listing_id = row["id"]
    title = row["title"] or ""
    asking = float(row["price"] or 0.0)
    description = row.get("description") or ""
    raw_price = float(row.get("raw_price") or asking or 0.0)
    price_extracted = bool(row.get("price_extracted"))

    # If asking is a $0/$1 placeholder and we have no description yet,
    # do a just-in-time detail fetch so the price extractor has something
    # to look at. Without this, the LLM scores the listing based on
    # asking=$0 vs median=$X and (correctly!) rates it 100/100, which
    # is misleading — the real price is hiding in the description.
    if asking <= 1.0 and not description:
        logger.info(
            "%s: placeholder price + no description; fetching detail JIT",
            listing_id,
        )
        detail = get_detail_client().fetch(listing_id)
        if detail.description:
            description = detail.description
            asking, price_extracted = _recover_price(raw_price, description)
            # Persist the rescued data so we don't refetch next cycle.
            with get_conn() as conn:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """UPDATE listings SET
                                  description = %s,
                                  detail_source = %s,
                                  price = %s,
                                  price_extracted_from_description = %s
                               WHERE id = %s""",
                            (description, detail.source, asking,
                             price_extracted, listing_id),
                        )

    # 1. Normalize the title to a clean comp-search keyword.
    search_term = normalize_title(title) or title
    logger.debug("normalize %s: %r -> %r", listing_id, title, search_term)

    # 2. Fetch comps (cache-first; refetches on miss).
    comp = get_comps(
        search_term=search_term,
        lat=lat, lng=lng, radius_km=radius_km,
        exclude_listing_id=listing_id,
    )

    # 3. Get LLM fair_value estimate ONLY when comps are too sparse.
    #    With enough comps, the trimmed median (× asking discount) is
    #    the fair value, no LLM needed.
    llm_estimate = None
    note = ""
    model_used = "formula-only"
    if llm_needed(comp):
        llm_estimate = estimate_fair_value(
            title=title,
            asking_price=asking,
            description=description or None,
            location=row.get("seller_location"),
            comp=comp,
            raw_price=raw_price,
            price_extracted=price_extracted,
        )
        if llm_estimate is not None:
            note = llm_estimate.note
            model_used = llm_estimate.model

    # 4. Compute score deterministically from comps + (optional) LLM.
    try:
        breakdown = compute_score(
            asking_price=asking,
            comp=comp,
            fair_value_from_llm=(
                llm_estimate.fair_value if llm_estimate else None
            ),
        )
    except ValueError as e:
        logger.warning("formula failed for %s: %s", listing_id, e)
        return False

    # 5. Persist breakdown + comp resolution + appraisal in one txn.
    annotated_note = note
    if breakdown.confidence_label:
        annotated_note = (
            f"[{breakdown.confidence_label} ±{breakdown.confidence_pm}] "
            f"{note}".strip()
        )

    with get_conn() as conn:
        with conn:
            update_comps_resolution(
                conn, listing_id,
                search_term=comp.search_term,
                source=comp.source,
                median=comp.median,
                mean=comp.mean,
                minimum=comp.minimum,
                maximum=comp.maximum,
                sample_size=comp.sample_size,
            )
            update_appraisal(
                conn, listing_id,
                deal_score=breakdown.deal_score,
                fair_value=breakdown.fair_value,
                appraisal_note=annotated_note,
                appraisal_model=model_used,
                breakdown=breakdown,
            )

    logger.info(
        "%s | score=%d conf=%s±%d ratio=%.2f n=%d trimmed=%d "
        "fair=$%.0f source=%s | %s",
        listing_id, breakdown.deal_score,
        breakdown.confidence_label, breakdown.confidence_pm,
        breakdown.ratio, comp.sample_size, breakdown.outliers_dropped,
        breakdown.fair_value, breakdown.fair_value_source,
        title[:60],
    )
    return True


def run_forever(
    *,
    batch_limit: int = 25,
    sleep_between_s: int = 60,
    **kwargs,
) -> None:
    """Loop, draining the queue and sleeping when empty. Used by the
    scheduler. Ctrl-C to stop."""
    warmup_models()
    while True:
        stats = drain_queue(limit=batch_limit, **kwargs)
        if stats.seen == 0:
            time.sleep(sleep_between_s)
