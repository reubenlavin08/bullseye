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

    HTTP request count: this function makes exactly ONE call to the
    FB GraphQL endpoint regardless of len(batch). The batch's keywords
    are space-joined into a single `query` field on a single
    SearchParams. Detail/PDP fetches per attributed listing happen
    after the search returns — those are separate requests, identical
    to single-watch polling.

    Why this can still be wrong: we have not empirically verified that
    FB does OR-matching across the joined tokens. If FB ranks results
    by AND-relevance, a query like "arduino raspberry pi esp32" might
    only return listings that mention multiple of those terms — losing
    most of the per-keyword listings we'd otherwise get from polling
    each watch separately. tests/test_batch_polling_live.py is the
    pending verification.

    The batch must share (latitude, longitude, radius_km) — see
    pick_next_watch_batch.
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

    # >>> THE single HTTP call. One request, K keywords. <<<
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


# --- Slow-start ramp -----------------------------------------------------
#
# Start cautious (1 poll/min) and progressively shorten the effective
# interval while FB stays quiet. Reset to the initial interval the
# moment we see a rate-limit. Off by default; opt-in via env so
# operators who already know their floor can run at COORDINATOR_TICK_S
# directly.
#
#   SLOW_START_MODE=1 to enable
#   SLOW_START_INITIAL_S       starting effective interval (default 60s)
#   SLOW_START_FLOOR_S         fastest we'll go (default 20s)
#   SLOW_START_HEALTHY_PERIOD_S window to be "clean" before ramping (default 300s)
#   SLOW_START_STEP_S          interval reduction per ramp step (default 5s)
#
# The coordinator still ticks every COORDINATOR_TICK_S, but most ticks
# are skipped while the effective interval > tick. As ramp-down
# happens, more ticks turn into actual polls.
SLOW_START_MODE = os.environ.get("SLOW_START_MODE", "0") == "1"
SLOW_START_INITIAL_S = int(os.environ.get("SLOW_START_INITIAL_S", "60"))
SLOW_START_FLOOR_S = int(os.environ.get("SLOW_START_FLOOR_S", "20"))
SLOW_START_HEALTHY_PERIOD_S = int(os.environ.get("SLOW_START_HEALTHY_PERIOD_S", "300"))
SLOW_START_STEP_S = int(os.environ.get("SLOW_START_STEP_S", "5"))

# Module-level state. Survives across ticks; reset on process restart
# (a fresh boot starts at SLOW_START_INITIAL_S — sensible default).
_slow_start_state = {
    "min_interval_s": SLOW_START_INITIAL_S,
    "last_poll_attempt_t": 0.0,
    "last_ramp_check_t": 0.0,
}


