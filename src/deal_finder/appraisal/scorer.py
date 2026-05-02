"""LLM fair-value estimator + human-readable note.

After the Phase 7.4 stats pivot, the LLM no longer picks the deal
score directly — that's mechanical now (`appraisal/formula.py`).
The LLM has two jobs left:

  1. Fair-value estimation, ONLY when comp data is sparse
     (sample_size < LLM_FALLBACK_THRESHOLD). When we have enough
     comps, the trimmed median (× asking-vs-sold discount) is a
     better estimator than any 7B model's intuition.

  2. Optional human-readable note explaining the score, used by the
     UI. The note never feeds back into the score itself.

Output: {"fair_value": float|null, "note": str}. No deal_score, no
confidence — those come from the formula.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..db.comps import CompStats
from .ollama_client import OllamaClient, get_default_client

logger = logging.getLogger(__name__)


# Defaults to the same 3B model used for normalization + price extraction.
# The fair_value estimation only fires as a fallback when comp sample is
# sparse (n<5), so the 3B's lower reasoning ceiling is acceptable here —
# we already mark those appraisals as low-confidence. Single resident
# model = much less VRAM pressure on small-GPU systems.
DEFAULT_MODEL = os.environ.get(
    "OLLAMA_APPRAISAL_MODEL", "llama3.2:3b-instruct-q4_K_M",
)


@dataclass
class LlmEstimate:
    """Raw output of one LLM fair-value call. Pure model output —
    the worker turns this into a final score via the formula module."""
    fair_value: float | None
    note: str
    model: str
    elapsed_s: float
    raw: dict | None = None


SYSTEM_PROMPT = """You estimate the fair secondhand market value of one Facebook Marketplace listing.

You receive a listing and (optionally) statistics from comparable items.
Comp stats, when present, are ASKING prices from Marketplace and run
15-30% above true sold prices.

YOUR JOB: estimate fair_value — what the item would actually SELL for
in a private secondhand transaction.

GUIDANCE:
- If sample_size is given and >= 3, anchor on the median asking price
  but discount it ~20% to approximate true sold value.
- If sample_size is small (1-2), use comps as one weak signal and
  weight your training knowledge heavily.
- If sample_size is 0, estimate purely from your training knowledge.
  Be conservative — pick the LOWER end of plausible secondhand value.
- If the listing notes damage / missing parts / "needs fix", reduce
  fair_value accordingly.
- If it includes accessories, increase fair_value modestly.

You do NOT compute a deal score. The score is calculated separately
by a deterministic formula from your fair_value estimate.

OUTPUT — return ONLY this JSON object, nothing else:
{
  "fair_value": <float, your estimated true secondhand market value>,
  "note": "<one short sentence on the listing's strengths/weaknesses>"
}
"""


def estimate_fair_value(
    *,
    title: str,
    asking_price: float,
    description: str | None,
    location: str | None,
    comp: CompStats,
    raw_price: float | None = None,
    price_extracted: bool = False,
    client: OllamaClient | None = None,
    model: str = DEFAULT_MODEL,
) -> LlmEstimate | None:
    """Ask the LLM for a fair-value estimate + a one-sentence note.

    Returns None if the LLM call fails or the output is unparseable —
    caller should fall back to the formula's raw-median path or skip
    this listing.
    """
    cli = client or get_default_client()
    user = _build_user_prompt(
        title=title,
        asking_price=asking_price,
        description=description,
        location=location,
        comp=comp,
        raw_price=raw_price,
        price_extracted=price_extracted,
    )

    try:
        resp = cli.generate_json(
            model=model,
            system=SYSTEM_PROMPT,
            user=user,
            temperature=0.2,
            num_predict=160,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("fair-value LLM call failed for %r: %s", title, e)
        return None

    parsed = resp.parsed
    if not isinstance(parsed, dict):
        logger.warning(
            "fair-value returned bad shape for %r: %s",
            title, resp.raw_text[:200],
        )
        return None

    fair_value = parsed.get("fair_value")
    if isinstance(fair_value, (int, float)) and fair_value > 0:
        fair_value = float(fair_value)
    else:
        fair_value = None

    note = str(parsed.get("note", "")).strip()[:500]

    return LlmEstimate(
        fair_value=fair_value,
        note=note,
        model=resp.model,
        elapsed_s=resp.elapsed_s,
        raw=parsed,
    )


def _build_user_prompt(
    *,
    title: str,
    asking_price: float,
    description: str | None,
    location: str | None,
    comp: CompStats,
    raw_price: float | None,
    price_extracted: bool,
) -> str:
    desc_block = (description or "").strip()
    if len(desc_block) > 1500:
        desc_block = desc_block[:1500] + "…"

    extracted_note = ""
    if price_extracted and raw_price is not None:
        extracted_note = (
            f"\nNote: asking price was extracted from the description; "
            f"the listing was originally posted at ${raw_price:.2f} "
            f"(common placeholder when sellers hide the price)."
        )

    if comp.sample_size > 0 and comp.median is not None:
        comp_block = (
            f"Comp stats for '{comp.search_term}' "
            f"(source: {comp.source}, current Marketplace asking prices):\n"
            f"  - sample_size: {comp.sample_size}\n"
            f"  - median: ${comp.median:.2f}\n"
            f"  - mean:   ${comp.mean:.2f}\n"
            f"  - range:  ${comp.minimum:.2f} – ${comp.maximum:.2f}"
        )
    else:
        comp_block = (
            f"Comp stats for '{comp.search_term}': "
            f"sample_size: 0 (no comparable listings found nearby). "
            f"Use your training knowledge to estimate fair value, and "
            f"set confidence to 'low'."
        )

    return (
        f"Listing:\n"
        f"  title: {title}\n"
        f"  asking price: ${asking_price:.2f}{extracted_note}\n"
        f"  location: {location or 'unknown'}\n"
        f"  description: {desc_block or '(none provided)'}\n\n"
        f"{comp_block}\n\n"
        f"Return your JSON object now."
    )
