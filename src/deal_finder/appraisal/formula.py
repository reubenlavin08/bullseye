"""Deterministic deal-scoring math.

Same inputs always produce the same outputs. No LLM, no randomness.
Every score that hits the DB has a fully-reproducible breakdown that
the trust UI can render.

Design choices baked in:

1. Score = percentile rank of the listing in the comp asking distribution
   "Cheaper than X% of similar listings" → score X.
   Score 80 means asking sits at the 20th percentile of the comp set.
   Score 50 means asking sits near the median.
   Score 20 means asking sits at the 80th percentile (overpriced relative
   to the peer set).
   This is the most direct, statistically defensible meaning for a
   "deal score" — it doesn't require us to estimate any latent
   "fair value" number.

2. Fair value (kept as a SEPARATE display field, not part of score)
   We still estimate fair_value = trimmed_median × 0.80 because users
   want to know "what should I actually offer / pay." This is the
   asking-vs-sold discount: Marketplace asks typically run 15-30%
   above true sale prices.
   But fair_value no longer drives the score — percentile does.
   This fixes the "iPhone 15 at \$430 only scored 50 because the
   discount pushed fair value to \$432 and asking was right at it"
   problem. Now the score reflects "are you paying less than your
   peers" rather than "are you paying less than our derived number."

3. Confidence cap
   Score is capped at (100 - confidence_pm) — we never claim a deal
   score higher than the upper bound of the confidence interval.

4. Outlier handling
   The percentile rank uses the full comp distribution (Tukey fences
   inform `trimmed_median` for fair_value display, but percentile
   rank uses the full sample).

5. Bimodal cluster detection
   Upstream of this module — see db/comps._maybe_split_bimodal.
   By the time prices reach this formula they should already be
   from a coherent cluster.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..db.comps import CompStats


# --- Tunables -------------------------------------------------------------

# Asking-vs-sold discount applied to comp median to estimate true
# secondhand market value. Calibrate downward when eBay sold data lands.
DEFAULT_ASKING_DISCOUNT = 0.20

# Sample size below which we ask the LLM for a fair-value estimate
# instead of using comps directly.
LLM_FALLBACK_THRESHOLD = 5

# Score-curve breakpoints. (ratio, score) pairs, must be ordered by ratio.
# Linear interpolation between adjacent points; flat outside the range.
SCORE_CURVE: tuple[tuple[float, float], ...] = (
    (0.40, 100.0),
    (0.70, 75.0),
    (1.00, 50.0),
    (1.30, 25.0),
    (1.70, 5.0),
)


# --- Output dataclass -----------------------------------------------------

@dataclass
class ScoreBreakdown:
    """Full reproducible breakdown of one score. Persisted as JSON in
    `listings.appraisal_breakdown` so any past score can be re-derived.

    Note: when `unscoreable` is True the other numeric fields may be
    None or zero — caller should branch on `unscoreable` first.
    """
    asking_price: float
    fair_value: float | None
    fair_value_source: str       # "trimmed_median*0.8" | "raw_median*0.8" | "none"
    ratio: float | None           # asking / fair_value
    deal_score: int | None        # 0-100, or None when unscoreable
    confidence_pm: int            # ± width on the score
    confidence_label: str         # "high" | "medium" | "low" | "none"
    sample_size: int
    trimmed_sample_size: int | None
    outliers_dropped: int
    median: float | None
    trimmed_median: float | None
    iqr: float | None
    asking_discount: float
    # Data-quality flag for heterogeneous comp sets ("vintage X" cases
    # where every unit is unique). True when IQR > median, meaning the
    # comp distribution is too dispersed for the score to be reliable.
    data_quality_poor: bool = False
    iqr_to_median_ratio: float | None = None
    # Percentile rank of `asking_price` within the comp set. 0.0 = cheaper
    # than every comp; 1.0 = more expensive than every comp. As of v2.0
    # this is the PRIMARY driver of deal_score.
    percentile_rank: float | None = None
    # Condition-signal adjustment in points (-35 to +10). Subtracts from
    # the percentile-derived raw_score before confidence cap. Captures
    # "this Civic has brake issues, even though comps are clean."
    # See appraisal/condition_signals.py.
    condition_adjustment: int = 0
    condition_flags: list[str] | None = None
    condition_note: str | None = None
    # Score before the condition adjustment was applied — useful so the
    # UI can show "raw 95 - 15 (needs_repair) = 80".
    raw_score_pre_condition: int | None = None
    # When True, we refused to score this listing because comp data was
    # insufficient. UI should show "not enough comparable data" rather
    # than a misleading score. As of v3.0 we no longer fall back to
    # LLM hallucinations for fair_value when comps are sparse.
    unscoreable: bool = False
    unscoreable_reason: str | None = None
    formula_version: str = "4.0"


# Minimum comps required to trust the percentile rank. Below this we
# refuse to score rather than make stuff up.
MIN_COMPS_TO_SCORE = 5

# Categories that are condition-sensitive (high-value, condition can
# vary widely). For these, we apply a higher minimum confidence_pm
# because mileage / accident history / wear matter as much as the comp
# median, and we can't fully model that statistically.
# Map FB marketplace_listing_category_id -> minimum confidence_pm.
CATEGORY_MIN_CONFIDENCE_PM: dict[str, int] = {
    "807311116002614": 20,   # Cars / vehicles
    # Add real-estate, motorcycles, etc. here as we encounter them.
}


# IQR / median above this threshold flags the comp set as too varied
# for a reliable single-number deal score (e.g. "vintage electric
# fishing motor" where every unit is unique).
DATA_QUALITY_IQR_THRESHOLD = 1.0


# --- Public API -----------------------------------------------------------

def compute_score(
    *,
    asking_price: float,
    comp: CompStats,
    fair_value_from_llm: float | None = None,
    asking_discount: float = DEFAULT_ASKING_DISCOUNT,
    condition_adjustment: int = 0,
    condition_flags: list[str] | None = None,
    condition_note: str | None = None,
    category_id: str | None = None,
) -> ScoreBreakdown:
    """Compute the deterministic deal score for a listing.

    Refuses to score (returns ScoreBreakdown with unscoreable=True)
    when comp data is insufficient. As of formula v3.0 we no longer
    fall back to LLM-hallucinated fair_values — better to admit "not
    enough data" than to invent a number.

    `fair_value_from_llm` is accepted for backwards compatibility but
    not used to score; if provided, it'll be reflected in the breakdown
    for transparency only.

    Raises ValueError on nonsensical inputs (asking_price <= 0).
    """
    if asking_price <= 0:
        raise ValueError(
            f"asking_price must be positive (got {asking_price})"
        )

    # Refuse to score with insufficient sample.
    effective_n = comp.trimmed_sample_size or comp.sample_size
    if effective_n < MIN_COMPS_TO_SCORE:
        return _unscoreable(
            asking_price=asking_price,
            comp=comp,
            asking_discount=asking_discount,
            reason=(
                f"insufficient comparable listings "
                f"(found {comp.sample_size}, need {MIN_COMPS_TO_SCORE}+)"
            ),
        )

    # We have enough comps. fair_value is purely statistical now —
    # trimmed_median × asking-vs-sold discount. No LLM in this path.
    fair_value, source = _resolve_fair_value(
        comp=comp,
        asking_discount=asking_discount,
    )
    if fair_value is None or fair_value <= 0:
        return _unscoreable(
            asking_price=asking_price,
            comp=comp,
            asking_discount=asking_discount,
            reason="could not derive a positive fair value from comps",
        )

    ratio = asking_price / fair_value

    # Percentile rank of asking inside the comp asking distribution —
    # the score driver.
    pct_rank = _percentile_rank(asking_price, comp)
    if pct_rank is None:
        return _unscoreable(
            asking_price=asking_price, comp=comp,
            asking_discount=asking_discount,
            reason="comp distribution too narrow to rank",
        )

    confidence_pm, confidence_label = _confidence(comp, source)

    # Category-aware confidence floor: condition-sensitive categories
    # (vehicles, real estate) get a wider minimum interval because
    # mileage / accident history / wear shift true value as much as
    # the comp distribution does.
    if category_id and category_id in CATEGORY_MIN_CONFIDENCE_PM:
        floor_pm = CATEGORY_MIN_CONFIDENCE_PM[category_id]
        if confidence_pm < floor_pm:
            confidence_pm = floor_pm
            if confidence_pm <= 7:
                confidence_label = "high"
            elif confidence_pm <= 14:
                confidence_label = "medium"
            else:
                confidence_label = "low"

    raw_score = (1 - pct_rank) * 100
    raw_score_pre_condition = max(0, min(100, int(round(raw_score))))

    # Apply condition adjustment (e.g. -15 for "needs repair", +5 for
    # "excellent condition"). Linear, additive, deterministic.
    adjusted_raw = raw_score + condition_adjustment

    confidence_cap = 100 - confidence_pm
    capped = min(adjusted_raw, confidence_cap)
    deal_score = max(0, min(100, int(round(capped))))

    iqr_ratio = None
    data_quality_poor = False
    if comp.iqr is not None and comp.trimmed_median:
        iqr_ratio = comp.iqr / comp.trimmed_median
        if iqr_ratio > DATA_QUALITY_IQR_THRESHOLD:
            data_quality_poor = True

    return ScoreBreakdown(
        asking_price=asking_price,
        fair_value=fair_value,
        fair_value_source=source,
        ratio=ratio,
        deal_score=deal_score,
        confidence_pm=confidence_pm,
        confidence_label=confidence_label,
        sample_size=comp.sample_size,
        trimmed_sample_size=comp.trimmed_sample_size,
        outliers_dropped=comp.outliers_dropped,
        median=comp.median,
        trimmed_median=comp.trimmed_median,
        iqr=comp.iqr,
        asking_discount=asking_discount,
        data_quality_poor=data_quality_poor,
        iqr_to_median_ratio=iqr_ratio,
        percentile_rank=pct_rank,
        condition_adjustment=condition_adjustment,
        condition_flags=condition_flags or [],
        condition_note=condition_note,
        raw_score_pre_condition=raw_score_pre_condition,
    )


def _unscoreable(
    *,
    asking_price: float,
    comp: CompStats,
    asking_discount: float,
    reason: str,
) -> ScoreBreakdown:
    """Build a ScoreBreakdown that records why we refused to score.

    Some informational fields (fair_value, percentile rank) may still
    be populated when computable, so the UI can show whatever partial
    insight is available alongside the "not enough data" message.
    """
    fair_value = None
    if comp.trimmed_median and comp.trimmed_median > 0:
        fair_value = comp.trimmed_median * (1 - asking_discount)

    pct_rank = None
    if comp.median is not None and comp.sample_size > 0:
        pct_rank = _percentile_rank(asking_price, comp)

    iqr_ratio = None
    data_quality_poor = False
    if comp.iqr is not None and comp.trimmed_median:
        iqr_ratio = comp.iqr / comp.trimmed_median
        if iqr_ratio > DATA_QUALITY_IQR_THRESHOLD:
            data_quality_poor = True

    return ScoreBreakdown(
        asking_price=asking_price,
        fair_value=fair_value,
        fair_value_source="none",
        ratio=(asking_price / fair_value) if fair_value else None,
        deal_score=None,
        confidence_pm=0,
        confidence_label="none",
        sample_size=comp.sample_size,
        trimmed_sample_size=comp.trimmed_sample_size,
        outliers_dropped=comp.outliers_dropped,
        median=comp.median,
        trimmed_median=comp.trimmed_median,
        iqr=comp.iqr,
        asking_discount=asking_discount,
        data_quality_poor=data_quality_poor,
        iqr_to_median_ratio=iqr_ratio,
        percentile_rank=pct_rank,
        unscoreable=True,
        unscoreable_reason=reason,
    )


def _percentile_rank(value: float, comp: CompStats) -> float | None:
    """Approximate percentile rank using the available stats.

    True percentile rank requires the full sample, which we don't have
    here (CompStats is aggregated). We approximate using p10, q1,
    median, q3, p90 as anchor points and linearly interpolate.
    Returns None if we can't anchor anywhere.
    """
    anchors: list[tuple[float, float]] = []
    if comp.minimum is not None:
        anchors.append((comp.minimum, 0.0))
    if comp.p10 is not None:
        anchors.append((comp.p10, 0.10))
    if comp.q1 is not None:
        anchors.append((comp.q1, 0.25))
    if comp.median is not None:
        anchors.append((comp.median, 0.50))
    if comp.q3 is not None:
        anchors.append((comp.q3, 0.75))
    if comp.p90 is not None:
        anchors.append((comp.p90, 0.90))
    if comp.maximum is not None:
        anchors.append((comp.maximum, 1.0))
    if len(anchors) < 2:
        return None
    anchors.sort()
    if value <= anchors[0][0]:
        return 0.0
    if value >= anchors[-1][0]:
        return 1.0
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= value <= x1:
            if x1 == x0:
                return y0
            t = (value - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return None


def llm_needed(comp: CompStats) -> bool:
    """Should the worker call the big LLM for a fair_value estimate?

    Yes when comps are too sparse for the trimmed median to be meaningful.
    The worker uses this to skip the slow LLM call when we already have
    enough data.
    """
    if comp.sample_size < LLM_FALLBACK_THRESHOLD:
        return True
    if comp.trimmed_sample_size is not None and \
       comp.trimmed_sample_size < LLM_FALLBACK_THRESHOLD:
        return True
    return False


# --- Internals ------------------------------------------------------------

def _resolve_fair_value(
    *,
    comp: CompStats,
    asking_discount: float,
) -> tuple[float | None, str]:
    """Pick the best statistical fair-value estimate from comps.

    Prefers trimmed_median (post-outlier-trim, post-cluster-split) over
    raw median. Both apply the asking-vs-sold discount. No LLM path
    here — caller has already verified comp count is sufficient.
    """
    if comp.trimmed_median is not None:
        return comp.trimmed_median * (1 - asking_discount), \
               f"trimmed_median*{1-asking_discount:.2f}"

    if comp.median is not None and comp.sample_size > 0:
        return comp.median * (1 - asking_discount), \
               f"raw_median*{1-asking_discount:.2f}"

    return None, "none"


def _curve(x: float, points: tuple[tuple[float, float], ...]) -> float:
    """Piecewise-linear interpolation. Flat outside the breakpoint range."""
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            t = (x - x0) / (x1 - x0)
            return y0 + (y1 - y0) * t
    return points[-1][1]  # unreachable but satisfies type-checker


def _confidence(comp: CompStats, fv_source: str) -> tuple[int, str]:
    """Return (confidence_pm, confidence_label).

    `confidence_pm` is the ± width on the score, on a 0-100 scale.
    Wider is less confident. Driven by sample size, IQR width relative
    to the median, and whether we had to fall back to LLM-only.
    """
    if fv_source == "llm" or fv_source == "none":
        return 20, "low"

    n = comp.trimmed_sample_size or comp.sample_size
    if n >= 12:
        base = 5
    elif n >= 8:
        base = 8
    elif n >= 5:
        base = 12
    else:
        base = 18

    # Penalty for high variance: IQR > 50% of median means the comps are
    # all over the place and the median is less reliable.
    if comp.iqr is not None and comp.median and comp.median > 0:
        iqr_ratio = comp.iqr / comp.median
        if iqr_ratio > 0.8:
            base += 8
        elif iqr_ratio > 0.5:
            base += 4

    if base <= 7:
        label = "high"
    elif base <= 14:
        label = "medium"
    else:
        label = "low"
    return base, label
