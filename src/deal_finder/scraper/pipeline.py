"""End-to-end scrape pipeline.

Wires together the search client, the detail fetcher, the price
extractor, and the rejection filter into one chain. The output is a
list of `ProcessedListing`s ready to feed downstream (eBay comps + LLM
appraisal in later phases).

No DB layer yet — that lands in Phase 2b/5 when Postgres is online. For
now, `process_search` returns everything in memory; deduplication is the
caller's concern. Once Postgres is wired up, a `seen_ids: set[str]`
parameter (or a callback) will short-circuit detail fetches for
already-seen listings.

Public API:
    process_search(params, *, fetch_details=True) -> list[ProcessedListing]
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from .facebook import (
    FacebookSearchClient,
    SearchListing,
    SearchParams,
    get_default_client as get_search_client,
)
from .facebook_detail import (
    Detail,
    FacebookDetailClient,
    get_default_client as get_detail_client,
)
from .price_extraction import resolve_price
from .rejection import evaluate as evaluate_rejection

logger = logging.getLogger(__name__)


@dataclass
class ProcessedListing:
    """Final output of one trip through the pipeline.

    Combines search-card data with the (optional) detail-fetch result
    and the outputs of price extraction + rejection filtering. This is
    the row a future `db.upsert` would persist.
    """
    # From search
    id: str
    title: str
    photo_url: str | None
    listing_url: str
    is_pending: bool
    previous_price: str | None
    seller_location: str | None
    price_formatted: str | None

    # From detail (None if detail fetch was skipped or failed)
    description: str | None
    seller_name: str | None
    seller_type: str | None
    listed_at_unix: int | None
    detail_source: str | None
    category_id: str | None = None
    detail_errors: list[str] = field(default_factory=list)

    # From price extraction
    raw_price: float = 0.0
    resolved_price: float = 0.0
    price_extracted_from_description: bool = False

    # From rejection filter
    rejected: bool = False
    rejection_reason: str | None = None


def process_search(
    params: SearchParams,
    *,
    fetch_details: bool = True,
    seen_ids: Iterable[str] | None = None,
    search_client: FacebookSearchClient | None = None,
    detail_client: FacebookDetailClient | None = None,
) -> list[ProcessedListing]:
    """Run the full scrape chain for one search.

    Steps:
      1. Search Marketplace (paginated to one page for now)
      2. Filter out IDs in `seen_ids` (cheap dedup, no DB needed)
      3. For each remaining listing, fetch detail (description) if
         `fetch_details=True`
      4. Run price extraction with the fetched description
      5. Run rejection filter on title + description
      6. Emit ProcessedListing rows for every input listing — including
         rejected ones, so callers can persist them with rejected=True
    """
    sc = search_client or get_search_client()
    dc = detail_client or get_detail_client() if fetch_details else None
    seen = set(seen_ids or ())

    page = sc.search(params)
    logger.info(
        "search keyword=%r returned=%d has_more=%s",
        params.keyword, len(page.listings), page.has_more,
    )

    out: list[ProcessedListing] = []
    for sl in page.listings:
        if sl.id in seen:
            continue
        detail = dc.fetch(sl.id) if dc is not None else None
        out.append(_combine(sl, detail))

    return out


def _combine(sl: SearchListing, detail: Detail | None) -> ProcessedListing:
    """Merge search row + detail row + filter outputs into one record."""
    description = detail.description if detail else None

    raw_price = sl.price_amount if sl.price_amount is not None else 0.0
    pr = resolve_price(raw_price, description or "")
    rj = evaluate_rejection(sl.title, description=description)

    return ProcessedListing(
        # Search side
        id=sl.id,
        title=sl.title,
        photo_url=sl.photo_url,
        listing_url=sl.listing_url,
        is_pending=sl.is_pending,
        previous_price=sl.previous_price,
        seller_location=sl.seller_location,
        price_formatted=sl.price_formatted,
        category_id=sl.category_id,
        # Detail side
        description=description,
        seller_name=detail.seller_name if detail else None,
        seller_type=detail.seller_type if detail else None,
        listed_at_unix=detail.listed_at_unix if detail else None,
        detail_source=detail.source if detail else None,
        detail_errors=list(detail.errors) if detail else [],
        # Pipeline side
        raw_price=pr.raw_price,
        resolved_price=pr.price,
        price_extracted_from_description=pr.extracted,
        rejected=rj.rejected,
        rejection_reason=rj.reason,
    )
