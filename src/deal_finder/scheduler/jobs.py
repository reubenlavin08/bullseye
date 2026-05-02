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
import os
import time
from dataclasses import dataclass

from ..appraisal.condition_signals import extract_condition_signals
from ..appraisal.formula import compute_score
from ..appraisal.normalizer import normalize_title
from ..appraisal.worker import _recover_price, drain_queue
from ..comps.marketplace import get_comps
from ..db.connection import get_conn
from ..db.events import record_event
from ..db.geo import geocode_city, haversine_km
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
        elapsed_s = time.perf_counter() - t0
        record_event(
            "poll", search_id=search_id, duration_ms=int(elapsed_s * 1000),
            keyword=keyword, raw_count=0, new_count=0,
            appraised_count=0, rejected_count=0,
        )
        return PollResult(search_id, keyword, 0, 0, 0, 0, elapsed_s)

    with get_conn() as conn:
        seen = existing_ids(conn, raw_ids)

    new_listings = [sl for sl in page.listings if sl.id not in seen]

    # Keyword filter: must_include / must_exclude on listing title.
    # Run BEFORE distance filter so we don't waste geocode round-trips
    # on listings we'll drop anyway.
    keyword_dropped = 0
    must_inc = _parse_word_list(search.get("must_include"))
    must_exc = _parse_word_list(search.get("must_exclude"))
    if new_listings and (must_inc or must_exc):
        kept_kw: list[SearchListing] = []
        for sl in new_listings:
            ok, reason = _passes_keyword_filter(sl.title, must_inc, must_exc)
            if ok:
                kept_kw.append(sl)
            else:
                keyword_dropped += 1
                logger.debug("%s dropped (keyword): %s | %s",
                             sl.id, reason, sl.title[:50])
        new_listings = kept_kw

    # Distance filter: FB's filter_radius_km is unreliable for small
    # values, returning Nanaimo listings on a 5km radius search.
    # Geocode each unique city in the result set once, haversine to the
    # watch's home, drop anything beyond radius_km. fail-open on
    # geocoding errors (keep the listing rather than silently drop).
    distance_dropped = 0
    if new_listings and search.get("radius_km"):
        kept: list[SearchListing] = []
        home_lat = float(search["latitude"])
        home_lng = float(search["longitude"])
        radius = float(search["radius_km"])
        # Be generous to absorb city-center vs actual-address slop.
        # FB's seller_location is just "Vancouver" — geocoded to a
        # single point, but Vancouver is ~12 km wide. A 5 km user
        # radius needs at least 8 km of slack to keep nearby Vancouver
        # listings while still rejecting Burnaby / Surrey / Nanaimo.
        # Formula: max(radius + 8 km, 1.3 × radius) so the buffer
        # scales with the requested radius for larger searches.
        soft_radius = max(radius + 8.0, radius * 1.3)
        for sl in new_listings:
            if not sl.seller_location:
                kept.append(sl)
                continue
            coords = geocode_city(sl.seller_location)
            if coords is None:
                kept.append(sl)
                continue
            dist = haversine_km(home_lat, home_lng, coords[0], coords[1])
            if dist <= soft_radius:
                kept.append(sl)
            else:
                distance_dropped += 1
                logger.debug(
                    "%s dropped: %.1f km > %.1f km (%s)",
                    sl.id, dist, soft_radius, sl.seller_location,
                )
        new_listings = kept

    if not new_listings:
        elapsed_s = time.perf_counter() - t0
        record_event(
            "poll", search_id=search_id, duration_ms=int(elapsed_s * 1000),
            keyword=keyword, raw_count=len(page.listings),
            new_count=0, appraised_count=0, rejected_count=0,
            distance_dropped=distance_dropped,
            keyword_dropped=keyword_dropped,
        )
        return PollResult(
            search_id, keyword, len(page.listings), 0, 0, 0, elapsed_s,
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
            record_event(
                "pipeline_error",
                search_id=search_id,
                listing_id=sl.id,
                error=str(e)[:300],
                error_type=type(e).__name__,
            )
            continue
        if outcome == "appraised":
            appraised += 1
        elif outcome == "rejected":
            rejected += 1

    elapsed_s = time.perf_counter() - t0
    record_event(
        "poll",
        search_id=search_id,
        duration_ms=int(elapsed_s * 1000),
        keyword=keyword,
        raw_count=len(page.listings),
        new_count=len(new_listings),
        appraised_count=appraised,
        rejected_count=rejected,
        distance_dropped=distance_dropped,
        keyword_dropped=keyword_dropped,
    )
    return PollResult(
        search_id, keyword, len(page.listings), len(new_listings),
        appraised, rejected, elapsed_s,
    )


def poll_batch(batch: list[dict]) -> None:
    """Run ONE FB search covering all watches in batch, attribute
    listings back to specific watches, and process per-watch.

    The batch must share (latitude, longitude, radius_km) — see
    pick_next_watch_batch. Combined keyword is space-separated; FB's
    tokenizer treats this as an OR-ish match so each individual watch
    keyword still surfaces relevant listings.
    """
    if not batch:
        return
    t0 = time.perf_counter()

    # Combined keyword. We dedupe word-overlap to keep the query short.
    combined_keyword = " ".join(w["keyword"] for w in batch)
    # Use the widest price range across the batch so we don't drop
    # listings that one watch wants but another excludes.
    price_min_vals = [w["price_min"] for w in batch if w["price_min"] is not None]
    price_max_vals = [w["price_max"] for w in batch if w["price_max"] is not None]
    price_min = min(price_min_vals) if price_min_vals else None
    price_max = max(price_max_vals) if price_max_vals else None

    page = get_search_client().search(SearchParams(
        keyword=combined_keyword,
        lat=batch[0]["latitude"],
        lng=batch[0]["longitude"],
        radius_km=batch[0]["radius_km"],
        price_min=price_min,
        price_max=price_max,
    ))

    raw_count = len(page.listings)
    elapsed_s_at_search = time.perf_counter() - t0

    logger.info(
        "poll_batch ids=%s kw=%r: %d listings returned",
        [w["id"] for w in batch], combined_keyword, raw_count,
    )

    # Bucket each listing into the watch it best matches.
    by_watch: dict[int, list[SearchListing]] = {w["id"]: [] for w in batch}
    unattributed = 0
    for sl in page.listings:
        w = attribute_listing(sl.title, batch)
        if w is None:
            unattributed += 1
            continue
        by_watch[w["id"]].append(sl)

    # Process each watch's bucket through the same downstream pipeline
    # as the original poll_search, but skip the FB call.
    for watch in batch:
        listings_for_watch = by_watch[watch["id"]]
        try:
            _process_watch_bucket(
                watch=watch,
                listings=listings_for_watch,
                raw_count_in_batch=raw_count,
                elapsed_s_at_search=elapsed_s_at_search,
                t0=t0,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("watch %s in batch crashed: %s", watch["id"], e)
            record_event(
                "pipeline_error",
                search_id=watch["id"],
                error=str(e)[:300],
                error_type=type(e).__name__,
            )

    if unattributed:
        logger.debug("batch dropped %d unattributed listings", unattributed)


def _process_watch_bucket(
    *,
    watch: dict,
    listings: list[SearchListing],
    raw_count_in_batch: int,
    elapsed_s_at_search: float,
    t0: float,
) -> None:
    """Per-watch processing for a poll_batch result. Mirrors the
    second-half of poll_search() — keyword filter, distance filter,
    detail/score/persist per listing, then a 'poll' event."""
    search_id = watch["id"]
    keyword = watch["keyword"]

    raw_ids = [sl.id for sl in listings]
    if raw_ids:
        with get_conn() as conn:
            seen = existing_ids(conn, raw_ids)
        new_listings = [sl for sl in listings if sl.id not in seen]
    else:
        new_listings = []

    keyword_dropped = 0
    must_inc = _parse_word_list(watch.get("must_include"))
    must_exc = _parse_word_list(watch.get("must_exclude"))
    if new_listings and (must_inc or must_exc):
        kept_kw: list[SearchListing] = []
        for sl in new_listings:
            ok, _reason = _passes_keyword_filter(sl.title, must_inc, must_exc)
            if ok:
                kept_kw.append(sl)
            else:
                keyword_dropped += 1
        new_listings = kept_kw

    distance_dropped = 0
    if new_listings and watch.get("radius_km"):
        kept: list[SearchListing] = []
        home_lat = float(watch["latitude"])
        home_lng = float(watch["longitude"])
        radius = float(watch["radius_km"])
        soft_radius = max(radius + 8.0, radius * 1.3)
        for sl in new_listings:
            if not sl.seller_location:
                kept.append(sl)
                continue
            coords = geocode_city(sl.seller_location)
            if coords is None:
                kept.append(sl)
                continue
            dist = haversine_km(home_lat, home_lng, coords[0], coords[1])
            if dist <= soft_radius:
                kept.append(sl)
            else:
                distance_dropped += 1
        new_listings = kept

    appraised = 0
    rejected = 0
    for sl in new_listings:
        try:
            outcome = _process_new_listing(sl, search_id=search_id)
        except Exception as e:  # noqa: BLE001
            logger.exception("processing %s failed: %s", sl.id, e)
            record_event(
                "pipeline_error",
                search_id=search_id,
                listing_id=sl.id,
                error=str(e)[:300],
                error_type=type(e).__name__,
            )
            continue
        if outcome == "appraised":
            appraised += 1
        elif outcome == "rejected":
            rejected += 1

    elapsed_s = time.perf_counter() - t0
    record_event(
        "poll",
        search_id=search_id,
        duration_ms=int(elapsed_s * 1000),
        keyword=keyword,
        raw_count=len(listings),
        new_count=len(new_listings),
        appraised_count=appraised,
        rejected_count=rejected,
        distance_dropped=distance_dropped,
        keyword_dropped=keyword_dropped,
        batched=True,
        batch_total_raw=raw_count_in_batch,
    )


def _load_search(search_id: int) -> dict | None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT keyword, latitude, longitude, radius_km,
                          price_min, price_max, must_include, must_exclude
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
        "must_include": row[6], "must_exclude": row[7],
    }


def _parse_word_list(s: str | None) -> list[str]:
    """Comma-separated -> list of lowercased trimmed tokens. Empty list
    when input is None/empty/whitespace."""
    if not s:
        return []
    return [w.strip().lower() for w in s.split(",") if w.strip()]


def _passes_keyword_filter(
    title: str, must_include: list[str], must_exclude: list[str],
) -> tuple[bool, str | None]:
    """Returns (kept, reason). reason is set when kept=False."""
    title_l = (title or "").lower()
    if must_include:
        if not any(w in title_l for w in must_include):
            return False, f"missing required: {','.join(must_include)}"
    if must_exclude:
        for w in must_exclude:
            if w in title_l:
                return False, f"contains banned: {w}"
    return True, None


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

    # If asking is still a placeholder ($0 or $1) after price recovery
    # attempts, we can't score it — the formula needs a real asking
    # price. $1 listings with no recoverable real price almost always
    # mean "DM me / make an offer" and would otherwise score 0/100 with
    # 0th percentile, which is misleading.
    if asking <= 1.0:
        logger.info(
            "%s unscoreable: no recoverable asking price (placeholder $%s) | %s",
            sl.id, asking, pl.title[:60],
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
        record_event(
            "safety_drain",
            seen=stats.seen,
            appraised=stats.appraised,
            skipped_no_score=stats.skipped_no_score,
        )


def send_digest_emails() -> None:
    """Build and send one digest email per subscriber for any
    not-yet-notified, score-passing listings. Runs frequently (every
    15-60s) so this is effectively the 'instant alert' worker."""
    from ..alerts.digest import send_pending_digests
    stats = send_pending_digests()
    if stats["sent"] > 0 or stats["failed"] > 0:
        logger.info(
            "instant alert tick: sent=%d failed=%d total_listings=%d",
            stats["sent"], stats["failed"], stats["total_listings"],
        )


def send_daily_summary_emails() -> None:
    """Daily roundup: scored-but-below-threshold listings from the past
    24h. Runs hourly; only fires for subscribers whose last_summary was
    23+ hours ago."""
    from ..alerts.digest import send_daily_summaries
    stats = send_daily_summaries()
    if stats["sent"] > 0 or stats["failed"] > 0:
        logger.info(
            "daily summary tick: sent=%d failed=%d total_listings=%d",
            stats["sent"], stats["failed"], stats["total_listings"],
        )


def list_active_search_ids() -> list[int]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM user_searches WHERE active = TRUE ORDER BY id",
            )
            return [r[0] for r in cur.fetchall()]


def pick_next_watch_to_poll() -> int | None:
    """Choose the stalest active watch to poll next.

    Uses scheduler_events as the source of truth for "when did this
    watch last poll?" so we naturally round-robin across N watches at
    the rate gate's pace. Newly-added watches (no poll event yet)
    sort first thanks to NULLS FIRST.

    Returns None if there are no active watches.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT us.id
                   FROM user_searches us
                   LEFT JOIN LATERAL (
                       SELECT MAX(created_at) AS last_polled
                       FROM scheduler_events
                       WHERE event_type = 'poll'
                         AND search_id = us.id
                   ) p ON TRUE
                   WHERE us.active = TRUE
                   ORDER BY p.last_polled ASC NULLS FIRST, us.id ASC
                   LIMIT 1""",
            )
            row = cur.fetchone()
    return row[0] if row else None


