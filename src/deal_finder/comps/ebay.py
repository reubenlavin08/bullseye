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
                results = []
                # Prefer Browse API (modern, well-quota'd, OAuth-based).
                # Only used if Cert ID is present so OAuth can complete.
                try:
                    results = client.find_active_items(
                        keywords=search_term, limit=entries_per_page,
                    )
                except ValueError as e:
                    # No Cert ID, or OAuth rejected. Fall through to
                    # the legacy Finding API which only needs App ID.
                    logger.info(
                        "ebay browse api unavailable (%s); trying finding api",
                        str(e)[:100],
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning("ebay browse-api error: %s", e)
                    results = []

                if not results:
                    # Fallback: legacy findCompletedItems (sold prices).
                    # Heavily rate-limited on new keysets; usually fails
                    # with HTTP 500 + 'exceeded number of times'. Keeping
                    # it as a fallback in case it's enabled for some
                    # accounts or comes back in the future.
                    try:
                        results = client.find_completed_items(
                            keywords=search_term,
                            entries_per_page=entries_per_page,
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "ebay-comp fetch failed for term=%r: %s",
                            search_term, e,
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
    """Get the best comp data for an FB Marketplace target listing.

    Decision rule (REVISED 2026-05 after observing score skew):
      * Marketplace sample >= MIN_MARKETPLACE_PRIMARY (default 8)
        → use Marketplace asking-prices (same-platform, fair comparison)
      * else (sparse Marketplace data, e.g. niche keyword)
        → fall back to eBay active listings; the platform mismatch is
          worth absorbing for a usable signal vs no signal at all

    Why we don't prefer eBay primary even though it has more data:
      eBay listings are systematically more expensive than Marketplace
      asking (shipping included, more new items, polished listings,
      retail-style sellers). Comparing a Marketplace listing's price
      against an eBay distribution puts it in eBay's bottom percentile
      → inflated deal_score. e.g. a $10 Marketplace router scored 91
      because eBay routers median $32; against Marketplace comps ($5-15
      typical) the same listing would score in the middle of the
      distribution. Unfair to the user — they'd get bombarded with
      'great deals' that are just average Marketplace prices.

      Marketplace-vs-Marketplace is the right comparison for an FB
      Marketplace target. eBay only when Marketplace has nothing.
    """
    MIN_MARKETPLACE_PRIMARY = 8

    # Lazy import to avoid circular dep
    from .marketplace import get_comps as get_marketplace_comps

    mp_stats = get_marketplace_comps(
        search_term=search_term,
        lat=49.2827, lng=-123.1207, radius_km=1500,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
    if mp_stats.sample_size >= MIN_MARKETPLACE_PRIMARY:
        return mp_stats

    # Sparse Marketplace data — try eBay as fallback. Better than
    # 'unscoreable' for niche keywords where Marketplace can't find
    # enough comps.
    ebay_stats = get_ebay_comps(
        search_term=search_term,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
    if ebay_stats.sample_size >= mp_stats.sample_size:
        return ebay_stats
    return mp_stats
