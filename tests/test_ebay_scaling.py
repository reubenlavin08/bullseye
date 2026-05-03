"""Tests for eBay price normalization (scale CompStats by a factor).

Why this exists: eBay listings are systematically more expensive than
FB Marketplace asking-prices (shipping, new items, retail-style
sellers). When we use eBay as the comp source for a Marketplace
target, the target lands in eBay's lower percentile → inflated score.
We multiply eBay CompStats by EBAY_PRICE_NORMALIZATION (default 0.85)
to bring it into Marketplace-comparable range while keeping eBay's
better product-identity matching.
"""
from __future__ import annotations

import pytest

from deal_finder.comps.ebay import _normalization_factor, _scale_comp_stats
from deal_finder.db.comps import CompStats


def _stats(**kw) -> CompStats:
    """Helper to build a CompStats with sensible defaults for testing."""
    base = dict(
        search_term="test",
        source="ebay",
        sample_size=20,
        median=100.0,
        mean=105.0,
        minimum=50.0,
        maximum=200.0,
        p10=60.0,
        q1=80.0,
        q3=130.0,
        p90=170.0,
        iqr=50.0,
        trimmed_median=98.0,
        trimmed_mean=102.0,
        trimmed_sample_size=18,
        outliers_dropped=2,
    )
    base.update(kw)
    return CompStats(**base)


def test_factor_default(monkeypatch):
    """Default normalization factor is 0.85 (15% reduction)."""
    monkeypatch.delenv("EBAY_PRICE_NORMALIZATION", raising=False)
    assert _normalization_factor() == 0.85


def test_factor_env_override(monkeypatch):
    """User can override via env."""
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "0.75")
    assert _normalization_factor() == 0.75


def test_factor_disabled(monkeypatch):
    """Factor = 1.0 disables scaling entirely."""
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "1.0")
    assert _normalization_factor() == 1.0


def test_factor_clamped_too_low(monkeypatch):
    """Negative or near-zero factors get clamped to 0.10 to prevent
    accidentally turning off scoring (every price → 0)."""
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "-0.5")
    assert _normalization_factor() == 0.10
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "0.001")
    assert _normalization_factor() == 0.10


def test_factor_clamped_too_high(monkeypatch):
    """Factor capped at 2.0 — sanity bound."""
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "10")
    assert _normalization_factor() == 2.0


def test_factor_invalid_falls_back_to_default(monkeypatch):
    """Garbage env value uses default rather than crashing."""
    monkeypatch.setenv("EBAY_PRICE_NORMALIZATION", "not-a-number")
    assert _normalization_factor() == 0.85


def test_scale_comp_stats_at_default_factor():
    """0.85 factor: every price field × 0.85, counts unchanged."""
    s = _stats()
    scaled = _scale_comp_stats(s, 0.85)
    assert scaled.median == 100.0 * 0.85
    assert scaled.mean == 105.0 * 0.85
    assert scaled.minimum == 50.0 * 0.85
    assert scaled.maximum == 200.0 * 0.85
    assert scaled.p10 == 60.0 * 0.85
    assert scaled.q1 == 80.0 * 0.85
    assert scaled.q3 == 130.0 * 0.85
    assert scaled.p90 == 170.0 * 0.85
    assert scaled.iqr == 50.0 * 0.85
    assert scaled.trimmed_median == 98.0 * 0.85
    assert scaled.trimmed_mean == 102.0 * 0.85
    # Counts must NOT scale
    assert scaled.sample_size == 20
    assert scaled.trimmed_sample_size == 18
    assert scaled.outliers_dropped == 2


def test_scale_comp_stats_at_one_is_noop():
    """factor=1.0 returns the input unchanged (avoids unnecessary
    object churn, also acts as the disable switch)."""
    s = _stats()
    scaled = _scale_comp_stats(s, 1.0)
    assert scaled is s   # short-circuit return


def test_scale_handles_none_fields():
    """CompStats with None price fields (e.g. tiny sample, no IQR)
    must not crash."""
    s = _stats(p10=None, p90=None, iqr=None, q1=None, q3=None)
    scaled = _scale_comp_stats(s, 0.5)
    assert scaled.p10 is None
    assert scaled.p90 is None
    assert scaled.iqr is None
    assert scaled.median == 50.0   # 100 * 0.5
    assert scaled.minimum == 25.0  # 50 * 0.5


def test_scale_preserves_distribution_shape():
    """After scaling, the percentile rank of any value v in the
    original distribution should equal the percentile rank of v*factor
    in the scaled distribution (linearity preservation)."""
    from deal_finder.appraisal.formula import _percentile_rank

    s = _stats()
    factor = 0.7
    scaled = _scale_comp_stats(s, factor)

    # A value at the original median should be at the scaled median's
    # percentile = ~50%. A value of 100 (orig median) should rank
    # below the scaled distribution since scaled median is 70.
    orig_at_median = _percentile_rank(100.0, s)
    scaled_at_scaled_median = _percentile_rank(70.0, scaled)
    assert orig_at_median == scaled_at_scaled_median

    # Asking $50 vs original distribution: rank ≈ 0% (= minimum)
    # Asking $50 vs scaled distribution (min=$35): rank > 0%
    rank_orig = _percentile_rank(50.0, s)
    rank_scaled = _percentile_rank(50.0, scaled)
    assert rank_scaled > rank_orig   # Marketplace asking now lands
                                     # higher in scaled distribution
                                     # → score goes DOWN (more
                                     # accurate)
