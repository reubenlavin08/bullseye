"""MiniMax cloud LLM client — used as the Tier-2 escalation in the
secondary-check pipeline.

Tier 1 (local Ollama 3B) handles every score≥85 listing for free. The
3B model is fast (~2s) but limited — it sometimes returns 'uncertain'
on subtle gotchas (financing language, parts-only, etc.) that a
larger model would catch.

Tier 2 (this module) is reserved for the cases where Tier 1 can't
confidently decide. We use a hard DAILY BUDGET so usage stays sparing
even under heavy poll volume. When the budget is exhausted, Tier 2
silently skips and the pipeline falls back to Tier 1's verdict.

MiniMax's API is OpenAI-compatible (`/v1/chat/completions` with
`Authorization: Bearer <key>` header). No SDK needed — plain HTTP.

Public API:
    available()                  → bool: ready to call?
    daily_budget_remaining()     → int:  calls left today
    verify(...)                  → VerifyResult (same shape as Ollama)

Env config:
    MINIMAX_API_KEY              required to enable
    MINIMAX_BASE_URL             default https://api.minimaxi.chat/v1
                                 (international endpoint; use
                                  api.minimax.chat for China region)
    MINIMAX_MODEL                default 'MiniMax-Text-01'
    MINIMAX_DAILY_BUDGET         default 20 calls/day
    MINIMAX_TIMEOUT_S            default 30
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

import requests

from ..db.connection import get_conn

logger = logging.getLogger(__name__)


DEFAULT_BASE_URL = "https://api.minimaxi.chat/v1"
DEFAULT_MODEL = "MiniMax-Text-01"
DEFAULT_DAILY_BUDGET = 20
DEFAULT_TIMEOUT_S = 30


# Same VerifyResult shape as secondary_check.VerifyResult so callers
# can swap backends without rewriting handlers.
@dataclass
class VerifyResult:
    verdict: str         # 'legit' | 'suspect' | 'uncertain' | 'skipped'
    concern: str | None
    confidence: str | None
    elapsed_s: float
    model: str | None
    backend: str         # 'minimax' for everything from this client


def _api_key() -> str:
    return os.environ.get("MINIMAX_API_KEY", "").strip()


def available() -> bool:
    """True if a MiniMax API key is configured."""
    return bool(_api_key())


def _daily_budget_cap() -> int:
    try:
        return max(0, int(os.environ.get("MINIMAX_DAILY_BUDGET", str(DEFAULT_DAILY_BUDGET))))
    except (TypeError, ValueError):
        return DEFAULT_DAILY_BUDGET


def daily_calls_used_today() -> int:
    """Count successful MiniMax calls recorded today (by scheduler_events
    of type minimax_call). Used to enforce the daily budget."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT COUNT(*) FROM scheduler_events
                       WHERE event_type = 'minimax_call'
                         AND created_at >= NOW()::date""",
                )
                return int(cur.fetchone()[0])
    except Exception as e:  # noqa: BLE001
        logger.warning("daily-budget query failed (assume 0): %s", e)
        return 0


def daily_budget_remaining() -> int:
    """Calls left in today's budget. Hits 0 → Tier 2 disables itself
    until midnight."""
    return max(0, _daily_budget_cap() - daily_calls_used_today())


def _record_call(*, success: bool, elapsed_s: float, error: str | None) -> None:
    """Persist a minimax_call event so the daily-budget query sees it.
    Best-effort; never raises."""
    try:
        with get_conn() as conn:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """INSERT INTO scheduler_events
                              (event_type, duration_ms, detail)
                           VALUES (%s, %s, %s::jsonb)""",
                        (
                            "minimax_call",
                            int(elapsed_s * 1000),
                            json.dumps({
                                "success": bool(success),
                                "error": error,
                            }),
                        ),
                    )
    except Exception as e:  # noqa: BLE001
        logger.warning("could not record minimax_call event: %s", e)


# --- Prompt (same as secondary_check, just text — kept here too so
#     callers can swap backends in isolation). ----------------------

_SYSTEM_PROMPT = """You are a careful, skeptical second-opinion reviewer for a Facebook Marketplace deal-finder.

The deal-finder uses statistical comparison to score listings. It's flagged this listing as a "great deal" because the asking price is much lower than the median of comparable items.

Your job is to spot when a listing scores high for the WRONG REASONS. Look hard at the title and the description for these gotchas:

