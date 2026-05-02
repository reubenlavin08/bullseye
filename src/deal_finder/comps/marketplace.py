"""Marketplace asking-price comp fetcher.

Bridge implementation while the eBay developer account is in review.
For each listing we want to score, do a second Marketplace search with
the listing's title (or a normalized version) as the query, take the
asking prices that come back, and write them to the `comps` table for
the appraisal worker to read.

Filtering pipeline (in order):
  1. Drop the listing being scored from its own comp set (by ID).
  2. For $0/$1 placeholder comps, attempt JIT detail-fetch + price
     recovery (regex first, LLM fallback). Up to PLACEHOLDER_RECOVERY_BUDGET
     per lookup. Comps with unrecoverable prices are dropped.
  3. (Optional, when target_text is provided) embed comps + target via
     nomic-embed-text and drop comps below the similarity threshold.
     Falls back to no filtering if embeddings unavailable.

Caveats:
  * These are ASKING prices, not SOLD. The appraisal formula applies
    a ~20% asking-vs-sold discount before scoring.
  * Comp set size is whatever the search returns — usually ~24 items.

Once eBay is approved, a sister module `comps/ebay.py` will fetch sold
prices into the same table with `source='ebay'` and the appraisal layer
will prefer eBay over Marketplace when both exist.
"""
from __future__ import annotations

import logging
import threading

from ..db.comps import CompObservation, CompStats, fetch_stats, insert_comps
from ..db.connection import get_conn
from ..scraper.facebook import (
    SearchParams,
    get_default_client as get_search_client,
)
from ..scraper.facebook_detail import (
    get_default_client as get_detail_client,
)
from ..scraper.price_extraction import resolve_price

logger = logging.getLogger(__name__)

SOURCE = "marketplace"
DEFAULT_TTL_SECONDS = 12 * 3600

# Cap how many $0/$1 placeholder comps we'll attempt to recover per
# lookup. Each recovery is a detail HTTP fetch + maybe an LLM call —
# 5 is a reasonable balance between data completeness and latency.
PLACEHOLDER_RECOVERY_BUDGET = 5


# --- Singleflight coalescing ---------------------------------------------
#
# Two appraisals for the same normalized title arriving within seconds
# would each cache-miss, each do a full FB search, and each insert the
# same comp rows. Coalescing collapses N concurrent cache-miss-fetches
# for the same (search_term, source, category) into ONE network call;
# the other N-1 callers block on the leader's result and then read from
# the shared cache.
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def _coalesce_key(search_term: str, source: str, category_id: str | None) -> str:
    return f"{source}|{search_term.strip().lower()}|{category_id or ''}"


def get_comps(
    *,
    search_term: str,
    lat: float,
    lng: float,
    radius_km: int = 100,
    exclude_listing_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force_refresh: bool = False,
    asking_price: float | None = None,
    target_text: str | None = None,
    use_embedding_filter: bool = False,
    category_id: str | None = None,
) -> CompStats:
    """Get comps for a search term, fetching from FB if cache is stale.

    Optional semantic filtering:
      target_text      -- the title (or title+description) of the listing
                          being scored. Used to compute cosine similarity
                          to each comp; comps below threshold are dropped.
      use_embedding_filter -- master switch (e.g. tests can disable).

    `asking_price` enables bimodal-cluster filtering: when comps split
    into a cheap-cluster and expensive-cluster (e.g. "boat motor" hits
    both trolling motors and outboards), we keep the cluster closest
    to the asking price.

    Returns a CompStats. Embedding-based filtering is annotated on the
    returned object via `embedding_filter_applied` etc. (see CompStats).
    """
    with get_conn() as conn:
        if not force_refresh:
            cached = fetch_stats(
                conn, search_term, SOURCE,
                ttl_seconds=ttl_seconds,
                asking_price=asking_price,
                target_text=target_text if use_embedding_filter else None,
            )
            if cached.fresh and cached.sample_size > 0:
                logger.debug(
                    "comp cache hit term=%r n=%d median=%.2f",
                    search_term, cached.sample_size, cached.median or 0,
                )
                return cached

    # Cache miss — refetch from Marketplace, coalescing concurrent
    # callers so we do exactly ONE FB request per (term, source, cat).
    key = _coalesce_key(search_term, SOURCE, category_id)
    am_leader = False
    with _inflight_lock:
        event = _inflight.get(key)
        if event is None:
            event = threading.Event()
            _inflight[key] = event
            am_leader = True

    inserted = 0
    if am_leader:
        try:
            obs = _fetch_observations(
                search_term=search_term,
                lat=lat, lng=lng, radius_km=radius_km,
                exclude_listing_id=exclude_listing_id,
                category_id=category_id,
            )
            with get_conn() as conn:
                with conn:
                    inserted = insert_comps(conn, search_term, SOURCE, obs)
        finally:
            event.set()
            with _inflight_lock:
                _inflight.pop(key, None)
    else:
        # Wait for the leader to finish its fetch + insert. 120s is
        # generous; FB searches normally complete in 5-30s.
        if not event.wait(timeout=120):
            logger.warning(
                "comp coalesce timeout waiting for leader on key=%s; "
                "proceeding to read whatever's in cache",
                key,
            )
        else:
            logger.debug("comp coalesce hit: rode along on leader for %r", search_term)

    with get_conn() as conn:
        stats = fetch_stats(
            conn, search_term, SOURCE,
            ttl_seconds=ttl_seconds,
            asking_price=asking_price,
            target_text=target_text if use_embedding_filter else None,
        )

    logger.info(
        "comp refetch term=%r inserted=%d sample=%d median=%s "
        "embed_filter=%s kept=%s",
        search_term, inserted, stats.sample_size,
        f"{stats.median:.2f}" if stats.median else "n/a",
        stats.embedding_filter_applied,
        stats.embedding_kept_count,
    )
    return stats


