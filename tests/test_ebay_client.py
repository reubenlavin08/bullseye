"""Tests for the eBay Finding API client.

Network paths can't run without an EBAY_APP_ID, so they're gated by a
RUN_LIVE_TESTS=1 env. The non-network paths — response parsing and the
to_comp_observations adapter — are exhaustively unit-tested here
because the Finding API's array-everywhere JSON shape has lots of
edge cases.

Run:  python -m pytest tests/test_ebay_client.py -v
"""
from __future__ import annotations

import os

import pytest

from deal_finder.scraper.ebay import (
    EbayCompResult,
    _extract_sold,
    _parse_completed_items,
    is_ebay_enabled,
    to_comp_observations,
)


# --- enable / disable gate -----------------------------------------------

def test_is_ebay_enabled_off_when_no_app_id(monkeypatch):
    monkeypatch.delenv("EBAY_APP_ID", raising=False)
    assert not is_ebay_enabled()


def test_is_ebay_enabled_on_when_app_id_set(monkeypatch):
    monkeypatch.setenv("EBAY_APP_ID", "TestAppID-Sandbox-PRD-abc123")
    assert is_ebay_enabled()


def test_is_ebay_enabled_force_off(monkeypatch):
    """Even with an APP_ID, EBAY_ENABLED=0 disables the integration."""
    monkeypatch.setenv("EBAY_APP_ID", "TestAppID")
    monkeypatch.setenv("EBAY_ENABLED", "0")
    assert not is_ebay_enabled()


# --- _extract_sold(): the per-item parser --------------------------------


def _make_item(price: float = 100.0, currency: str = "USD") -> dict:
    """Build a Finding-API-shaped item dict (every value wrapped in a
    1-element list) for testing the parser."""
    return {
        "itemId": [f"item-{int(price)}"],
        "title": [f"Test Item @ {price}"],
        "viewItemURL": ["https://www.ebay.com/itm/123"],
        "location": ["California, USA"],
        "listingInfo": [{"endTime": ["2026-04-30T12:00:00.000Z"]}],
        "sellingStatus": [{
            "currentPrice": [{"@currencyId": currency, "__value__": str(price)}],
            "convertedCurrentPrice": [{"@currencyId": "USD", "__value__": str(price)}],
        }],
    }


def test_extract_sold_happy_path():
    item = _make_item(price=42.0)
    result = _extract_sold(item)
    assert result is not None
    assert result.item_id == "item-42"
    assert result.title == "Test Item @ 42.0"
    assert result.price_amount == 42.0
    assert result.currency == "USD"
    assert result.view_url == "https://www.ebay.com/itm/123"
    assert result.location == "California, USA"


def test_extract_sold_missing_price_returns_none():
    item = _make_item()
    item["sellingStatus"] = [{}]   # no currentPrice or converted
    assert _extract_sold(item) is None


def test_extract_sold_missing_id_returns_none():
    item = _make_item()
    del item["itemId"]
    assert _extract_sold(item) is None


def test_extract_sold_missing_title_returns_none():
    item = _make_item()
    del item["title"]
    assert _extract_sold(item) is None


def test_extract_sold_falls_back_to_converted_price():
    """When currentPrice is malformed, convertedCurrentPrice is the
    fallback. Real responses sometimes have only one of the two."""
    item = _make_item(price=99.0)
    item["sellingStatus"] = [{
        "currentPrice": "not a dict",   # broken / wrong type
        "convertedCurrentPrice": [{"@currencyId": "CAD", "__value__": "150.5"}],
    }]
    result = _extract_sold(item)
    assert result is not None
    assert result.price_amount == 150.5
    assert result.currency == "CAD"


def test_extract_sold_handles_unwrapped_dicts():
    """Sometimes a field arrives as a plain dict instead of [dict].
    Parser should handle both shapes gracefully."""
    item = {
        "itemId": "raw-id-123",                      # not wrapped in a list
        "title": "Unwrapped",
        "sellingStatus": {                           # also not wrapped
            "currentPrice": {"@currencyId": "USD", "__value__": "12.34"},
        },
    }
    result = _extract_sold(item)
    assert result is not None
    assert result.item_id == "raw-id-123"
    assert result.price_amount == 12.34