def _no_rate_limits_in_last_period(period_s: int) -> bool:
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT COUNT(*) FROM scheduler_events
                        WHERE event_type='fb_rate_limit'
                          AND created_at >= NOW() - INTERVAL '{period_s} seconds'""",
                )
                return cur.fetchone()[0] == 0
    except Exception:  # noqa: BLE001
        return True  # fail open — assume healthy


def _slow_start_should_skip() -> bool:
    """When SLOW_START_MODE is on, skip ticks that arrive sooner than
    the current effective minimum interval.

    Side-effects: when we DO let a tick through, this also runs the
    ramp-down check (every SLOW_START_HEALTHY_PERIOD_S) and may shorten
    the effective interval, or reset it to initial if a rate-limit
    was seen during the window.
    """
    if not SLOW_START_MODE:
        return False
    now = time.monotonic()
    state = _slow_start_state

    # First-call init: don't immediately ramp down because last_ramp_check_t
    # was 0 (which would make any boot skip the full initial period). We
    # set both timers to now so the first poll goes through unimpeded
    # AND the first ramp is delayed by a full HEALTHY_PERIOD.
    if state["last_ramp_check_t"] == 0.0:
        state["last_ramp_check_t"] = now
        state["last_poll_attempt_t"] = now
        return False

    elapsed = now - state["last_poll_attempt_t"]
    if elapsed < state["min_interval_s"]:
        return True

    # We're letting this tick through. Ramp check first.
    if now - state["last_ramp_check_t"] >= SLOW_START_HEALTHY_PERIOD_S:
        state["last_ramp_check_t"] = now
        if _no_rate_limits_in_last_period(SLOW_START_HEALTHY_PERIOD_S):
            old = state["min_interval_s"]
            new = max(SLOW_START_FLOOR_S, old - SLOW_START_STEP_S)
            if new != old:
                state["min_interval_s"] = new
                logger.info(
                    "slow-start ramp: %ds clean → effective interval %ds → %ds",
                    SLOW_START_HEALTHY_PERIOD_S, old, new,
                )
                record_event(
                    "slow_start_ramp",
                    direction="down", from_s=old, to_s=new,
                )
        else:
            old = state["min_interval_s"]
            state["min_interval_s"] = SLOW_START_INITIAL_S
            if state["min_interval_s"] != old:
                logger.warning(
                    "slow-start reset: rate-limit in window → interval %ds → %ds",
                    old, state["min_interval_s"],
                )
                record_event(
                    "slow_start_ramp",
                    direction="reset",
                    from_s=old, to_s=state["min_interval_s"],
                )

    state["last_poll_attempt_t"] = now
    return False


# --- Adaptive rate-limit backoff -----------------------------------------
#
# Philosophy: the moment FB rate-limits us we should HARD pull back, not
# pretend nothing happened. Exponential cooldown that resets when FB
# starts answering cleanly again.
#
# Cooldown schedule (each rate-limit event in the last 30 min compounds):
#   1 rate limit  -> 60s cooldown
#   2 rate limits -> 120s
#   3 rate limits -> 240s
#   4 rate limits -> 480s
#   5+            -> 600s (10 min cap)
#
# The cooldown clock starts at the MOST RECENT rate limit. So if we get
# hit twice in a row, we wait 120s after the *second* hit, not after the
# first. After 30 min with no rate limits, the count rolls off and we
# fully reset. Configurable via env if anyone wants to tune.
RATE_LIMIT_BASE_COOLDOWN_S = int(os.environ.get("RATE_LIMIT_BASE_COOLDOWN_S", "60"))
RATE_LIMIT_MAX_COOLDOWN_S = int(os.environ.get("RATE_LIMIT_MAX_COOLDOWN_S", "600"))
RATE_LIMIT_WINDOW_S = int(os.environ.get("RATE_LIMIT_WINDOW_S", "1800"))  # 30 min

# Module-level state so we only log "entered cooldown" once, not every tick.
_last_logged_cooldown_until: float = 0.0


def coordinator_tick() -> None:
    """Pick the stalest watch and run one FB search for it.

    Polling mode is controlled by env var BATCH_POLL_MODE:
      "off" (default) — single-watch polling. Each tick fires one FB
        search for the stalest watch's keyword, processes only listings
        that come back. Predictable, accurate, FB returns relevant
        results for that one keyword.
      "on" — combined-keyword batching. K=BATCH_SIZE stalest watches
        share ONE FB request with a space-joined keyword string. The
        request count is 1, not K — see poll_batch() docstring. RISKY:
        we haven't empirically verified that FB returns OR-matching
        results for multi-word queries; it may rank by AND-relevance
        and miss the less-popular keywords. Off until verified by
        tests/test_batch_polling_live.py.

    Three-stage gate before we actually poll:
      1. Cooldown gate (_should_skip_tick_for_backoff): if recent
         rate-limits put us in exponential cooldown, skip this tick.
      2. Slow-start gate (_slow_start_should_skip): if SLOW_START_MODE
         is on and we polled too recently, skip.
      3. Half-open probe gate (_circuit_breaker_should_skip): if we
         were rate-limited recently but cooldown cleared, do a cheap
         HTML probe BEFORE committing to a real GraphQL search. If the
         probe is blocked or FB is down, skip and re-arm cooldown via
         a synthetic rate-limit event so we don't burn search quota
         confirming we're still flagged.
    """
    if _should_skip_tick_for_backoff():
        return
    if _slow_start_should_skip():
        return
    if _circuit_breaker_should_skip():
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


def _compute_cooldown_remaining_s() -> int:
    """How many seconds to wait before next FB request, based on
    recent rate-limit history.

    Returns 0 when we're clear to proceed.

    Strategy:
      - count fb_rate_limit events in the last RATE_LIMIT_WINDOW_S
      - cooldown = base * 2^(min(n-1, 4)), capped at MAX
      - apply decorrelated jitter (uniform 0.85x-1.15x of nominal) so
        repeated retries don't fire at synchronized wall-clock offsets
        and so any external rate-limit observer can't fingerprint our
        retry cadence
      - clock starts at the MOST RECENT rate-limit timestamp
      - remaining = (most_recent + cooldown) - now

    The jitter is *deterministic per rate-limit timestamp* so the
    countdown timer the dashboard shows doesn't oscillate every time
    the dashboard polls. We seed random with the timestamp.
    """
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT COUNT(*),
                              EXTRACT(EPOCH FROM (NOW() - MAX(created_at))),
                              EXTRACT(EPOCH FROM MAX(created_at))
                       FROM scheduler_events
                       WHERE event_type = 'fb_rate_limit'
                         AND created_at >= NOW() - INTERVAL '{RATE_LIMIT_WINDOW_S} seconds'""",
                )
                row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return 0  # fail open — better to poll than to get stuck

    n, secs_since_last, last_epoch = row[0], row[1], row[2]
    if not n or secs_since_last is None:
        return 0

    # Exponential cooldown: 60s, 120s, 240s, 480s, 600s (nominal).
    nominal = min(
        RATE_LIMIT_BASE_COOLDOWN_S * (2 ** min(int(n) - 1, 4)),
        RATE_LIMIT_MAX_COOLDOWN_S,
    )
    # Decorrelated jitter, deterministic per (n, last_epoch) so the
    # countdown is stable across dashboard refreshes within the same
    # cooldown period.
    import random as _r
    jitter_seed = int((last_epoch or 0) * 1000) ^ int(n) << 8
    rng = _r.Random(jitter_seed)
    jittered = nominal * rng.uniform(0.85, 1.15)
    remaining = jittered - float(secs_since_last)
    return max(0, int(remaining))


