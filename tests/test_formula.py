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


# --- Percentile scoring semantics (formula 2.0) -------------------------
# Score = (1 - percentile_rank) * 100, capped by confidence.
# "Score X means this listing is cheaper than X% of similar listings."

def test_asking_well_below_min_scores_max():
    """Asking cheaper than every comp → percentile 0 → score capped at
    the confidence ceiling (typically 95 for high-confidence)."""
    comp = _comp(n=15, median=400.0, trimmed_median=400.0, iqr=40.0)
    # _comp helper sets minimum = median * 0.5 = 200; asking $50 < $200
    s = compute_score(asking_price=50.0, comp=comp)
    assert s.percentile_rank == 0.0
    assert s.deal_score >= 90


def test_asking_at_median_scores_about_50():
    """Asking equal to the median → percentile 0.5 → score ~50."""
    comp = _comp(n=10, median=250.0, trimmed_median=250.0, iqr=40.0)
    s = compute_score(asking_price=250.0, comp=comp)
    assert s.percentile_rank == pytest.approx(0.5, abs=0.01)
    assert 48 <= s.deal_score <= 52


def test_asking_above_max_scores_zero():
    """Asking above every comp → percentile 1.0 → score 0."""
    comp = _comp(n=10, median=300.0, trimmed_median=300.0, iqr=50.0)
    # max from helper = median * 2 = 600
    s = compute_score(asking_price=2000.0, comp=comp)
    assert s.percentile_rank == 1.0
    assert s.deal_score == 0


def test_iphone_11_overpriced_case_scores_below_50():
    """Real case from user: iPhone 11 at $250, median asking $230.
    Under percentile scoring: asking is slightly above median, so
    percentile ~0.55 → score ~45. Not a unicorn (correctly), but also
    not catastrophic — it's only slightly above peer pricing."""
    comp = _comp(n=10, median=230.0, trimmed_median=230.0, iqr=40.0)
    s = compute_score(asking_price=250.0, comp=comp)
    assert s.percentile_rank > 0.5
    assert s.deal_score < 50
    assert s.deal_score > 20
    assert s.fair_value_source.startswith("trimmed_median")


def test_score_is_monotonic_in_asking_price():
    """Score should never increase as asking increases (comps fixed).
    Percentile rank is monotonic, so the score must be too."""
    comp = _comp(n=10, median=250.0, trimmed_median=250.0, iqr=40.0)
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


# --- Unscoreable behavior (formula v3.0: refuse rather than guess) ------

def test_uses_trimmed_median_when_sample_is_sufficient():
    comp = _comp(n=10, median=200.0, trimmed_median=180.0)
    s = compute_score(asking_price=150.0, comp=comp)
    assert s.unscoreable is False
    assert s.fair_value_source.startswith("trimmed_median")
    assert s.fair_value == pytest.approx(180.0 * (1 - DEFAULT_ASKING_DISCOUNT))


def test_unscoreable_when_comps_too_sparse():
    """Below MIN_COMPS_TO_SCORE, refuse to score — don't invent a number."""
    from deal_finder.appraisal.formula import MIN_COMPS_TO_SCORE
    comp = _comp(n=MIN_COMPS_TO_SCORE - 1, median=200.0, trimmed_median=200.0)
    s = compute_score(asking_price=150.0, comp=comp)
    assert s.unscoreable is True
    assert s.deal_score is None
    assert s.unscoreable_reason is not None
    assert "insufficient" in s.unscoreable_reason.lower()


def test_unscoreable_ignores_llm_param():
    """Even if a fair_value_from_llm is passed, sparse comps -> unscoreable.
    Formula v3.0 no longer falls back to the LLM."""
    comp = _comp(n=2, median=200.0, trimmed_median=200.0)
    s = compute_score(asking_price=150.0, comp=comp, fair_value_from_llm=170.0)
    assert s.unscoreable is True


def test_unscoreable_when_no_data_at_all():
    comp = CompStats(search_term="x", source="marketplace", sample_size=0)
    s = compute_score(asking_price=100.0, comp=comp)
    assert s.unscoreable is True
    assert s.deal_score is None


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


def test_unscoreable_below_threshold_remains_unscored():
    """Old 'GE AC motor' case (3 comps): now unscoreable, no number."""
    from deal_finder.appraisal.formula import MIN_COMPS_TO_SCORE
    comp = _comp(n=MIN_COMPS_TO_SCORE - 2, median=47.5,
                 trimmed_median=47.5, iqr=20.0)
    s = compute_score(asking_price=15.0, comp=comp)
    assert s.unscoreable is True
    assert s.deal_score is None


# --- Confidence cap on the score ----------------------------------------

def test_high_confidence_does_not_cap_legitimate_unicorns():
    """Plenty of tight comps + asking well below the range — should
    score near max (capped only by ±5 high-confidence interval)."""
    comp = _comp(n=14, median=400.0, trimmed_median=400.0, iqr=40.0)
    # _comp helper sets min = median * 0.5 = 200; asking $120 < min.
    s = compute_score(asking_price=120.0, comp=comp)
    assert s.unscoreable is False
    assert s.deal_score >= 95
    assert s.confidence_label == "high"


def test_cap_does_not_inflate_low_scores_when_scoreable():
    """An overpriced asking still scores low when comps are sufficient."""
    comp = _comp(n=10, median=200.0, trimmed_median=200.0, iqr=80.0)
    s = compute_score(asking_price=500.0, comp=comp)
    assert s.unscoreable is False
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
