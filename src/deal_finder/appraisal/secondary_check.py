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

from . import minimax_client
from .ollama_client import get_default_client

logger = logging.getLogger(__name__)


SECONDARY_CHECK_ENABLED = os.environ.get("LLM_SECONDARY_CHECK", "1") not in ("0", "")
LLM_VERIFY_HIGH_SCORE = int(os.environ.get("LLM_VERIFY_HIGH_SCORE", "85"))
LLM_VERIFY_LOW_CONF_THRESHOLD = int(os.environ.get("LLM_VERIFY_LOW_CONF_THRESHOLD", "70"))
SECONDARY_CHECK_MODEL = os.environ.get(
    "OLLAMA_SECONDARY_MODEL",
    os.environ.get("OLLAMA_APPRAISAL_MODEL", "llama3.2:3b-instruct-q4_K_M"),
)

# Cloud escalation thresholds — when do we promote a Tier-1 (Ollama)
# verdict to a Tier-2 (MiniMax cloud) check? Designed to use the cloud
# sparingly:
#   - score >= LLM_CLOUD_FORCE_SCORE   → ALWAYS escalate (paranoia tier)
#   - tier-1 verdict 'uncertain'       → escalate (model couldn't decide)
#   - score >= LLM_CLOUD_LOWCONF_SCORE AND confidence='low' → escalate
LLM_CLOUD_FORCE_SCORE = int(os.environ.get("LLM_CLOUD_FORCE_SCORE", "95"))
LLM_CLOUD_LOWCONF_SCORE = int(os.environ.get("LLM_CLOUD_LOWCONF_SCORE", "90"))


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
    backend: 'ollama' | 'minimax' | None — which model produced the
      final verdict; useful for the dashboard to know when MiniMax
      was consulted vs. when local was sufficient.
    """
    verdict: str
    concern: str | None
    confidence: str | None
    elapsed_s: float
    model: str | None
    backend: str | None = None


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


def _verify_with_ollama(
    *, title: str, description: str | None, asking_price: float,
    comp_median: float | None, comp_sample_size: int | None,
    deal_score: int,
) -> VerifyResult:
    """Tier-1 check via local Ollama. Always free; ~2-5s per call."""
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
            temperature=0.1,
            num_predict=256,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("ollama secondary-check failed: %s", e)
        return VerifyResult("skipped", None, None, 0.0, None, "ollama")

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
        backend="ollama",
    )


def _should_escalate_to_cloud(
    *, tier1_verdict: str, deal_score: int, confidence_label: str | None,
) -> tuple[bool, str]:
    """Decide whether to spend a MiniMax call confirming Tier-1's
    verdict. Returns (should_escalate, reason).

    Triggers (any one is enough):
      1. tier1 returned 'uncertain' — local model couldn't decide
      2. deal_score >= LLM_CLOUD_FORCE_SCORE — paranoia tier (default 95)
      3. deal_score >= LLM_CLOUD_LOWCONF_SCORE AND confidence='low' —
         high score + sparse data is the classic false-positive zone
    """
    if not minimax_client.available():
        return False, "minimax unavailable (no API key)"
    if minimax_client.daily_budget_remaining() <= 0:
        return False, "minimax daily budget exhausted"
    if tier1_verdict == "uncertain":
        return True, "tier1=uncertain"
    if deal_score >= LLM_CLOUD_FORCE_SCORE:
        return True, f"score>={LLM_CLOUD_FORCE_SCORE}"
    if deal_score >= LLM_CLOUD_LOWCONF_SCORE and confidence_label == "low":
        return True, f"score>={LLM_CLOUD_LOWCONF_SCORE}+low-conf"
    return False, "tier1 sufficient"


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
    """Run the secondary-check pipeline.

    Tier 1: local Ollama (free, every score≥85)
    Tier 2: MiniMax cloud — ESCALATED ONLY when:
              - Tier 1 returned 'uncertain', OR
              - score >= LLM_CLOUD_FORCE_SCORE (default 95), OR
              - score >= LLM_CLOUD_LOWCONF_SCORE (90) AND confidence='low'
            AND MiniMax is configured AND today's daily budget isn't
            exhausted.

    Returns the FINAL verdict (Tier 2 if it ran, otherwise Tier 1).
    `backend` field on the result tells you which model spoke last.
    """
    if not SECONDARY_CHECK_ENABLED:
        return VerifyResult("skipped", None, None, 0.0, None, None)

    # --- Tier 1: local Ollama ---
    tier1 = _verify_with_ollama(
        title=title, description=description, asking_price=asking_price,
        comp_median=comp_median, comp_sample_size=comp_sample_size,
        deal_score=deal_score,
    )

    # --- Tier 2: cloud escalation? ---
    should, reason = _should_escalate_to_cloud(
        tier1_verdict=tier1.verdict, deal_score=deal_score,
        confidence_label=confidence_label,
    )
    if not should:
        logger.debug("staying with tier1 verdict (%s)", reason)
        return tier1

    logger.info(
        "escalating to MiniMax (%s) — tier1=%s score=%d budget_left=%d",
        reason, tier1.verdict, deal_score,
        minimax_client.daily_budget_remaining(),
    )
    tier2 = minimax_client.verify(
        title=title, description=description, asking_price=asking_price,
        comp_median=comp_median, comp_sample_size=comp_sample_size,
        deal_score=deal_score, confidence_label=confidence_label,
    )
    if tier2.verdict == "skipped":
        # MiniMax bailed (network, budget race, parse error). Fall back
        # to tier1 — better something than nothing.
        logger.info("tier2 skipped (%s); falling back to tier1", tier2.concern or "no reason")
        return tier1

    # Adapt tier2's VerifyResult shape (minimax_client uses its own
    # dataclass; copy to ours).
    return VerifyResult(
        verdict=tier2.verdict,
        concern=tier2.concern,
        confidence=tier2.confidence,
        elapsed_s=tier1.elapsed_s + tier2.elapsed_s,
        model=tier2.model,
        backend="minimax",
    )
