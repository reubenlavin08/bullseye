"""Condition-signal extraction for category-aware score adjustment.

The percentile-rank score answers "how cheap is this listing relative
to similar ones." It does NOT know that a 2010 Civic with 'needs new
brakes' or 'frame damage' is worth less than the same year/model in
clean condition. This module fills that gap.

Hybrid extractor — both signals fire if EITHER source detects them:

  REGEX BANK   (deterministic, fast, free) catches explicit phrasing
               like "small dent", "loud noise when braking", "scratches",
               "won't start", "blown speaker", etc. Hundreds of patterns
               organized by flag.

  SMALL LLM    (llama3.2:3B, JSON output) catches novel phrasings the
               regex misses ("starts on cold mornings sometimes",
               "took it in for the rumble").

Each flag still has a fixed adjustment factor; final score adjustment
is the SUM of fired flags (clamped to [-35, +10]). Math stays
deterministic — the LLM only does binary extraction, never picks the
score.
"""
from __future__ import annotations

import logging
import os
import re
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


# --- Regex pattern bank --------------------------------------------------
#
# Each flag maps to a list of compiled regex patterns. A flag fires if
# ANY pattern matches the description (case-insensitive). These are
# tuned to catch common Marketplace phrasing across vehicles,
# electronics, appliances, and furniture.
#
# Add cautiously: false positives drag down legitimate listings. When
# in doubt, prefer letting the LLM catch it.

_RAW_PATTERNS: dict[str, list[str]] = {
    "needs_repair": [
        # Direct mentions
        r"\bneeds?\s+(?:new\s+)?(?:repair|fix|fixing|servic|work|tune)",
        r"\b(?:needs?|requires?)\s+(?:a\s+)?(?:new|replacement)\s+\w+",
        r"\b(?:as[- ]is|sold\s+as[- ]is)\b",
        r"\bnot\s+(?:working|functional|running|charging)\b",
        r"\bdoesn'?t\s+(?:work|run|start|charge|hold)\b",
        r"\bwon'?t\s+(?:start|charge|turn|run|boot)\b",
        # Audible / mechanical issues
        r"\b(?:loud|strange|odd|weird|grinding|knocking|clunk\w*|rattl\w*|whirr\w*)\s+(?:noise|sound)",
        r"\bnoise\s+when\s+(?:braking|driving|turning|accelerating)",
        r"\b(?:engine|transmission|brake|clutch|alternator|starter|battery)\s+(?:issue|problem|trouble)",
        r"\bcheck\s+engine\s+(?:light|on)\b",
        r"\bleak\w*\b.*\b(?:oil|coolant|fluid|gas|radiator|transmission)",
        r"\b(?:oil|coolant|fluid|gas|radiator|transmission)\s+leak\w*",
        r"\b(?:slipping|slips|slipped)\s+(?:transmission|gear|clutch)",
        # Battery / charging
        r"\b(?:battery|charging\s+port|usb-?c\s+port)\s+(?:issue|problem|won'?t\s+hold)",
        r"\bdoesn'?t\s+hold\s+(?:a\s+)?charge\b",
        # Generic wear
        r"\bbroken\s+\w+",
        r"\b\w+\s+(?:is|are)\s+broken\b",
        # Electronics issues
        r"\b(?:cracked|shattered)\s+(?:screen|display|lcd)",
        r"\b(?:dead|stuck)\s+(?:pixel|key)",
        r"\b(?:burn[- ]?in|ghosting)\b",
        r"\bbattery\s+(?:swollen|bulg\w+|degraded)",
    ],
    "accident_history": [
        r"\b(?:minor|prior|previous|been\s+in)\s+(?:an?\s+)?accident",
        r"\baccident\s+(?:history|damage|on\s+report)",
        r"\b(?:was|been)\s+in\s+a?\s*(?:fender[- ]bender|crash|collision|wreck)",
        r"\b(?:rear|front|side)[- ]ended",
        r"\b(?:body|frame|chassis)\s+(?:damage|repair)",
        r"\bcollision\s+(?:repair|damage)",
    ],
    "salvage_title": [
        r"\bsalvage(?:\s+title)?\b",
        r"\brebuilt(?:\s+title)?\b",
        r"\b(?:branded|reconstructed|flood|lemon)\s+title\b",
        r"\bfire\s+damaged\b",
    ],
    "high_mileage": [
        # Cars: 150k+ miles or km
        r"\b(?:1[5-9]\d|[2-9]\d\d|\d{3,})\s*,?\s*\d{3}\s*(?:miles|mi|km|kms)\b",
        r"\b(?:150|175|200|225|250|300|350|400)\s*k\s*(?:miles|km)?\b",
        r"\bhigh\s+(?:mileage|miles|km)\b",
        r"\bdaily\s+driver\b",
    ],
    "cosmetic_damage": [
        r"\b(?:small|minor|few|some|tiny)\s+(?:dents?|scratches?|scrapes?|dings?|chips?|nicks?)",
        r"\b(?:dents?|scratches?|scrapes?|dings?|chips?|nicks?)\s+(?:here\s+and\s+there|on\s+\w+)",
        r"\bcosmetic\s+(?:damage|wear|issues?)",
        r"\bpaint\s+(?:scratch\w*|chip\w*|fad\w*|peel\w*|damage)",
        r"\b(?:fad\w+|peel\w+|cracked|chipped)\s+\w+",
        r"\b(?:rust|rusty|rusted|corrosion)\b",
        r"\b(?:bumper|fender|door|hood|trunk|panel)\s+(?:dent|scratch|scrape|damage)",
        r"\b(?:scuff|scuffs|scuffed)\b",
        r"\bsurface\s+(?:rust|corrosion)",
    ],
    "missing_parts": [
        r"\bmissing\s+\w+",
        r"\b(?:no|without)\s+(?:charger|cable|remote|stand|cord|battery|key|manual|box)",
        r"\b(?:incomplete|partial)\s+(?:set|unit)",
        r"\bfor\s+parts\s+only",
        r"\bdoes\s+not\s+come\s+with",
    ],
    # ---- Positive signals ----
    "excellent_condition": [
        r"\bmint\s+condition\b",
        r"\b(?:like|as)\s+new\b",
        r"\bbarely\s+(?:used|driven|ridden|worn|touched)\b",
        r"\bshowroom\s+(?:condition|new)\b",
        r"\bperfect\s+condition\b",
        r"\bpristine\b",
        r"\bnever\s+used\b",
        r"\bbrand\s+new\b",
    ],
    "has_warranty": [
        r"\b(?:active|valid|remaining|transferable)\s+warranty",
        r"\bunder\s+warranty\b",
        r"\bextended\s+warranty\s+(?:included|valid|active)",
        r"\bwarranty\s+(?:until|valid\s+until|good\s+until|active)",
    ],
    "low_use": [
        r"\b(?:garage|barn)\s+kept\b",
        r"\blow\s+(?:miles|mileage|hours)\b",
        r"\b(?:rarely|seldom|hardly)\s+(?:used|driven|ridden|run)",
        r"\b(?:single|one)\s+owner\b",
        r"\boriginal\s+(?:owner|miles)",
    ],
    "recently_serviced": [
        r"\bnew(?:ly)?\s+(?:tire|battery|brake|clutch|oil|filter|spark plug)",
        r"\brecent(?:ly)?\s+(?:service|tune[- ]up|oil change|inspect)",
        r"\bjust\s+(?:serviced|tuned|inspected|detailed|cleaned)",
        r"\bfresh\s+(?:oil|tune|inspect|detail|paint)",
        r"\bdetailed\s+(?:last\s+week|recently)",
    ],
}