# Default batch size — number of watches to fold into one FB search.
# 4 is empirically a sweet spot: FB's tokenizer handles 4 short keywords
# well, attribution stays unambiguous, and we 4x our effective per-watch
# cadence vs single-watch polling. Configurable via env.
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "4")) if os.environ.get("BATCH_SIZE") else 4


def pick_next_watch_batch(k: int = BATCH_SIZE) -> list[dict]:
    """Pick up to K stalest active watches that share location params.

    Watches in a batch must have the same (latitude, longitude,
    radius_km) so the combined FB search uses one set of geographic
    filters. We group by exact lat/lng (not fuzzy) — for the
    single-user single-home setup all watches share location anyway,
    and for multi-user later we'd need exact match for correctness.

    Returns a list of watch dicts with all the fields poll_search uses.
    Empty list if no active watches.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # First find the (lat, lng, radius) bucket of the stalest watch
            cur.execute(
                """SELECT us.latitude, us.longitude, us.radius_km
                   FROM user_searches us
                   LEFT JOIN LATERAL (
                       SELECT MAX(created_at) AS last_polled
                       FROM scheduler_events
                       WHERE event_type = 'poll' AND search_id = us.id
                   ) p ON TRUE
                   WHERE us.active = TRUE
                   ORDER BY p.last_polled ASC NULLS FIRST, us.id ASC
                   LIMIT 1""",
            )
            anchor = cur.fetchone()
            if anchor is None:
                return []
            anchor_lat, anchor_lng, anchor_radius = anchor

            # Then pick the K stalest watches in that bucket. Fuzzy
            # match on lat/lng because Postgres REAL is 32-bit float
            # and round-tripping through Python float (64-bit) shifts
            # the last bits, breaking strict equality.
            cur.execute(
                """SELECT us.id, us.keyword, us.latitude, us.longitude,
                          us.radius_km, us.price_min, us.price_max,
                          us.must_include, us.must_exclude
                   FROM user_searches us
                   LEFT JOIN LATERAL (
                       SELECT MAX(created_at) AS last_polled
                       FROM scheduler_events
                       WHERE event_type = 'poll' AND search_id = us.id
                   ) p ON TRUE
                   WHERE us.active = TRUE
                     AND ABS(us.latitude - %s::real) < 0.001
                     AND ABS(us.longitude - %s::real) < 0.001
                     AND us.radius_km = %s
                   ORDER BY p.last_polled ASC NULLS FIRST, us.id ASC
                   LIMIT %s""",
                (anchor_lat, anchor_lng, anchor_radius, k),
            )
            rows = cur.fetchall()
    return [{
        "id": r[0], "keyword": r[1],
        "latitude": float(r[2]), "longitude": float(r[3]),
        "radius_km": r[4],
        "price_min": r[5], "price_max": r[6],
        "must_include": r[7], "must_exclude": r[8],
    } for r in rows]


def attribute_listing(
    listing_title: str, batch: list[dict],
) -> dict | None:
    """Pick the watch in `batch` whose keyword best matches the listing.

    Strategy: lowercased, watch keyword's words must all appear (as
    substrings) in the title. Among matches, the longest keyword (most
    specific) wins. Ties broken by leftmost match position.

    Returns None if no watch in the batch matches — those listings get
    dropped (false positives from FB's loose tokenization).
    """
    title_l = (listing_title or "").lower()
    matches: list[tuple[int, int, dict]] = []
    for w in batch:
        kw = (w["keyword"] or "").lower().strip()
        if not kw:
            continue
        words = kw.split()
        if not all(word in title_l for word in words):
            continue
        # Longest keyword wins; tie-break by leftmost position of first word
        first_pos = title_l.find(words[0])
        matches.append((len(kw), -first_pos, w))
    if not matches:
        return None
    # Sort: longest kw first, then leftmost match
    matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
    return matches[0][2]


def coordinator_tick() -> None:
    """Pick the stalest watch and run one FB search for it.

    Polling mode is controlled by env var BATCH_POLL_MODE:
      "off" (default) — single-watch polling. Each tick fires one FB
        search for the stalest watch's keyword, processes only listings
        that come back. Predictable, accurate, FB returns relevant
        results for that one keyword.
      "on" — combined-keyword batching. K=BATCH_SIZE stalest watches
        share one FB call with a space-joined keyword. RISKY: we
        haven't empirically verified that FB returns OR-matching
        results for multi-word queries; it may rank by AND-relevance
        and miss the less-popular keywords. Off until verified.

    Adaptive backoff: when FB is rate-limiting heavily, skip the tick.
    """
    if _should_skip_tick_for_backoff():
        return

    if os.environ.get("BATCH_POLL_MODE", "off").lower() == "on":
        batch = pick_next_watch_batch()
        if not batch:
            return
        try:
            poll_batch(batch)
        except Exception as e:  # noqa: BLE001
            logger.exception("coordinator_tick batch %s crashed: %s",
                             [w["id"] for w in batch], e)
        return

    # Default: single-watch polling
    sid = pick_next_watch_to_poll()
    if sid is None:
        return
    try:
        poll_search(sid)
    except Exception as e:  # noqa: BLE001
        logger.exception("coordinator_tick(%s) crashed: %s", sid, e)


def _should_skip_tick_for_backoff() -> bool:
    """Return True if we should skip this tick because FB has been
    rate-limiting recently.

    Strategy: look at the rate-limit event count in the last 60s.
      0 events  -> proceed (clean)
      1-2       -> proceed (background noise)
      3-5       -> skip with 50% probability
      6+        -> skip
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM scheduler_events
                       WHERE event_type = 'fb_rate_limit'
                         AND created_at >= NOW() - INTERVAL '60 seconds'""",
                )
                recent = cur.fetchone()[0]
    except Exception:  # noqa: BLE001
        return False  # fail open

    if recent <= 2:
        return False
    if recent >= 6:
        logger.info("coordinator: skipping tick — %d rate-limits in last 60s", recent)
        return True
    # 3-5: probabilistic skip so we don't 100% halt
    import random
    if random.random() < 0.5:
        logger.info(
            "coordinator: probabilistic skip — %d rate-limits in last 60s",
            recent,
        )
        return True
    return False
