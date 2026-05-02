"""Title normalizer + LLM-based price extractor.

Marketplace titles are messy and prices are often hidden in descriptions.
This module exposes two functions:

  normalize_title(title)
      → clean comp-search keyword

  extract_price_llm(description)
      → numeric price recovered from free-text description, or None

Both use llama3.2:3B — small, fast, fits fully in VRAM. The LLM-based
price extractor is a fallback used by the worker when the regex
extractor misses (e.g. "asking two hundred", "$2k", "1.5K firm",
"DM for price"). Regex always tried first since it's free.
"""
from __future__ import annotations

import logging
import os
import re

from .ollama_client import OllamaClient, get_default_client

logger = logging.getLogger(__name__)


DEFAULT_MODEL = os.environ.get(
    "OLLAMA_NORMALIZER_MODEL", "llama3.2:3b-instruct-q4_K_M",
)

NORMALIZE_SYSTEM_PROMPT = """You normalize Facebook Marketplace listing titles into clean product search terms.

Rules:
- Output ONLY a JSON object: {"term": "..."}
- The term should be the brand + model + key specs (storage, size, color)
- Drop adjectives (great, mint, perfect, excellent, must-go)
- Drop punctuation, emoji, exclamation marks
- Drop urgency words (urgent, today, must sell, moving, asap)
- Drop condition words unless they're part of the model name
- Keep model numbers and capacities exactly
- If the title is too vague to identify a specific product, return the
  most specific category phrase you can extract (e.g. "electric scooter")
- Maximum 8 words

Examples:
  Input: "iPhone 14 Pro 256GB Space Blk works perfect!!!"
  Output: {"term": "iPhone 14 Pro 256GB"}

  Input: "Razor e90 electric scooter (works great)"
  Output: {"term": "Razor E90 electric scooter"}

  Input: "MUST GO! Snowboard 158cm Burton Custom mint"
  Output: {"term": "Burton Custom snowboard 158cm"}

  Input: "Some old bike for cheap"
  Output: {"term": "bicycle"}
"""

PRICE_EXTRACT_SYSTEM_PROMPT = """You extract the seller's asking price from a Facebook Marketplace listing description.

Sellers often post listings at $0 or $1 as a placeholder, then state the
real price somewhere in the description. Your job is to find that price.

Rules:
- Output ONLY a JSON object: {"price": <number-or-null>}
- "price" must be the numeric asking price as a number (not a string)
- Return null if the description does not state a clear asking price
- Handle these formats:
    "$200", "$1,200", "$2k", "$1.5K"
    "200 obo", "asking 350", "price is 500", "firm 450", "200 firm"
    "two hundred", "fifteen hundred", "two thousand"
- Convert text numbers to digits (e.g. "two hundred" -> 200, "1.5k" -> 1500)
- If multiple prices appear, return the seller's primary asking price
  (usually the largest specific number that makes sense as a price)
- Ignore dimensions, weights, model numbers, years, mileage, etc.
- Ignore prices that are clearly accessories' prices ("comes with $20 charger")
- If the seller says "DM for price" or "message me" with no number, return null

Examples:
  "$200 obo, no lowballs" → {"price": 200}
  "asking two hundred firm" → {"price": 200}
  "1.5K obo cash only" → {"price": 1500}
  "Comes with $20 charger and free helmet" → {"price": null}
  "DM me for price serious buyers only" → {"price": null}
  "2024 model, 350 miles, $1200 firm" → {"price": 1200}
"""


def normalize_title(
    title: str,
    *,
    client: OllamaClient | None = None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Return a clean search term derived from `title`. Regex fallback
    on LLM failure."""
    title = (title or "").strip()
    if not title:
        return ""

    cli = client or get_default_client()
    try:
        resp = cli.generate_json(
            model=model,
            system=NORMALIZE_SYSTEM_PROMPT,
            user=f"Title: {title}",
            temperature=0.1,
            num_predict=64,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("normalizer LLM call failed for %r: %s", title, e)
        return _regex_fallback(title)

    if not resp.parsed or "term" not in resp.parsed:
        logger.warning(
            "normalizer returned bad shape for %r: %s",
            title, resp.raw_text[:200],
        )
        return _regex_fallback(title)

    term = str(resp.parsed["term"]).strip()
    if not term:
        return _regex_fallback(title)
    return term[:120]


def extract_price_llm(
    description: str,
    *,
    client: OllamaClient | None = None,
    model: str = DEFAULT_MODEL,
) -> float | None:
    """Use the small LLM to extract an asking price from free-text
    description. Returns None when no clear price is stated.

    Used as a fallback by callers when the regex extractor misses
    (rare/odd formats). Costs one LLM call, ~3-5s on this hardware.
    """
    description = (description or "").strip()
    if not description:
        return None

    # Truncate very long descriptions — keeps prompt tight, prices are
    # almost always near the start anyway.
    if len(description) > 1500:
        description = description[:1500]

    cli = client or get_default_client()
    try:
        resp = cli.generate_json(
            model=model,
            system=PRICE_EXTRACT_SYSTEM_PROMPT,
            user=f"Description: {description}",
            temperature=0.0,
            num_predict=48,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("LLM price extract failed: %s", e)
        return None

    if not resp.parsed:
        return None

    price = resp.parsed.get("price")
    if price is None:
        return None
    try:
        val = float(price)
    except (TypeError, ValueError):
        return None
    # Sanity bounds: anything <$1 or >$1M is almost certainly a hallucination.
    if val < 1.0 or val > 1_000_000:
        return None
    return val


_FALLBACK_STRIP = re.compile(
    r"[!?.\(\)\[\]\{\}'\"]+|"
    r"\b(?:must|sell|today|asap|urgent|mint|perfect|great|excellent|"
    r"moving|cheap|bargain|like\s*new|works|wow|hot)\b",
    re.IGNORECASE,
)


def _regex_fallback(title: str) -> str:
    """Best-effort regex cleanup when the LLM is unreachable. Drops
    obvious noise words and excess punctuation; returns the original
    if the result would be empty."""
    cleaned = _FALLBACK_STRIP.sub("", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or title
