"""LIVE network test: does FB Marketplace combined-keyword search
actually return results for each individual term?

This is the empirical verification we've been deferring. The whole
multi-keyword batching feature (BATCH_POLL_MODE=on) hinges on the
assumption that posting `query="arduino raspberry pi esp32"` to FB's
search endpoint returns a *union* of results matching ANY of those
tokens, not just listings that match ALL of them.

If the test passes → flip BATCH_POLL_MODE=on, get ~Kx faster effective
polling (one FB request covers K watches).
If it fails → batching is fundamentally broken; keep it off and find
another approach.

Run manually:
    pytest tests/test_batch_polling_live.py -v -s -m live

Skipped by default because:
  - hits real FB Marketplace, costs rate-limit budget
  - non-deterministic (depends on what's currently listed)
  - blocked entirely when FB has rate-limited our IP

Set RUN_LIVE_TESTS=1 to force them on, or pass `-m live` to pytest.
"""
from __future__ import annotations

import os

import pytest

from deal_finder.scheduler.jobs import attribute_listing
from deal_finder.scraper.facebook import (
    SearchParams,
    get_default_client,
)

# Vancouver, BC — well-populated metro area, gives us plenty of comps.
VAN_LAT = 49.2827
VAN_LNG = -123.1207

LIVE_ENABLED = os.environ.get("RUN_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not LIVE_ENABLED,
    reason="live FB tests skipped; set RUN_LIVE_TESTS=1 or use -m live",
)


# Generic, high-volume keywords picked so each one will almost certainly
# have ≥1 result on any given day in Vancouver. Adjust if any of these
# stop being reliable.
BATCH_KEYWORDS = ["bike", "couch", "guitar", "monitor"]


def _fb_search(keyword: str) -> list:
    """One unbatched FB search. Returns the listings list."""
    page = get_default_client().search(SearchParams(
        keyword=keyword,
        lat=VAN_LAT,
        lng=VAN_LNG,
        radius_km=40,
    ))
    if page.rate_limited:
        pytest.skip(f"rate-limited on baseline {keyword!r}: {page.error_message}")
    return page.listings


def test_combined_keyword_returns_results_for_each_term():
    """The smoking-gun test. Issue ONE combined search and verify that
    the result set contains listings attributable to EVERY individual
    term in the batch.

    Pass criteria: every keyword in BATCH_KEYWORDS gets at least one
    listing attributed to it via attribute_listing().

    Failure mode: FB returns only AND-matched listings (titles
    containing multiple/all batch terms) → most keywords get 0
    attributions → batching is broken.
    """
    combined = " ".join(BATCH_KEYWORDS)
    page = get_default_client().search(SearchParams(
        keyword=combined,
        lat=VAN_LAT,
        lng=VAN_LNG,
        radius_km=40,
    ))
    if page.rate_limited:
        pytest.skip(f"rate-limited on combined search: {page.error_message}")

    # Build the same shape attribute_listing() expects.
    batch = [{"id": i, "keyword": kw} for i, kw in enumerate(BATCH_KEYWORDS)]

    # Count attributions per keyword.
    attributions: dict[str, int] = {kw: 0 for kw in BATCH_KEYWORDS}
    for sl in page.listings:
        w = attribute_listing(sl.title, batch)
        if w is not None:
            attributions[w["keyword"]] += 1

    print(f"\n=== combined query={combined!r} returned {len(page.listings)} listings ===")
    for kw, n in attributions.items():
        print(f"  {kw:>10s}: {n} attributed")

    missing = [kw for kw, n in attributions.items() if n == 0]
    assert not missing, (
        f"FB combined search FAILED to return any listings matching: {missing}. "
        f"This is evidence FB does AND-matching, not OR. "
        f"Combined-keyword batching is broken; keep BATCH_POLL_MODE=off."
    )


def test_combined_recall_vs_individual_baseline():
    """Stronger test: compare combined batching's per-keyword recall
    against single-keyword polling.

    For each keyword in the batch, count how many distinct listing IDs
    we get from a single-keyword search. Then run one combined search
    and count how many of those IDs we recover via attribution.

    Pass criteria: combined recovers ≥30% of the per-keyword listings
    on average. (We don't expect 100% — FB caps results at 24 per page,
    so a combined search physically can't return all of 4×24 = 96
    distinct items.)

    Failure mode: <30% recall means combined-keyword search effectively
    drops most per-keyword traffic; batching saves request count but
    loses too many listings to be worth it.
    """
    # Step 1: gather baseline IDs per keyword
    per_kw: dict[str, set[str]] = {}
    for kw in BATCH_KEYWORDS:
        listings = _fb_search(kw)
        per_kw[kw] = {sl.id for sl in listings}
        print(f"baseline {kw!r}: {len(per_kw[kw])} unique IDs")

    # Step 2: one combined search
    combined = " ".join(BATCH_KEYWORDS)
    page = get_default_client().search(SearchParams(
        keyword=combined,
        lat=VAN_LAT,
        lng=VAN_LNG,
        radius_km=40,
    ))
    if page.rate_limited:
        pytest.skip(f"rate-limited on combined search: {page.error_message}")
    combined_ids = {sl.id for sl in page.listings}
    print(f"combined query={combined!r}: {len(combined_ids)} unique IDs")

    # Step 3: per-keyword recall
    print("\n=== per-keyword recall ===")
    recalls = []
    for kw, baseline_ids in per_kw.items():
        if not baseline_ids:
            continue
        overlap = baseline_ids & combined_ids
        recall = len(overlap) / len(baseline_ids)
        recalls.append(recall)
        print(f"  {kw:>10s}: {len(overlap)}/{len(baseline_ids)} = {recall:.0%}")

    avg_recall = sum(recalls) / len(recalls) if recalls else 0.0
    print(f"\navg recall: {avg_recall:.0%}")

    assert avg_recall >= 0.30, (
        f"Combined search avg recall {avg_recall:.0%} below 30% threshold — "
        f"too many per-keyword listings missed. Batching is not safe to enable."
    )