- The asking price is for RENT or LEASE per period, not a sale. (Phrases: '/mo', 'per month', 'lease', 'weekly', 'rental', '/biweekly'. Watch for leetspeak/typos: 'r3nt', 'l3ase', 'mnthly'.)
- The asking price is a DEPOSIT or DOWN PAYMENT, not full price. ('deposit', 'down payment', 'OAC', '$X down', 'finance from $Y/mo'.)
- The listing is for PARTS / a single component, not the whole item. ('for parts', 'parting out', 'frame only', 'no engine', 'as-is parts car'.)
- The item is a TOY, REPLICA, or KNOCK-OFF being compared to the real thing. ('barbie', 'doll', 'toy', '1:18 scale', '6V', 'replica', 'kids ride-on'.)
- Scam / fake / bait listing — title doesn't match description, suspicious vibe, brand-new item priced 95% below market.
- The price is for SHIPPING, VIEWING, or DELIVERY only.
- Wrong generation/model from what title implies (e.g. titled 'iPhone' but it's an iPhone 5).
- 'OBO', 'name your price', 'send offers' — listed price is bait.
- Bundle/package — listed price is for one tiny part of a multi-item bundle.

If NONE of these apply and the listing genuinely appears to be the real item at the listed price (just a good deal), return verdict 'legit'.

If you SEE a concrete red flag, return 'suspect' with a SHORT concern.

If the description doesn't give you enough to tell, return 'uncertain'.

Respond with valid JSON ONLY (no markdown, no commentary):
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
    desc = (description or "").strip()
    if len(desc) > 1500:
        desc = desc[:1500] + "...[truncated]"
    comp_line = (
        f"Comparable listings median ${comp_median:.0f} (n={comp_sample_size or 0})"
        if comp_median is not None else "No comp data available."
    )
    if comp_median:
        ratio = asking_price / comp_median * 100
        comp_line += f" — asking is {ratio:.0f}% of comp median"
    return (
        f"LISTING UNDER REVIEW:\n\n"
        f"Title: {title}\n"
        f"Asking price: ${asking_price:.0f}\n\n"
        f"Description:\n{desc or '(no description)'}\n\n"
        f"COMP DATA:\n{comp_line}\n\n"
        f"Statistical score: {deal_score}/100 (higher = better deal vs. comps)\n\n"
        f"Question: Based on the title and description, is this a legitimate good deal at the listed asking price, or is something obfuscated? Respond in JSON."
    )


def verify(
    *,
    title: str,
    description: str | None,
    asking_price: float,
    comp_median: float | None,
    comp_sample_size: int | None,
    deal_score: int,
    confidence_label: str | None = None,
) -> VerifyResult:
    """Run a verification through MiniMax. Returns verdict='skipped' on
    any failure (no key, budget exhausted, network error, parse error)
    so the caller can fall back to Tier 1's verdict.

    Important: this function ALWAYS records a minimax_call event when
    a real network call is made (success OR failure), so daily budget
    is enforced even on errors that consume API quota.
    """
    import time

    if not available():
        return VerifyResult("skipped", None, None, 0.0, None, "minimax")
    if daily_budget_remaining() <= 0:
        logger.info("minimax skipped: daily budget exhausted")
        return VerifyResult("skipped", None, None, 0.0, None, "minimax")

    base = (os.environ.get("MINIMAX_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("MINIMAX_MODEL") or DEFAULT_MODEL
    timeout = int(os.environ.get("MINIMAX_TIMEOUT_S") or DEFAULT_TIMEOUT_S)

    user_prompt = _build_user_prompt(
        title=title or "(no title)",
        description=description or "",
        asking_price=asking_price,
        comp_median=comp_median,
        comp_sample_size=comp_sample_size,
        deal_score=deal_score,
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }

    t0 = time.perf_counter()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - t0
    except requests.RequestException as e:
        elapsed = time.perf_counter() - t0
        logger.warning("minimax network error: %s", e)
        _record_call(success=False, elapsed_s=elapsed, error=str(e)[:200])
        return VerifyResult("skipped", None, None, elapsed, None, "minimax")

    if resp.status_code == 401 or resp.status_code == 403:
        _record_call(success=False, elapsed_s=elapsed,
                     error=f"auth {resp.status_code}")
        logger.error("minimax auth rejected (HTTP %d) — check MINIMAX_API_KEY",
                     resp.status_code)
        return VerifyResult("skipped", None, None, elapsed, None, "minimax")

    if resp.status_code >= 400:
        _record_call(success=False, elapsed_s=elapsed,
                     error=f"http {resp.status_code}: {resp.text[:120]}")
        logger.warning("minimax HTTP %d: %s",
                       resp.status_code, resp.text[:200])
        return VerifyResult("skipped", None, None, elapsed, None, "minimax")

    # Successful HTTP — count it against the budget
    _record_call(success=True, elapsed_s=elapsed, error=None)

    try:
        body = resp.json()
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content) if content else {}
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as e:
        logger.warning("minimax response parse error: %s", e)
        return VerifyResult("uncertain", "(parse error)", None,
                            elapsed, model, "minimax")

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
        elapsed_s=elapsed,
        model=model,
        backend="minimax",
    )
