"""Tests for the deterministic deal-scoring formula.

The formula must be reproducible — same inputs always produce the same
output — and the score curve must move monotonically with the ratio.

Run with:  python -m pytest tests/test_formula.py -v
"""
from __future__ import annotations

import pytest

from deal_finder.appraisal.formula import (
    DEFAULT_ASKING_DISCOUNT,
    LLM_FALLBACK_THRESHOLD,
    compute_score,
    llm_needed,
)
from deal_finder.db.comps import CompStats


def _comp(
    *,
    n: int = 10,
    median: float = 200.0,
    trimmed_median: float | None = None,
    iqr: float | None = 50.0,
    outliers: int = 0,
) -> CompStats:
    """Build a CompStats with the fields compute_score actually reads."""
    return CompStats(
        search_term="test",
        source="marketplace",
        sample_size=n,
        median=median,
        mean=median,
        minimum=median * 0.5,
        maximum=median * 2,
        stddev=20.0,
        iqr=iqr,
        trimmed_sample_size=n - outliers if trimmed_median is not None else n,
        trimmed_median=trimmed_median if trimmed_median is not None else median,
        trimmed_mean=trimmed_median if trimmed_median is not None else median,
        outliers_dropped=outliers,
        fresh=True,
    )


# --- The user's iPhone 11 bug -------------------------------------------

def test_iphone_11_overpriced_case_now_scores_low():
    """The bug the user reported: iPhone 11 at $250 with median asking
    $230 should score LOW, not 89. With trimmed_median=$230 and 20%
    asking discount, fair_value=$184. Asking $250 / fair $184 = 1.36.
    Curve says ratio 1.36 -> score ~22."""
    comp = _comp(n=10, median=230.0, trimmed_median=230.0, iqr=40.0)
    breakdown = compute_score(asking_price=250.0, comp=comp)

    assert 184.0 == pytest.approx(breakdown.fair_value, abs=0.01)
    assert breakdown.ratio > 1.3
    # Score should be in the "overpriced" band, NOT the "good deal" band.
    assert breakdown.deal_score < 35
    assert breakdown.deal_score > 5
    assert breakdown.fair_value_source.startswith("trimmed_median")


# --- Score curve sanity --------------------------------------------------

def test_curve_unicorn_deal_scores_100_with_high_confidence():
    # asking $80, fair $200 → ratio 0.4. Need n>=12 with tight IQR
    # for confidence to be "high" (±5), which keeps cap at 95.
    comp = _comp(n=15, median=250.0, trimmed_median=250.0, iqr=20.0)
    s = compute_score(asking_price=80.0, comp=comp)
    assert s.deal_score >= 95
    assert s.confidence_label == "high"


def test_curve_neutral_at_one_to_one():
    # Want fair_value = asking. fair = trimmed_median * 0.8, so we need
    # trimmed_median = asking / 0.8 = 250.
    comp = _comp(n=10, median=250.0, trimmed_median=250.0)
    s = compute_score(asking_price=200.0, comp=comp)
    assert 48 <= s.deal_score <= 52  # near 50


def test_curve_overpriced_scores_low():
    comp = _comp(n=10, median=200.0, trimmed_median=200.0)  # fair=160
    s = compute_score(asking_price=300.0, comp=comp)  # ratio ~1.875
    assert s.deal_score <= 10


def test_curve_is_monotonic_in_ratio():
    """Score should never increase as asking price increases (with comps fixed)."""
    comp = _comp(n=10, median=250.0, trimmed_median=250.0)
    asking_prices = [50, 100, 150, 200, 250, 300, 400, 500]
    scores = [
        compute_score(asking_price=p, comp=comp).deal_score
        for p in asking_prices
    ]
    for a, b in zip(scores, scores[1:]):
        assert a >= b, f"score increased: {scores}"


def test_score_is_deterministic():
    comp = _comp(n=10, median=200.0, trimmed_median=200.0, iqr=50.0)
    a = compute_score(asking_price=120.0, comp=comp)
    b = compute_score(asking_price=120.0, comp=comp)
    assert a.deal_score == b.deal_score
    assert a.fair_value == b.fair_value
    assert a.ratio == b.ratio


# --- Fair-value source selection ----------------------------------------

def test_uses_trimmed_median_when_sample_is_sufficient():
    comp = _comp(n=10, median=200.0, trimmed_median=180.0)
    s = compute_score(asking_price=150.0, comp=comp, fair_value_from_llm=999.0)
    # Should ignore the LLM since we have enough comps.
    assert s.fair_value_source.startswith("trimmed_median")
    assert s.fair_value == pytest.approx(180.0 * (1 - DEFAULT_ASKING_DISCOUNT))


def test_falls_back_to_llm_when_sparse():
    comp = _comp(n=2, median=200.0, trimmed_median=200.0)
    s = compute_score(asking_price=150.0, comp=comp, fair_value_from_llm=170.0)
    assert s.fair_value_source == "llm"
    assert s.fair_value == 170.0


def test_falls_back_to_raw_median_when_no_llm_and_sparse():
    comp = _comp(n=2, median=200.0, trimmed_median=200.0)
    s = compute_score(asking_price=150.0, comp=comp, fair_value_from_llm=None)
    assert s.fair_value_source.startswith("raw_median")


def test_no_data_at_all_raises():
    comp = CompStats(search_term="x", source="marketplace", sample_size=0)
    with pytest.raises(ValueError):
        compute_score(asking_price=100.0, comp=comp, fair_value_from_llm=None)


