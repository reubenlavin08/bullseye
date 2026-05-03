"""LLM secondary-check for anomalous high-score listings.

The statistical scorer is great at "this asking price is much lower than
comps." It's BAD at distinguishing a real bargain from a misleading
listing — examples we've seen miss:

  * "$40 car r3ntal" — uses '3' instead of 'e' to dodge our regex
    rental-rejector. Looks like a $40 car. Is actually $40/month.
  * "$1 macbook" — listed as $1 to attract clicks; real price is in
    the description ("$1200 OBO"). We try to recover with regex, but
    when we miss, the score is still computed against $1.
  * "$50 RTX 4090" — fake / scam listing. Comp median for an RTX 4090
    is $1500, so this scores 99/100 on raw stats.
  * "$200 'parts' for car X" — partial item. Whole-car comps make it
    look like a steal.
  * Fake / placeholder bait listings designed to harvest clicks.

When a listing scores anomalously high, we send the title + description
+ asking + comp summary to a small local LLM and ask "is this
legitimate, or is something off?" The LLM has more world-knowledge than
our regex bank and catches obvious red flags humans would.

Trigger logic (in jobs.py at score time):
  * deal_score >= LLM_VERIFY_HIGH_SCORE        (default 85)
  * OR (deal_score >= 70 AND confidence == 'low')

Disable entirely with LLM_SECONDARY_CHECK=0 in .env (e.g. when Ollama
is offline or you just want raw stats).

Latency: ~1-3s per check on llama3.2:3b. We only call it for high-score
candidates, which is <5% of appraised listings, so the budget cost is
small.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .ollama_client import get_default_client

logger = logging.getLogger(__name__)


SECONDARY_CHECK_ENABLED = os.environ.get("LLM_SECONDARY_CHECK", "1") not in ("0", "")
LLM_VERIFY_HIGH_SCORE = int(os.environ.get("LLM_VERIFY_HIGH_SCORE", "85"))
LLM_VERIFY_LOW_CONF_THRESHOLD = int(os.environ.get("LLM_VERIFY_LOW_CONF_THRESHOLD", "70"))
SECONDARY_CHECK_MODEL = os.environ.get(
    "OLLAMA_SECONDARY_MODEL",
    os.environ.get("OLLAMA_APPRAISAL_MODEL", "llama3.2:3b-instruct-q4_K_M"),
)


@dataclass
class VerifyResult:
    """Outcome of the secondary check.

    verdict: 'legit' | 'suspect' | 'uncertain' | 'skipped'
      legit     — LLM thinks it's a real bargain matching its title
      suspect   — LLM flags concrete red flags (rental, financing, etc.)
      uncertain — LLM isn't sure either way
      skipped   — secondary check disabled or LLM unavailable

    concern: short human-readable reason, or None if legit/skipped.
    confidence: LLM's stated confidence in its own verdict.
    elapsed_s: how long the check took.
    """
    verdict: str
    concern: str | None
    confidence: str | None
    elapsed_s: float
    model: str | None


def should_verify(*, deal_score: int | None, confidence_label: str | None) -> bool:
    """Decide if a scored listing warrants a secondary LLM check.

    Two trigger conditions:
      1. Score >= LLM_VERIFY_HIGH_SCORE — anomalously good deals are
         the population most likely to contain hidden gotchas.
      2. Score >= LOW_CONF_THRESHOLD AND confidence == 'low' — the
         statistical confidence is weak so we want a second opinion
         before alerting the user.
    """
    if not SECONDARY_CHECK_ENABLED:
        return False
    if deal_score is None:
        return False
    if deal_score >= LLM_VERIFY_HIGH_SCORE:
        return True
    if (
        deal_score >= LLM_VERIFY_LOW_CONF_THRESHOLD
        and confidence_label == "low"
    ):
        return True
    return False


_SYSTEM_PROMPT = """You are a careful, skeptical second-opinion reviewer for a Facebook Marketplace deal-finder.

The deal-finder uses statistical comparison against comparable listings to score listings. It has flagged this listing as a "great deal" because the asking price is much lower than the median of comparable items.

Your job is to spot when a listing scores high for the WRONG REASONS. Common gotchas:

