"""eBay comps adapter — sold-price comp lookups via the Finding API.

Mirrors the shape of `comps/marketplace.py`:

  get_ebay_comps(search_term=...) → CompStats

Same TTL cache (12h default) backed by the same `comps` table; rows are
distinguished from Marketplace rows by `source='ebay'`. Callers can
decide which source to prefer (typically eBay > Marketplace because
sold > asking).

Disabled gracefully: if EBAY_APP_ID isn't set or the Finding API call
errors out, returns an empty CompStats so the appraisal pipeline can
fall back to Marketplace comps without crashing.
"""
from __future__ import annotations

import logging
import threading

from ..db.comps import CompStats, fetch_stats, insert_comps
from ..db.connection import get_conn
from ..scraper.ebay import (
    get_default_client as get_ebay_client,
    is_ebay_enabled,
    to_comp_observations,
)

logger = logging.getLogger(__name__)

SOURCE = "ebay"
DEFAULT_TTL_SECONDS = 12 * 3600  # same as marketplace, ground-truth lasts longer
                                  # but keep cadence aligned for now.

# Singleflight coalescer (same pattern as comps/marketplace.py).
_inflight_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}


def _coalesce_key(search_term: str) -> str:
    return f"{SOURCE}|{search_term.strip().lower()}"


def get_ebay_comps(
    *,
    search_term: str,
    asking_price: float | None = None,
    target_text: str | None = None,
    use_embedding_filter: bool = False,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force_refresh: bool = False,
    entries_per_page: int = 50,
) -> CompStats:
    """Returns sold-price comp stats from eBay for `search_term`.

    Behavior:
      * If EBAY_APP_ID isn't configured → returns an empty CompStats
        immediately (no error, just no eBay data).
      * Otherwise: cache hit → return; cache miss → call Finding API,
        insert results, return fresh stats.
      * Concurrent callers for the same term coalesce to ONE API call.

    Returns an empty (sample_size=0) CompStats on disable / network
    error, so the appraisal pipeline can always proceed.
    """
    if not is_ebay_enabled():
        return CompStats(search_term=search_term, source=SOURCE, sample_size=0)

    # Try cache first (same shape as marketplace path)
    if not force_refresh:
        with get_conn() as conn:
            cached = fetch_stats(
                conn, search_term, SOURCE,
                ttl_seconds=ttl_seconds,
                asking_price=asking_price,
                target_text=target_text if use_embedding_filter else None,
            )
            if cached.fresh and cached.sample_size > 0:
                logger.debug(
                    "ebay-comp cache hit term=%r n=%d median=%.2f",
                    search_term, cached.sample_size, cached.median or 0,
                )
                return cached

    # Cache miss → coalesce + fetch
    key = _coalesce_key(search_term)
    am_leader = False
    with _inflight_lock:
        event = _inflight.get(key)
        if event is None:
            event = threading.Event()
            _inflight[key] = event
            am_leader = True

    if am_leader:
        try:
            client = get_ebay_client()
            if client is None:
                # disabled or invalid APP_ID — leave cache empty
                pass
            else:
                try:
                    results = client.find_completed_items(
                        keywords=search_term,
                        entries_per_page=entries_per_page,
                    )
                    obs = to_comp_observations(results)
                    if obs:
                        with get_conn() as conn:
                            with conn:
                                inserted = insert_comps(conn, search_term, SOURCE, obs)
                        logger.info(
                            "ebay-comp refetch term=%r inserted=%d",
                            search_term, inserted,
                        )
                    else:
                        logger.info("ebay-comp empty result term=%r", search_term)
                except Exception as e:  # noqa: BLE001
                    # Fail soft: log + continue with whatever cache we have.
                    # Common reasons: rate-limited (5000/day cap),
                    # transient network, app-id mis-key.
                    logger.warning(
                        "ebay-comp fetch failed for term=%r: %s",
                        search_term, e,
                    )
        finally:
            event.set()
            with _inflight_lock:
                _inflight.pop(key, None)
    else:
        if not event.wait(timeout=60):
            logger.warning(
                "ebay-comp coalesce timeout for key=%s; reading cache",
                key,
            )

    with get_conn() as conn:
        return fetch_stats(
            conn, search_term, SOURCE,
            ttl_seconds=ttl_seconds,
            asking_price=asking_price,
            target_text=target_text if use_embedding_filter else None,
        )


def get_best_comps(
    *,
    search_term: str,
    asking_price: float | None = None,
    target_text: str | None = None,
    use_embedding_filter: bool = False,
) -> CompStats:
    """Prefer eBay sold comps; fall back to Marketplace if eBay has
    nothing useful. The appraisal pipeline can call this directly to
    get 'whichever source has data' without needing source-specific
    branching.

    Decision rule:
      * eBay sample >= MIN_SAMPLE_FOR_PREFERENCE  → use eBay (sold prices)
      * else                                       → use Marketplace
    """
    MIN_SAMPLE_FOR_PREFERENCE = 5

    # Lazy import to avoid circular dep — marketplace.py imports
    # nothing from this module so the cycle is asymmetric, but keeping
    # the import lazy is cheap and safe.
    from .marketplace import get_comps as get_marketplace_comps

    ebay_stats = get_ebay_comps(
        search_term=search_term,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
    if ebay_stats.sample_size >= MIN_SAMPLE_FOR_PREFERENCE:
        return ebay_stats

    # Fall back to Marketplace asking-prices
    return get_marketplace_comps(
        search_term=search_term,
        lat=49.2827, lng=-123.1207, radius_km=1500,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
