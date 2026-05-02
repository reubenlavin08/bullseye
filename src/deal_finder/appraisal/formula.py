"""Deterministic deal-scoring math.

Same inputs always produce the same outputs. No LLM, no randomness.
Every score that hits the DB has a fully-reproducible breakdown that
the trust UI can render.

Design choices baked in:

1. Asking-vs-sold discount (default 20%)
   Marketplace asking prices typically run 15-30% above what items
   actually sell for. We anchor `fair_value` on the trimmed comp
   median, then apply this discount. Calibrated from public estimates;
   easy to retune as we collect real sale data.

2. Outlier handling
   The scorer uses `trimmed_median` from CompStats (Tukey fences).
   See `db.comps._compute_stats` for the trim policy.

3. Score curve
   Piecewise-linear function of `ratio = asking / fair_value`.
   - ratio = 0.4 -> score 100  (half-off+ unicorn)
   - ratio = 0.7 -> score 75   (good deal)
   - ratio = 1.0 -> score 50   (asking = fair value, neutral)
   - ratio = 1.3 -> score 25   (overpriced)
   - ratio = 1.7 -> score 5    (very overpriced)
   Smooth between breakpoints; clamped to [0, 100].

4. Confidence interval
   Score uncertainty is widened when:
   - sample_size is small
   - IQR is wide relative to the median (high variance)
   - We had to fall back to LLM-only fair_value (no comps)
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
    `listings.appraisal_breakdown` so any past score can be re-derived."""
    asking_price: float
    fair_value: float
    fair_value_source: str       # "trimmed_median*0.8" | "llm" | "raw_median*0.8"
    ratio: float                  # asking / fair_value
    deal_score: int               # 0-100, after curve mapping
    confidence_pm: int            # ± width on the score
    confidence_label: str         # "high" | "medium" | "low"
    sample_size: int
    trimmed_sample_size: int | None
    outliers_dropped: int
    median: float | None
    trimmed_median: float | None
    iqr: float | None
    asking_discount: float
    formula_version: str = "1.0"


# --- Public API -----------------------------------------------------------

def compute_score(
    *,
    asking_price: float,
    comp: CompStats,
    fair_value_from_llm: float | None = None,
    asking_discount: float = DEFAULT_ASKING_DISCOUNT,
) -> ScoreBreakdown:
    """Compute the deterministic deal score for a listing.

    `fair_value_from_llm` is only consulted when comps are too sparse
    to anchor on (sample_size < LLM_FALLBACK_THRESHOLD). When comps are
    sufficient, the score is purely a function of the comp stats and
    the asking price.

    Returns a ScoreBreakdown with full provenance. Raises ValueError on
    nonsensical inputs (asking_price <= 0, no comps AND no LLM estimate).
    """
    if asking_price <= 0:
        raise ValueError(
            f"asking_price must be positive (got {asking_price})"
        )

    fair_value, source = _resolve_fair_value(
        comp=comp,
        fair_value_from_llm=fair_value_from_llm,
        asking_discount=asking_discount,
    )
    if fair_value is None or fair_value <= 0:
        raise ValueError("could not estimate a positive fair_value")

    ratio = asking_price / fair_value
    raw_score = _curve(ratio, SCORE_CURVE)

    confidence_pm, confidence_label = _confidence(comp, source)

    # Cap the score by confidence — we can never claim a deal score
    # higher than `100 - confidence_pm` because that's the upper bound
    # of the confidence interval. Saying "100 ±18" implies the real
    # score could be as low as 82, so we report 82 instead. This is
    # how statisticians report uncertain estimates: stay inside the
    # interval. Keeps the system honest on niche items where comp
    # sample is thin (a $15 part with ratio 0.4 won't score 100 if
    # we only had 3 comps to work from).
    confidence_cap = 100 - confidence_pm
    capped = min(raw_score, confidence_cap)
    deal_score = max(0, min(100, int(round(capped))))

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
    )


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
    fair_value_from_llm: float | None,
    asking_discount: float,
) -> tuple[float | None, str]:
    """Pick the best available fair-value estimate. Returns (value, source)."""
    if comp.sample_size >= LLM_FALLBACK_THRESHOLD and \
       comp.trimmed_median is not None:
        return comp.trimmed_median * (1 - asking_discount), \
               f"trimmed_median*{1-asking_discount:.2f}"

    if fair_value_from_llm is not None and fair_value_from_llm > 0:
        return float(fair_value_from_llm), "llm"

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