- The asking price is for RENT or LEASE per month, not a sale. (Look for: "/mo", "per month", "lease", "weekly", "rental", words like "r3nt" with leetspeak.)
- The asking price is for a DEPOSIT or DOWN PAYMENT, not full price. (Look for: "deposit", "down payment", "/biweekly", "OAC", financing language.)
- The listing is for PARTS / a single component, not the whole item. (Look for: "for parts", "parting out", "frame only", "no engine".)
- The item is a TOY, REPLICA, or KNOCK-OFF being compared to the real thing. (Look for: "barbie", "doll", "toy", "1:18 scale", "replica".)
- The listing is a SCAM / FAKE / bait listing — title doesn't match description, suspicious vibe.
- The price is for SHIPPING, VIEWING, or DELIVERY only, not the item.
- The item is the WRONG GENERATION / model from what the title implies (e.g. "iPhone" but it's an iPhone 5 in 2026).
- "OBO" / "name your price" / "send offers" — the listed price isn't the real price.

If NONE of these apply and the listing genuinely appears to be the real item at the listed price (just a good deal), return verdict "legit".

If you SEE a concrete red flag, return "suspect" with a short concern.

If the description doesn't give you enough to tell, return "uncertain".

Always respond with valid JSON in this exact shape:
{
  "verdict": "legit" | "suspect" | "uncertain",
  "concern": "<one short sentence if suspect/uncertain, else null>",
  "confidence": "high" | "medium" | "low"
}
"""


def _build_user_prompt(
    *, title: str, description: str, asking_price: float,
    comp_median: float | None, comp_sample_size: int | None,
    deal_score: int,
) -> str:
    desc_excerpt = (description or "").strip()
    if len(desc_excerpt) > 1500:
        desc_excerpt = desc_excerpt[:1500] + "...[truncated]"

    comp_line = (
        f"Comparable listings on Marketplace median ${comp_median:.0f} "
        f"(n={comp_sample_size or 0} comps)"
        if comp_median is not None
        else "No comp data available."
    )
    ratio = (
        f"asking is {asking_price / comp_median * 100:.0f}% of comp median"
        if comp_median and comp_median > 0
        else ""
    )
    if ratio:
        comp_line += f" — {ratio}"

    return f"""LISTING UNDER REVIEW:

Title: {title}
Asking price: ${asking_price:.0f}

Description:
{desc_excerpt or "(no description)"}

COMP DATA:
{comp_line}

Statistical score: {deal_score}/100 (higher = better deal vs. comps)

Question: Based on the title and description, is this a legitimate good deal at the listed asking price, or is something obfuscated (rental, financing, parts-only, replica, scam, etc.)? Respond in JSON."""


def verify_listing(
    *,
    title: str,
    description: str | None,
    asking_price: float,
    comp_median: float | None,
    comp_sample_size: int | None,
    deal_score: int,
    confidence_label: str | None = None,
) -> VerifyResult:
    """Run the secondary-check LLM and parse the result.

    Returns VerifyResult.verdict='skipped' if LLM is unavailable or
    secondary check is disabled — caller should treat that as
    "verification did not happen, use the original score."
    """
    if not SECONDARY_CHECK_ENABLED:
        return VerifyResult("skipped", None, None, 0.0, None)

    user_prompt = _build_user_prompt(
        title=title or "(no title)",
        description=description or "",
        asking_price=asking_price,
        comp_median=comp_median,
        comp_sample_size=comp_sample_size,
        deal_score=deal_score,
    )

    try:
        resp = get_default_client().generate_json(
            model=SECONDARY_CHECK_MODEL,
            system=_SYSTEM_PROMPT,
            user=user_prompt,
            temperature=0.1,   # we want consistent verdicts, not creativity
            num_predict=256,
        )
    except Exception as e:  # noqa: BLE001 — never let LLM failure block the pipeline
        logger.warning("secondary-check LLM call failed: %s", e)
        return VerifyResult("skipped", None, None, 0.0, None)

    parsed = resp.parsed or {}
    raw_verdict = str(parsed.get("verdict") or "uncertain").lower()
    if raw_verdict not in ("legit", "suspect", "uncertain"):
        raw_verdict = "uncertain"
    concern = parsed.get("concern")
    if concern is not None and not isinstance(concern, str):
        concern = str(concern)
    if concern:
        concern = concern.strip()[:300]
    raw_conf = str(parsed.get("confidence") or "").lower()
    if raw_conf not in ("high", "medium", "low"):
        raw_conf = None  # type: ignore[assignment]

    return VerifyResult(
        verdict=raw_verdict,
        concern=concern if raw_verdict != "legit" else None,
        confidence=raw_conf,
        elapsed_s=resp.elapsed_s,
        model=resp.model,
    )
