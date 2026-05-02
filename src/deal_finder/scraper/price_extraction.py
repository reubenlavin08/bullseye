"""Price extraction layer.

Sellers often list items at $0 or $1 as a placeholder and put the real
price in the description ("$200 obo", "asking 350"). This module recovers
those prices.

Logic per the architecture doc:
  - If listing.price <= 1.00, scan description with regex patterns in
    priority order
  - First match wins
  - If no match: treat raw $0 as a true free listing, raw $1 as a true $1

Returns a small result dataclass so callers know whether the price was
extracted or original (preserved in the DB as `raw_price` and
`price_extracted_from_description`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Patterns in priority order. Each must capture the price as group 1.
# Use raw strings; case-insensitive matching applied at compile time.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\$\s*(\d+(?:[,\.]\d+)?)", re.IGNORECASE),
    re.compile(r"(\d+(?:[,\.]\d+)?)\s*obo\b", re.IGNORECASE),
    re.compile(r"\basking\s+(\d+(?:[,\.]\d+)?)", re.IGNORECASE),
    re.compile(r"\bprice\s+is\s+(\d+(?:[,\.]\d+)?)", re.IGNORECASE),
    re.compile(r"\bfirm\s+(\d+(?:[,\.]\d+)?)", re.IGNORECASE),
    re.compile(r"(\d+(?:[,\.]\d+)?)\s*firm\b", re.IGNORECASE),
]

PLACEHOLDER_THRESHOLD = 1.00


@dataclass(frozen=True)
class PriceResolution:
    """Result of running the price extraction layer.

    price            -- the price to use downstream (numeric, may be 0.0)
    raw_price        -- the originally scraped price, preserved for audit
    extracted        -- True iff price was recovered from description text
    """
    price: float
    raw_price: float
    extracted: bool


def _normalize_number(s: str) -> float | None:
    """Turn '1,200', '1.200', '1200', '1.5' into a float.

    Heuristic: if the string has both '.' and ',', treat ',' as thousands
    separator. If only ',' is present and there are 3 digits after it,
    treat as thousands separator. Otherwise treat ',' as decimal.
    """
    s = s.strip()
    if not s:
        return None

    has_dot = "." in s
    has_comma = "," in s

    if has_dot and has_comma:
        # e.g. "1,234.56" → strip commas, keep dot
        s = s.replace(",", "")
    elif has_comma and not has_dot:
        # ambiguous; if pattern looks like thousands ("1,200"), strip it
        if re.fullmatch(r"\d{1,3},\d{3}", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    # else: only dot or only digits — leave alone

    try:
        return float(s)
    except ValueError:
        return None


def extract_price_from_description(description: str) -> float | None:
    """Run patterns in priority order; return first numeric match or None.

    Returns None if description is empty or no pattern matches.
    """
    if not description:
        return None
    for pat in _PATTERNS:
        m = pat.search(description)
        if not m:
            continue
        val = _normalize_number(m.group(1))
        if val is None:
            continue
        # Sanity: ignore unrealistically tiny matches (e.g. "$0", "1 obo"
        # when the placeholder itself just gets re-matched). Anything <
        # threshold is treated as no-match.
        if val <= PLACEHOLDER_THRESHOLD:
            continue
        return val
    return None


def resolve_price(raw_price: float, description: str | None) -> PriceResolution:
    """Apply the full extraction policy.

    Always returns a PriceResolution. Callers should write `.price` to
    the DB's price column, `.raw_price` to raw_price, and `.extracted`
    to price_extracted_from_description.
    """
    if raw_price > PLACEHOLDER_THRESHOLD:
        # Normal listing — no extraction needed.
        return PriceResolution(
            price=raw_price, raw_price=raw_price, extracted=False
        )

    extracted = extract_price_from_description(description or "")
    if extracted is not None:
        return PriceResolution(
            price=extracted, raw_price=raw_price, extracted=True
        )

    # No usable extraction. Treat the raw value as the genuine price
    # ($0 = free, $1 = a real one-dollar listing).
    return PriceResolution(
        price=raw_price, raw_price=raw_price, extracted=False
    )