def test_extract_sold_invalid_price_returns_none():
    item = _make_item()
    item["sellingStatus"][0]["currentPrice"][0]["__value__"] = "NaN-not-a-number"
    item["sellingStatus"][0]["convertedCurrentPrice"][0]["__value__"] = "ALSO-BAD"
    # Both parses fail — returns None
    assert _extract_sold(item) is None


# --- _parse_completed_items(): the top-level walker ----------------------


def test_parse_completed_items_happy_path():
    body = {
        "findCompletedItemsResponse": [{
            "ack": ["Success"],
            "searchResult": [{
                "@count": "2",
                "item": [_make_item(price=10.0), _make_item(price=20.0)],
            }],
        }],
    }
    results = _parse_completed_items(body)
    assert len(results) == 2
    assert results[0].price_amount == 10.0
    assert results[1].price_amount == 20.0


def test_parse_completed_items_empty_body():
    """Truly empty response (no items) → empty list, no crash."""
    assert _parse_completed_items({}) == []
    assert _parse_completed_items({"findCompletedItemsResponse": []}) == []
    assert _parse_completed_items({
        "findCompletedItemsResponse": [{"searchResult": [{"item": []}]}],
    }) == []


def test_parse_completed_items_skips_malformed_items():
    """One bad item shouldn't take down the whole response — we keep
    the good ones."""
    body = {
        "findCompletedItemsResponse": [{
            "searchResult": [{
                "item": [
                    _make_item(price=10.0),
                    {"itemId": ["only-id"]},               # missing title + price
                    _make_item(price=20.0),
                ],
            }],
        }],
    }
    results = _parse_completed_items(body)
    assert len(results) == 2
    assert {r.price_amount for r in results} == {10.0, 20.0}


def test_parse_completed_items_handles_failure_ack():
    """API-level failure shouldn't crash the parser — log the error
    and return whatever items came back (usually none)."""
    body = {
        "findCompletedItemsResponse": [{
            "ack": ["Failure"],
            "errorMessage": [{"error": [{"message": ["bad request"]}]}],
        }],
    }
    # Should not raise
    results = _parse_completed_items(body)
    assert results == []


# --- to_comp_observations() adapter --------------------------------------


def test_to_comp_observations_basic():
    raw = [
        EbayCompResult(
            item_id="1", title="Arduino Uno", price_amount=15.0,
            currency="USD", end_time_iso=None,
            view_url="https://example.com/1", location="CA",
        ),
        EbayCompResult(
            item_id="2", title="Arduino Nano", price_amount=8.0,
            currency="USD", end_time_iso=None,
            view_url="https://example.com/2", location="NY",
        ),
    ]
    obs = to_comp_observations(raw)
    assert len(obs) == 2
    assert obs[0].price == 15.0
    assert obs[0].title == "Arduino Uno"
    assert obs[0].listing_url == "https://example.com/1"
    assert obs[1].price == 8.0


def test_to_comp_observations_empty_input():
    assert to_comp_observations([]) == []


# --- live test (gated) ---------------------------------------------------

LIVE_ENABLED = (
    os.environ.get("RUN_LIVE_TESTS") == "1"
    and os.environ.get("EBAY_APP_ID")
)


@pytest.mark.skipif(not LIVE_ENABLED, reason="live eBay tests need RUN_LIVE_TESTS=1 + EBAY_APP_ID")
def test_finding_api_live_returns_real_sold_arduinos():
    """Smoke test: hit eBay's real Finding API for a high-volume keyword
    ('arduino uno') and verify we get at least 5 sold listings with
    sane prices.

    Only runs with RUN_LIVE_TESTS=1 + EBAY_APP_ID. Costs 1 of your
    5000 daily Finding API calls."""
    from deal_finder.scraper.ebay import EbayClient

    client = EbayClient()
    results = client.find_completed_items(
        keywords="arduino uno", entries_per_page=20,
    )

    print(f"\n=== live eBay sold-comps for 'arduino uno' ===")
    print(f"got {len(results)} results")
    for r in results[:5]:
        print(f"  ${r.price_amount:7.2f} {r.currency} | {r.title[:60]}")

    assert len(results) >= 5, "expected at least 5 sold arduinos on eBay"
    prices = [r.price_amount for r in results]
    median = sorted(prices)[len(prices) // 2]
    assert 5 <= median <= 200, f"median ${median} outside reasonable range for arduino uno"