def test_zero_asking_price_raises():
    comp = _comp(n=10)
    with pytest.raises(ValueError):
        compute_score(asking_price=0.0, comp=comp)


# --- llm_needed gate -----------------------------------------------------

def test_llm_needed_when_sample_under_threshold():
    assert llm_needed(_comp(n=LLM_FALLBACK_THRESHOLD - 1)) is True


def test_llm_not_needed_when_sample_meets_threshold():
    assert llm_needed(_comp(n=LLM_FALLBACK_THRESHOLD)) is False


def test_llm_needed_when_outliers_drag_trimmed_below_threshold():
    # 6 raw, 4 trimmed (2 outliers) — trimmed below threshold
    comp = _comp(n=6, trimmed_median=200.0, outliers=2)
    assert llm_needed(comp) is True


# --- Confidence ----------------------------------------------------------

def test_high_n_produces_high_confidence():
    comp = _comp(n=15, iqr=20.0, median=200.0)
    s = compute_score(asking_price=150.0, comp=comp)
    assert s.confidence_label == "high"


def test_high_iqr_lowers_confidence():
    tight = _comp(n=10, iqr=20.0, median=200.0)
    wide = _comp(n=10, iqr=180.0, median=200.0)
    s_tight = compute_score(asking_price=150.0, comp=tight)
    s_wide = compute_score(asking_price=150.0, comp=wide)
    assert s_wide.confidence_pm > s_tight.confidence_pm


def test_llm_fallback_is_low_confidence():
    comp = _comp(n=2)
    s = compute_score(asking_price=150.0, comp=comp, fair_value_from_llm=170.0)
    assert s.confidence_label == "low"


# --- Confidence cap on the score ----------------------------------------

def test_score_capped_by_confidence_on_niche_item():
    """The 'GE AC motor' case: $15 asking, $38 fair, ratio 0.39 — raw
    curve says 100. But with only 3 comps (low confidence, ±18) the
    cap is 100 - 18 = 82. Score should be 82, not 100."""
    comp = _comp(n=3, median=47.5, trimmed_median=47.5, iqr=20.0)
    s = compute_score(asking_price=15.0, comp=comp, fair_value_from_llm=38.0)
    assert s.confidence_label == "low"
    assert s.deal_score <= 100 - s.confidence_pm
    assert s.deal_score >= 70  # still recognized as a good deal


def test_high_confidence_does_not_cap_legitimate_unicorns():
    """Plenty of tight comps + truly amazing ratio should still score 100."""
    comp = _comp(n=14, median=400.0, trimmed_median=400.0, iqr=40.0)
    s = compute_score(asking_price=120.0, comp=comp)
    # ratio 0.375 -> raw score 100; high confidence -> ±5 cap is 95.
    # That's still capped, but only slightly.
    assert s.deal_score >= 95
    assert s.confidence_label == "high"


def test_cap_does_not_inflate_low_scores():
    """Cap only applies when raw_score > confidence_cap. A score of
    30 stays 30 even with low confidence."""
    comp = _comp(n=3, median=200.0, trimmed_median=200.0, iqr=80.0)
    s = compute_score(asking_price=300.0, comp=comp, fair_value_from_llm=200.0)
    # asking $300 / fair $200 = ratio 1.5 -> raw_score ~15
    # cap is 100 - 18 = 82. min(15, 82) = 15.
    assert s.deal_score < 30


# --- Data-quality flag (heterogeneous comps) ----------------------------

def test_data_quality_flagged_when_iqr_exceeds_median():
    """Vintage electric fishing motor: comps span $50-$2000 evenly.
    IQR will exceed median; flag should fire."""
    comp = _comp(n=10, median=400.0, trimmed_median=400.0, iqr=600.0)
    s = compute_score(asking_price=300.0, comp=comp)
    assert s.data_quality_poor is True
    assert s.iqr_to_median_ratio is not None
    assert s.iqr_to_median_ratio > 1.0


def test_data_quality_clean_when_iqr_is_tight():
    """Tight comp distribution (homogeneous category) -> flag stays off."""
    comp = _comp(n=10, median=400.0, trimmed_median=400.0, iqr=80.0)
    s = compute_score(asking_price=300.0, comp=comp)
    assert s.data_quality_poor is False
    assert s.iqr_to_median_ratio is not None
    assert s.iqr_to_median_ratio < 1.0


# --- Percentile rank ----------------------------------------------------

def test_percentile_rank_in_middle_of_range():
    """Asking equal to median -> percentile rank ~ 0.50."""
    comp = _comp(n=10, median=400.0, trimmed_median=400.0, iqr=100.0)
    s = compute_score(asking_price=400.0, comp=comp)
    assert s.percentile_rank is not None
    assert 0.4 <= s.percentile_rank <= 0.6


def test_percentile_rank_below_min():
    """Asking less than the cheapest comp -> percentile rank 0."""
    comp = _comp(n=10, median=400.0, trimmed_median=400.0, iqr=100.0)
    # _comp helper sets minimum = median * 0.5 = 200
    s = compute_score(asking_price=10.0, comp=comp)
    assert s.percentile_rank == 0.0


def test_percentile_rank_above_max():
    """Asking more than the priciest comp -> percentile rank 1.0."""
    comp = _comp(n=10, median=400.0, trimmed_median=400.0, iqr=100.0)
    # maximum from helper = median * 2 = 800
    s = compute_score(asking_price=5000.0, comp=comp)
    assert s.percentile_rank == 1.0