# Compile once at import time.
_PATTERNS: dict[str, list[re.Pattern]] = {
    flag: [re.compile(p, re.IGNORECASE) for p in patterns]
    for flag, patterns in _RAW_PATTERNS.items()
}


def _regex_extract(description: str) -> dict[str, list[str]]:
    """Return {flag_name: [matched_pattern_strings]} for everything that
    fires. Empty dict if nothing matched."""
    out: dict[str, list[str]] = {}
    for flag, patterns in _PATTERNS.items():
        hits = []
        for pat in patterns:
            m = pat.search(description)
            if m:
                hits.append(m.group(0))
        if hits:
            out[flag] = hits
    return out


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
    use_llm: bool = True,
) -> ConditionSignals:
    """Extract condition flags from a description.

    Hybrid: regex bank fires first (deterministic, free). LLM runs
    second to catch novel phrasings the regex missed. Final flag set
    is the UNION — a flag fires if EITHER source detects it.

    Returns ConditionSignals with score_adjustment computed. On LLM
    failure or empty description, returns whatever regex caught (or
    a default if regex caught nothing).
    """
    if not description or not description.strip():
        return ConditionSignals()

    desc = description[:2000]  # cap; flags should be near the top anyway

    # 1. Regex pass (always runs, fast, free)
    regex_hits = _regex_extract(desc)
    flags = {k: (k in regex_hits) for k in SCORE_ADJUSTMENTS}

    # 2. LLM pass (optional — catches what regex missed)
    note = ""
    llm_added: list[str] = []
    if use_llm:
        cli = client or get_default_client()
        try:
            resp = cli.generate_json(
                model=model,
                system=SYSTEM_PROMPT,
                user=f"Description:\n{desc}",
                temperature=0.0,
                num_predict=200,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("condition LLM call failed: %s", e)
            resp = None

        if resp is not None and isinstance(resp.parsed, dict):
            parsed = resp.parsed
            note = str(parsed.get("note", "")).strip()[:200]
            for flag in SCORE_ADJUSTMENTS:
                if bool(parsed.get(flag, False)) and not flags[flag]:
                    flags[flag] = True
                    llm_added.append(flag)

    # Sum adjustments for fired flags
    fired = [k for k, v in flags.items() if v]
    raw_adj = sum(SCORE_ADJUSTMENTS[k] for k in fired)

    # Clamp the net adjustment
    if raw_adj < MAX_NEGATIVE_ADJ:
        raw_adj = MAX_NEGATIVE_ADJ
    if raw_adj > MAX_POSITIVE_ADJ:
        raw_adj = MAX_POSITIVE_ADJ

    if fired and (regex_hits or llm_added):
        # Auto-build a note showing what fired and how it was detected
        # if the LLM didn't supply one.
        if not note:
            srcs = []
            for f in fired:
                if f in regex_hits and f in llm_added:
                    srcs.append(f"{f}(regex+llm)")
                elif f in regex_hits:
                    srcs.append(f"{f}(regex)")
                else:
                    srcs.append(f"{f}(llm)")
            note = "Detected: " + ", ".join(srcs)

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
