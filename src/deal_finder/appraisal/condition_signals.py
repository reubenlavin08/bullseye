"""Condition-signal extraction for category-aware score adjustment.

The percentile-rank score answers "how cheap is this listing relative
to similar ones." It does NOT know that a 2010 Civic with 'needs new
brakes' or 'frame damage' is worth less than the same year/model in
clean condition. This module fills that gap.

Approach:
  1. Use the small LLM (llama3.2:3B) to extract a fixed, finite set of
     binary condition flags from the listing description.
  2. Each flag has a known, fixed adjustment factor (e.g. needs_repair
     = −15 score points).
  3. Sum of factors is the score adjustment, applied linearly to the
     percentile-derived score.

Why this approach:
  * Stays deterministic — same flags + same factors = same adjustment.
  * Defensible — every adjustment is visible in the breakdown.
  * Cheap — one LLM call per scoreable listing.
  * The LLM's only job is binary extraction (something it does well);
    it is NOT picking the score directly.

Adjustment factors are calibrated by intuition for now. Once we have
real sale-price data (eBay sold comps), we can recalibrate from
ground truth.
"""
from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass, field

from .ollama_client import OllamaClient, get_default_client

logger = logging.getLogger(__name__)


DEFAULT_MODEL = os.environ.get(
    "OLLAMA_NORMALIZER_MODEL", "llama3.2:3b-instruct-q4_K_M",
)


# Score adjustments per flag, in raw score points (additive).
#
# These get summed and added to the percentile-based score. Negatives
# are penalties (item is worse than median comp), positives are bonuses
# (item is better than median comp).
SCORE_ADJUSTMENTS: dict[str, int] = {
    # Negative signals (issues / wear)
    "needs_repair":      -15,   # explicit issues: brakes, transmission, etc.
    "accident_history":  -20,   # crash, frame damage, rebuild
    "salvage_title":     -25,   # branded title, severe history
    "high_mileage":      -10,   # 150k+ miles cars; >5yr daily use other goods
    "cosmetic_damage":   -5,    # dents, scratches, fading
    "missing_parts":     -8,    # incomplete, missing accessories
    # Positive signals (above-typical condition)
    "excellent_condition": +5,  # mint, like new, barely used
    "has_warranty":      +3,    # transferable warranty included
    "low_use":           +5,    # garage kept, low miles for age
    "recently_serviced": +3,    # new tires, recent oil change, fresh detail
}

# Cap the cumulative adjustment so a worst-case listing isn't dragged
# below 0 by stacking flags. Score is also clamped to [0, 100] later.
MAX_NEGATIVE_ADJ = -35
MAX_POSITIVE_ADJ = +10


@dataclass
class ConditionSignals:
    """Extracted condition flags + their net score adjustment."""
    needs_repair: bool = False
    accident_history: bool = False
    salvage_title: bool = False
    high_mileage: bool = False
    cosmetic_damage: bool = False
    missing_parts: bool = False
    excellent_condition: bool = False
    has_warranty: bool = False
    low_use: bool = False
    recently_serviced: bool = False
    # Net score adjustment in points (sum, clamped)
    score_adjustment: int = 0
    # Which flags actually fired (for the trust UI)
    flags_fired: list[str] = field(default_factory=list)
    # Free-form note from the LLM (one short sentence, optional)
    note: str = ""


SYSTEM_PROMPT = """You extract condition signals from a Facebook Marketplace listing description.

Read the description and return a JSON object with one boolean per
signal. Be conservative — only flag a signal when the description
clearly states or strongly implies it.

Signals:

NEGATIVE (issues, wear, damage):
  needs_repair          - any mechanical/functional issue mentioned (brakes,
                          engine, transmission, electrical, "needs work")
  accident_history      - past accident, collision, frame damage, "rebuilt"
  salvage_title         - branded title (salvage, rebuilt, flood, lemon)
  high_mileage          - >150,000 miles on a car, OR explicit mention
                          like "high mileage", "lots of use"
  cosmetic_damage       - dents, scratches, fading, peeling, cracks (not
                          functional, just appearance)
  missing_parts         - parts missing, incomplete, no accessories,
                          "as is", "for parts"

POSITIVE (above-typical condition):
  excellent_condition   - explicit superlatives: "mint", "like new",
                          "barely used", "showroom"
  has_warranty          - active manufacturer warranty, transferable
                          extended warranty
  low_use               - "garage kept", "low miles for age",
                          "rarely driven", "barely ridden"
  recently_serviced     - new tires, fresh oil change, recent tune-up,
                          recent detail, recent inspection

Rules:
- Output ONLY this JSON object, no other text.
- Each value is true or false.
- "note" is a short sentence (max 15 words) summarizing the condition.
- Default to false when ambiguous. Don't infer.

OUTPUT FORMAT (exact):
{
  "needs_repair": false,
  "accident_history": false,
  "salvage_title": false,
  "high_mileage": false,
  "cosmetic_damage": false,
  "missing_parts": false,
  "excellent_condition": false,
  "has_warranty": false,
  "low_use": false,
  "recently_serviced": false,
  "note": "<one short sentence>"
}
"""


def extract_condition_signals(
    description: str | None,
    *,
    client: OllamaClient | None = None,
    model: str = DEFAULT_MODEL,
) -> ConditionSignals:
    """Extract condition flags from a description.

    Returns ConditionSignals with score_adjustment computed. On LLM
    failure or empty description, returns the default (no flags fired,
    score_adjustment=0) — caller can treat that as "no condition info,
    no adjustment."
    """
    if not description or not description.strip():
        return ConditionSignals()

    cli = client or get_default_client()
    desc = description[:2000]  # cap prompt size; flags should be near top
    try:
        resp = cli.generate_json(
            model=model,
            system=SYSTEM_PROMPT,
            user=f"Description:\n{desc}",
            temperature=0.0,
            num_predict=200,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("condition extract failed: %s", e)
        return ConditionSignals()

    parsed = resp.parsed
    if not isinstance(parsed, dict):
        logger.warning(
            "condition extract returned bad shape: %s", resp.raw_text[:200],
        )
        return ConditionSignals()

    # Build the dataclass from the JSON, defaulting any missing key to False.
    flags = {k: bool(parsed.get(k, False)) for k in SCORE_ADJUSTMENTS}
    note = str(parsed.get("note", "")).strip()[:200]

    # Sum adjustments for fired flags
    fired = [k for k, v in flags.items() if v]
    raw_adj = sum(SCORE_ADJUSTMENTS[k] for k in fired)

    # Clamp the net adjustment
    if raw_adj < MAX_NEGATIVE_ADJ:
        raw_adj = MAX_NEGATIVE_ADJ
    if raw_adj > MAX_POSITIVE_ADJ:
        raw_adj = MAX_POSITIVE_ADJ

    return ConditionSignals(
        needs_repair=flags["needs_repair"],
        accident_history=flags["accident_history"],
        salvage_title=flags["salvage_title"],
        high_mileage=flags["high_mileage"],
        cosmetic_damage=flags["cosmetic_damage"],
        missing_parts=flags["missing_parts"],
        excellent_condition=flags["excellent_condition"],
        has_warranty=flags["has_warranty"],
        low_use=flags["low_use"],
        recently_serviced=flags["recently_serviced"],
        score_adjustment=raw_adj,
        flags_fired=fired,
        note=note,
    )
