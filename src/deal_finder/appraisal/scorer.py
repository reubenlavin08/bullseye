"""Listing → deal score.

Sends one listing + its comp stats to Qwen-2.5:7B and parses the JSON
back. Three behavioral rules baked into the prompt:

  1. Comps are ASKING prices from Facebook Marketplace, not sold prices.
     Marketplace asks tend to run 15-30% above what items actually sell
     for. The model is told this explicitly so it doesn't mistake the
     median for true market value.

  2. When sample_size is small (< 3) or zero, the model is told to lean
     on prior knowledge and emit confidence="low".

  3. Output is a strict JSON object: deal_score (0-100), fair_value
     (float), confidence ("high"|"medium"|"low"), note (one sentence).

The system prompt does not change between calls — keeps the prompt
prefix identical so any KV-cache benefit Ollama provides accrues.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from ..db.comps import CompStats
from .ollama_client import OllamaClient, get_default_client

logger = logging.getLogger(__name__)


DEFAULT_MODEL = os.environ.get(
    "OLLAMA_APPRAISAL_MODEL", "qwen2.5:7b-instruct-q4_K_M",
)


@dataclass
class Appraisal:
    """Output of one scoring call."""
    deal_score: int          # 0-100
    fair_value: float | None
    confidence: str          # "high" | "medium" | "low"
    note: str
    model: str
    elapsed_s: float
    raw: dict | None = None  # original LLM JSON for audit


SYSTEM_PROMPT = """You are a secondhand-marketplace deal evaluator.

You receive one local Facebook Marketplace listing and aggregate stats
of similar items currently listed nearby. You return a JSON object
with a 0-100 deal score and a fair-value estimate.

CRITICAL CONTEXT:
- The comp stats are ASKING prices from Marketplace, NOT sold prices.
- Asking prices typically run 15-30% above actual sale prices.
- When you estimate fair_value, anchor on the comp median but discount
  it ~20% to approximate true market value (unless your training tells
  you the item commands premium prices).

SAMPLE SIZE GUIDANCE:
- sample_size >= 5: anchor strongly on the comp median (with the 20%
  asking-price discount). Confidence: "high".
- sample_size 3-4: blend the median with your training knowledge.
  Confidence: "medium".
- sample_size 1-2: use comps as one weak signal; weight your training
  knowledge heavily. Confidence: "low".
- sample_size 0: estimate fair_value purely from your training
  knowledge. Be conservative — pick the LOWER end of what you think
  the item is worth secondhand. Confidence: "low".

SCORING:
- 90-100: exceptional deal, asking ≤ 50% of fair value
- 70-89:  good deal, asking 50-75% of fair value
- 50-69:  fair price, asking 75-100% of fair value
- 30-49:  slightly overpriced, asking 100-130% of fair value
- 0-29:   significantly overpriced, asking > 130% of fair value

OUTPUT — return ONLY this JSON object, no other text:
{
  "deal_score": <int 0-100>,
  "fair_value": <float, your estimated true secondhand market value>,
  "confidence": "high" | "medium" | "low",
  "note": "<one sentence explaining the score>"
}
"""


def score_listing(
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
) -> Appraisal | None:
    """Score one listing. Returns None if the LLM call fails terminally
    or the output is unparseable — caller should leave the listing
    unappraised and retry next cycle."""
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
            num_predict=200,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("scorer LLM call failed for %r: %s", title, e)
        return None

    parsed = resp.parsed
    if not isinstance(parsed, dict):
        logger.warning(
            "scorer returned bad shape for %r: %s", title, resp.raw_text[:200],
        )
        return None

    score = parsed.get("deal_score")
    if not isinstance(score, (int, float)):
        logger.warning("scorer returned non-numeric deal_score: %s", parsed)
        return None

    deal_score = max(0, min(100, int(score)))
    fair_value = parsed.get("fair_value")
    if isinstance(fair_value, (int, float)):
        fair_value = float(fair_value)
    else:
        fair_value = None

    confidence = parsed.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"

    note = str(parsed.get("note", "")).strip()[:500]

    return Appraisal(
        deal_score=deal_score,
        fair_value=fair_value,
        confidence=confidence,
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
