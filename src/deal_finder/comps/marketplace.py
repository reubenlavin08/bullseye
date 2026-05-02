"""Marketplace asking-price comp fetcher.

Bridge implementation while the eBay developer account is in review.
For each listing we want to score, do a second Marketplace search with
the listing's title (or a normalized version) as the query, take the
asking prices that come back, and write them to the `comps` table for
the appraisal worker to read.

Caveats — important for the LLM appraisal prompt later:
  * These are ASKING prices, not SOLD. Asking is typically inflated by
    15-30% over what items actually sell for.
  * Comp set size is whatever the search returns — usually ~24 items.
  * Filtering: we drop the listing being scored from its own comp set
    (matching by ID), and we drop free / $0 comps that are obviously
    placeholder listings.

Once eBay is approved, a sister module `comps/ebay.py` will fetch sold
prices into the same table with `source='ebay'` and the appraisal layer
will prefer eBay over Marketplace when both exist.
"""
from __future__ import annotations

import logging

from ..db.comps import CompObservation, CompStats, fetch_stats, insert_comps
from ..db.connection import get_conn
from ..scraper.facebook import (
    SearchParams,
    get_default_client as get_search_client,
)

logger = logging.getLogger(__name__)

SOURCE = "marketplace"
DEFAULT_TTL_SECONDS = 12 * 3600


def get_comps(
    *,
    search_term: str,
    lat: float,
    lng: float,
    radius_km: int = 100,
    exclude_listing_id: str | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    force_refresh: bool = False,
) -> CompStats:
    """Get comps for a search term, fetching from FB if cache is stale.

    Returns a CompStats with median/mean/etc. Even if the fetch fails
    we return a CompStats (with fresh=False, sample_size=0) so the caller
    has a consistent shape to handle.
    """
    with get_conn() as conn:
        if not force_refresh:
            cached = fetch_stats(
                conn, search_term, SOURCE, ttl_seconds=ttl_seconds,
            )
            if cached.fresh and cached.sample_size > 0:
                logger.debug(
                    "comp cache hit term=%r n=%d median=%.2f",
                    search_term, cached.sample_size, cached.median or 0,
                )
                return cached

    # Cache miss — refetch from Marketplace.
    obs = _fetch_observations(
        search_term=search_term,
        lat=lat, lng=lng, radius_km=radius_km,
        exclude_listing_id=exclude_listing_id,
    )

    with get_conn() as conn:
        with conn:
            inserted = insert_comps(conn, search_term, SOURCE, obs)
        with conn:
            stats = fetch_stats(
                conn, search_term, SOURCE, ttl_seconds=ttl_seconds,
            )

    logger.info(
        "comp refetch term=%r inserted=%d sample=%d median=%s",
        search_term, inserted, stats.sample_size,
        f"{stats.median:.2f}" if stats.median else "n/a",
    )
    return stats


def _fetch_observations(
    *,
    search_term: str,
    lat: float,
    lng: float,
    radius_km: int,
    exclude_listing_id: str | None,
) -> list[CompObservation]:
    """Run a Marketplace search and convert the listings into comp observations."""
    page = get_search_client().search(SearchParams(
        keyword=search_term,
        lat=lat, lng=lng, radius_km=radius_km,
    ))

    out: list[CompObservation] = []
    for sl in page.listings:
        if exclude_listing_id and sl.id == exclude_listing_id:
            continue
        if sl.price_amount is None or sl.price_amount <= 1.0:
            # $0/$1 are placeholder listings; they pollute the median.
            # We don't try to recover their hidden price here — the
            # listing-side pipeline already handles that for items
            # we're scoring. Comps are noisy enough already.
            continue
        out.append(CompObservation(
            price=sl.price_amount,
            title=sl.title,
            listing_url=sl.listing_url,
            location=sl.seller_location,
        ))
    return out