def _fetch_observations(
    *,
    search_term: str,
    lat: float,
    lng: float,
    radius_km: int,
    exclude_listing_id: str | None,
    category_id: str | None = None,
) -> list[CompObservation]:
    """Run a Marketplace search and convert the listings into comp observations.

    Category filtering: FB's `filter_category_id` parameter is silently
    ignored on the public search GraphQL (verified empirically — passing
    it returns the same mixed-category set). So we filter CLIENT-SIDE:
    after fetching, drop any listing whose category_id doesn't match
    the target's. This solves the cars-vs-dashcams bug.

    For $0/$1 placeholder listings, attempt to recover the real price
    via detail-fetch + extraction (up to PLACEHOLDER_RECOVERY_BUDGET).
    Anything still placeholder after that is dropped.
    """
    page = get_search_client().search(SearchParams(
        keyword=search_term,
        lat=lat, lng=lng, radius_km=radius_km,
        category_id=category_id,
    ))

    # Client-side category filter (FB's server-side one is broken).
    if category_id:
        before = len(page.listings)
        page.listings = [
            sl for sl in page.listings
            if sl.category_id == category_id or not sl.category_id
        ]
        dropped = before - len(page.listings)
        if dropped:
            logger.info(
                "category filter dropped %d/%d off-category comps "
                "(target_cat=%s, term=%r)",
                dropped, before, category_id, search_term,
            )

    out: list[CompObservation] = []
    placeholder_attempts = 0
    placeholder_dropped = 0
    placeholder_recovered = 0

    for sl in page.listings:
        if exclude_listing_id and sl.id == exclude_listing_id:
            continue
        if sl.price_amount is None:
            continue

        price = sl.price_amount
        title = sl.title

        if price <= 1.0:
            # Placeholder. Try to recover within budget.
            if placeholder_attempts >= PLACEHOLDER_RECOVERY_BUDGET:
                placeholder_dropped += 1
                continue
            placeholder_attempts += 1
            recovered = _try_recover_placeholder_price(sl.id)
            if recovered is None:
                placeholder_dropped += 1
                continue
            price = recovered
            placeholder_recovered += 1

        out.append(CompObservation(
            price=price,
            title=title,
            listing_url=sl.listing_url,
            location=sl.seller_location,
        ))

    if placeholder_attempts > 0:
        logger.info(
            "comp placeholder recovery term=%r attempts=%d recovered=%d dropped=%d",
            search_term, placeholder_attempts,
            placeholder_recovered, placeholder_dropped,
        )
    return out


def _try_recover_placeholder_price(listing_id: str) -> float | None:
    """Detail-fetch a $0/$1 listing and try to recover its real price.

    Regex first; falls back to the small LLM if regex misses. Returns
    None if no price could be recovered (we drop the comp in that case).

    Imported lazily so the comp module doesn't depend on appraisal at
    import time — keeps test suites and isolated modules fast.
    """
    try:
        detail = get_detail_client().fetch(listing_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("placeholder detail fetch failed %s: %s", listing_id, e)
        return None
    if not detail.description:
        return None

    pr = resolve_price(0.0, detail.description)
    if pr.extracted:
        return pr.price

    # Regex missed — try the small LLM. Lazy import to avoid the
    # comps module pulling in Ollama deps at import time.
    try:
        from ..appraisal.normalizer import extract_price_llm
    except ImportError:
        return None
    llm_price = extract_price_llm(detail.description)
    return llm_price