def _circuit_breaker_should_skip() -> bool:
    """Half-open probe gate. If we have RECENT rate-limits and the
    cooldown JUST cleared (we're about to retry for the first time
    since being blocked), do a cheap HTML probe FIRST to test the
    waters. If the probe says we're still blocked, skip the tick and
    record a synthetic rate-limit event so the next cooldown is
    longer (we don't burn a search request to learn the same thing).

    Closed → Open → Half-Open → Closed semantics:
      * Closed     = no recent rate-limits, normal polling
      * Open       = inside cooldown window (handled by
                     _should_skip_tick_for_backoff)
      * Half-Open  = cooldown just cleared. Probe instead of polling.
                     Probe success → closed (proceed to actual poll).
                     Probe blocked → re-open with longer cooldown.
                     Probe says down → skip this tick, retry next.

    The 'recent rate-limits' check uses a SHORTER window than the
    cooldown's 30-min window so we don't probe every tick once the
    rate-limits roll off — once we have a clean run we trust the
    closed state.
    """
    HALF_OPEN_LOOKBACK_S = 600  # only probe if rate-limited in last 10 min
    PROBE_COOLDOWN_S = 90       # don't re-probe within 90s of last probe

    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                # How recent is the last rate-limit?
                cur.execute(
                    f"""SELECT EXTRACT(EPOCH FROM (NOW() - MAX(created_at)))
                        FROM scheduler_events
                        WHERE event_type = 'fb_rate_limit'
                          AND created_at >= NOW() - INTERVAL '{HALF_OPEN_LOOKBACK_S} seconds'""",
                )
                row = cur.fetchone()
                secs_since_last_rl = row[0] if row else None

                # Did we already probe recently? Don't probe more than
                # once per PROBE_COOLDOWN_S — wasteful and looks bot-y.
                # Use ORDER BY ... LIMIT 1 instead of MAX() so we can
                # also pull `detail->>'result'` from the same row
                # (mixing an aggregate with a non-aggregate non-grouped
                # column would be a SQL grouping error).
                cur.execute(
                    """SELECT EXTRACT(EPOCH FROM (NOW() - created_at)),
                              detail->>'result'
                       FROM scheduler_events
                       WHERE event_type = 'fb_probe'
                       ORDER BY created_at DESC
                       LIMIT 1""",
                )
                row = cur.fetchone()
                if row and row[0] is not None and row[0] <= PROBE_COOLDOWN_S:
                    secs_since_probe, last_probe_result = row[0], row[1]
                else:
                    secs_since_probe, last_probe_result = None, None
    except Exception as e:  # noqa: BLE001
        logger.warning("circuit breaker query failed (fail open): %s", e)
        return False  # fail open

    # No recent rate-limits → closed state, proceed normally.
    if secs_since_last_rl is None:
        return False

    # If we have a recent probe result, trust it briefly.
    if secs_since_probe is not None:
        if last_probe_result == "ok":
            return False  # probe just said we're back, proceed
        # Probe said blocked / down recently — skip without re-probing.
        return True

    # Half-open: cooldown cleared but we're still in the lookback
    # window. Probe before committing to a real search.
    try:
        result = get_search_client().probe_marketplace_root()
    except Exception as e:  # noqa: BLE001
        logger.warning("probe raised (treating as 'down'): %s", e)
        result = "down"

    record_event("fb_probe", result=result)
    logger.info("fb-probe (half-open) → %s", result)

    if result == "ok":
        return False  # transition to closed; let the actual poll happen
    if result == "blocked":
        # Synthesize a rate-limit event so the cooldown extends. This
        # is the "open with reset timer" half of the circuit breaker.
        record_event(
            "fb_rate_limit",
            code=1675004,
            message="probe-detected block (no quota burned)",
            severity="probe",
        )
        return True
    # 'down' → don't re-open; FB looks unhealthy, just skip this tick.
    return True


def _should_skip_tick_for_backoff() -> bool:
    """Skip this tick if we're inside the rate-limit cooldown window."""
    global _last_logged_cooldown_until
    remaining = _compute_cooldown_remaining_s()
    if remaining <= 0:
        return False

    # Log "entered cooldown" once per cooldown period, not every tick.
    cooldown_until = time.time() + remaining
    if cooldown_until > _last_logged_cooldown_until + 5:
        # New / extended cooldown — log it and emit a dashboard event.
        logger.warning(
            "coordinator: rate-limit cooldown active — skipping ticks for ~%ds",
            remaining,
        )
        record_event(
            "rate_limit_backoff",
            cooldown_remaining_s=remaining,
        )
        _last_logged_cooldown_until = cooldown_until
    else:
        logger.debug(
            "coordinator: still in cooldown (~%ds remaining)", remaining,
        )
    return True
