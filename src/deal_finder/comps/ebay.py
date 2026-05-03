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
import os
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

# Platform normalization factor for eBay prices. eBay listings are
# systematically more expensive than FB Marketplace asking-prices for
# the same item (shipping included, more new items, retail-style
# sellers, buyer-protection premium). Multiplying every eBay price by
# this factor before computing percentile rank brings the eBay
# distribution into Marketplace's scale, giving more accurate scores
# without losing eBay's better product-identity matching.
#
# Tuning: empirical observation shows eBay router/printer/monitor
# prices ~2-3x Marketplace medians for the same product. So 0.5-0.8
# is a reasonable range. Start at 0.85 (modest 15% reduction) and
# tune downward if scores are still inflated.
#
# Set EBAY_PRICE_NORMALIZATION=1.0 to disable (treat eBay prices
# as-is). Applied at READ time so retuning takes effect on next
# appraisal — no cache rebuild required.
def _normalization_factor() -> float:
    try:
        f = float(os.environ.get("EBAY_PRICE_NORMALIZATION", "0.85"))
    except (TypeError, ValueError):
        f = 0.85
    # Clamp to sane range so a typo can't turn off scoring entirely.
    return max(0.10, min(2.0, f))


def _scale_comp_stats(stats: CompStats, factor: float) -> CompStats:
    """Return a new CompStats with all price fields multiplied by
    `factor`. The anchor-based percentile rank in formula.py uses
    minimum/p10/q1/median/q3/p90/maximum, so all of those scale.
    Trimmed and outlier counts/sample_size pass through unchanged.

    Idempotent at factor=1.0: returns the input unchanged.
    """
    if factor == 1.0:
        return stats

    def _s(v: float | None) -> float | None:
        return v * factor if v is not None else None

    # Construct a new CompStats dataclass instance with same shape.
    # We replicate every field so this stays robust if CompStats grows.
    from dataclasses import fields as _fields, replace
    scalable = {
        "median", "mean", "minimum", "maximum",
        "trimmed_median", "trimmed_mean",
        "p10", "q1", "q3", "p90",
        "iqr",
    }
    overrides = {}
    for fld in _fields(stats):
        if fld.name in scalable:
            overrides[fld.name] = _s(getattr(stats, fld.name))
    return replace(stats, **overrides)

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
    factor = _normalization_factor()
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
                    "ebay-comp cache hit term=%r n=%d median=%.2f factor=%.2f",
                    search_term, cached.sample_size, cached.median or 0, factor,
                )
                return _scale_comp_stats(cached, factor)

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
        stats = fetch_stats(
            conn, search_term, SOURCE,
            ttl_seconds=ttl_seconds,
            asking_price=asking_price,
            target_text=target_text if use_embedding_filter else None,
        )
    # Scale eBay's distribution down to Marketplace-comparable range
    # via EBAY_PRICE_NORMALIZATION (default 0.85). Done at READ time
    # so retuning takes effect on next appraisal.
    return _scale_comp_stats(stats, factor)


def get_best_comps(
    *,
    search_term: str,
    asking_price: float | None = None,
    target_text: str | None = None,
    use_embedding_filter: bool = False,
) -> CompStats:
    """Get the best comp data for an FB Marketplace target listing.

    Decision rule: prefer eBay (better product matching), fall back to
    Marketplace if eBay is sparse.

    Why eBay-primary even though scores skew higher:
      User wants product-match accuracy over distribution shape. eBay
      listings include brand/model/condition explicitly, so when we
      search 'Dlink R15 Router' on eBay we get actual Dlink R15
      Router listings — not 'random Marketplace listings whose title
      happens to contain Dlink'. The slight upward skew in scores is
      acceptable because the underlying comp set is a far more
      faithful comparison. User can raise their score threshold (e.g.
      from 90 to 95) to filter out the 'merely average' tier.

    Trade-off acknowledged:
      eBay listings tend to be ~20-30% higher than Marketplace asking
      for the same item (shipping, new items, retail-style sellers).
      A Marketplace target will land in eBay's lower percentiles
      → inflated deal_score. The trade is: better product identity
      match (signal) at the cost of less spread (precision in the
      tails). Tune via score_threshold if it becomes noisy.

    Tunables:
      MIN_EBAY_PRIMARY (default 5) — minimum eBay sample to use it
      as primary. Below that, we fall back to Marketplace which
      tends to have more samples for non-branded niche keywords.
    """
    MIN_EBAY_PRIMARY = int(
        os.environ.get("MIN_EBAY_PRIMARY_SAMPLE", "5")
    )

    # Lazy import to avoid circular dep
    from .marketplace import get_comps as get_marketplace_comps

    ebay_stats = get_ebay_comps(
        search_term=search_term,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
    if ebay_stats.sample_size >= MIN_EBAY_PRIMARY:
        return ebay_stats

    # Sparse eBay data — fall back to Marketplace asking-prices.
    return get_marketplace_comps(
        search_term=search_term,
        lat=49.2827, lng=-123.1207, radius_km=1500,
        asking_price=asking_price,
        target_text=target_text,
        use_embedding_filter=use_embedding_filter,
    )
