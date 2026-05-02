"""Tests for the price extraction layer.

Run with:  python -m pytest tests/test_price_extraction.py -v
"""
from __future__ import annotations

from deal_finder.scraper.price_extraction import (
    PLACEHOLDER_THRESHOLD,
    PriceResolution,
    extract_price_from_description,
    resolve_price,
)


# ---- normal-price passthrough -------------------------------------------

def test_normal_price_passes_through():
    r = resolve_price(250.0, "great condition, must sell")
    assert r == PriceResolution(price=250.0, raw_price=250.0, extracted=False)


def test_normal_price_with_no_description():
    r = resolve_price(50.0, None)
    assert r.price == 50.0
    assert r.extracted is False


def test_normal_price_above_threshold_ignores_description_dollar_sign():
    r = resolve_price(500.0, "comes with $20 charger included")
    assert r.price == 500.0
    assert r.extracted is False


# ---- placeholder $0 / $1 with extractable description -------------------

def test_zero_with_dollar_sign_in_description():
    r = resolve_price(0.0, "selling for $200 obo, no lowballs")
    assert r.price == 200.0
    assert r.raw_price == 0.0
    assert r.extracted is True


def test_one_dollar_with_obo():
    r = resolve_price(1.0, "400 obo, please don't lowball")
    assert r.price == 400.0
    assert r.raw_price == 1.0
    assert r.extracted is True


def test_asking_keyword():
    r = resolve_price(0.0, "asking 350, firm price")
    assert r.price == 350.0
    assert r.extracted is True


def test_price_is_phrase():
    r = resolve_price(1.0, "Price is 500 cash only")
    assert r.price == 500.0
    assert r.extracted is True


def test_firm_prefix():
    r = resolve_price(0.0, "firm 200 no negotiation")
    assert r.price == 200.0
    assert r.extracted is True


def test_firm_suffix():
    r = resolve_price(0.0, "200 firm")
    assert r.price == 200.0
    assert r.extracted is True


def test_dollar_sign_takes_priority_over_other_patterns():
    # $1500 should win over "asking 200" because $-pattern is first
    r = resolve_price(0.0, "asking 200 but I'd accept $1500 honestly")
    assert r.price == 1500.0


def test_thousands_with_comma():
    r = resolve_price(0.0, "selling for $1,200 obo")
    assert r.price == 1200.0


def test_decimal_price():
    r = resolve_price(0.0, "$19.99 each")
    assert r.price == 19.99


# ---- placeholder with no extractable price -------------------------------

def test_zero_with_no_price_in_description_stays_zero():
    r = resolve_price(0.0, "free if you pick it up today")
    assert r.price == 0.0
    assert r.raw_price == 0.0
    assert r.extracted is False


def test_zero_with_empty_description():
    r = resolve_price(0.0, "")
    assert r.price == 0.0
    assert r.extracted is False


def test_zero_with_none_description():
    r = resolve_price(0.0, None)
    assert r.price == 0.0
    assert r.extracted is False


def test_one_with_no_extractable_price_stays_one():
    r = resolve_price(1.0, "see photos, runs perfect")
    assert r.price == 1.0
    assert r.extracted is False


# ---- threshold guards ---------------------------------------------------

def test_extraction_ignores_sub_threshold_match():
    # "$1" in description shouldn't be treated as a real price extraction —
    # the threshold guard skips it. Falls through to next pattern or to
    # placeholder fallback.
    r = resolve_price(0.0, "totally free, $1 if you insist")
    # No pattern matches >= 1.00 strictly; "$1" tied to threshold should be
    # rejected, leaving the listing as $0.
    assert r.price <= PLACEHOLDER_THRESHOLD


# ---- helper-only tests ---------------------------------------------------

def test_extract_helper_returns_none_when_empty():
    assert extract_price_from_description("") is None
    assert extract_price_from_description("   ") is None


def test_extract_helper_returns_none_when_no_match():
    assert extract_price_from_description("just some text, no numbers") is None
