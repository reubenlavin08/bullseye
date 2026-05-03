"""Facebook Marketplace search client.

Production version of `scripts/spike_fb_scraper.py`. Same GraphQL endpoint,
same `doc_id`, but with:

  * typed dataclasses instead of dicts
  * retries with exponential backoff for transient HTTP / network errors
  * a configurable rate-limit gate so a caller looping over many searches
    does not hammer Facebook
  * structured logging so failures are debuggable from logs alone

Public API:
    search_listings(params: SearchParams) -> SearchPage

If the doc_id rotates and breaks the scraper, recapture from
`facebook.com/marketplace` Network tab > filter "graphql" and update the
constants below.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# curl_cffi instead of plain `requests` — it impersonates Chrome's
# TLS/JA3 fingerprint and HTTP/2 frame ordering, which moves us off
# Python's static fingerprint that's been known to anti-bot databases
# for years. Drop-in replacement for requests.Session() except we pass
# `impersonate=...` and import its exceptions namespace explicitly.
from curl_cffi import requests  # type: ignore[import-untyped]
from curl_cffi.requests import exceptions as cffi_exc  # type: ignore[import-untyped]
# Refresh the impersonation target every few months as Chrome ships
# new versions; curl_cffi ships chrome120/124/131/etc presets.
_IMPERSONATE_TARGET = "chrome131"

logger = logging.getLogger(__name__)


# --- Constants ------------------------------------------------------------

FB_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
LISTING_SEARCH_DOC_ID = "7111939778879383"
FRIENDLY_NAME = "CometMarketplaceSearchContentContainerQuery"

DEFAULT_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://www.facebook.com",
    "Referer": "https://www.facebook.com/marketplace/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "X-FB-Friendly-Name": FRIENDLY_NAME,
}


# --- Public dataclasses ---------------------------------------------------

@dataclass(frozen=True)
class SearchParams:
    """Inputs to one search request.

    `category_id` (optional) restricts results to a specific Marketplace
    category — critical for comp lookups so a Honda Civic search doesn't
    pull dash-cam listings. Captured per-listing as
    `marketplace_listing_category_id` and threaded through.
    """
    keyword: str
    lat: float
    lng: float
    radius_km: int = 40
    price_min: int | None = None
    price_max: int | None = None
    last_24h_only: bool = False
    cursor: str | None = None
    category_id: str | None = None


@dataclass(frozen=True)
class SearchListing:
    """Slim listing card returned by the search endpoint.

    Description and seller info are NOT in the search response — fetch
    them via `facebook_detail.fetch_detail(id)` per new listing.
    """
    id: str
    title: str
    price_amount: float | None      # parsed from formatted_amount
    price_formatted: str | None
    previous_price: str | None
    is_pending: bool
    photo_url: str | None
    seller_location: str | None
    listing_url: str
    category_id: str | None = None


@dataclass
class SearchPage:
    """One page of search results."""
    listings: list[SearchListing] = field(default_factory=list)
    end_cursor: str | None = None
    has_more: bool = False
    rate_limited: bool = False
    error_message: str | None = None


# --- Rate limiter ---------------------------------------------------------

class FacebookRateLimited(Exception):
    """Raised by the rate gate when a process-wide block is in effect.

    Carries the seconds-until-unblock so the caller can decide whether
    to give up immediately, log + skip, or schedule a retry. The
    coordinator's cooldown logic catches this and treats it as a fast
    skip (no FB request burned)."""

    def __init__(self, seconds_remaining: float, reason: str = "rate-limited"):
        self.seconds_remaining = seconds_remaining
        self.reason = reason
        super().__init__(
            f"FB rate-limit block active for ~{int(seconds_remaining)}s more "
            f"({reason})"
        )


# Process-wide, shared block. The moment ANY FB request comes back as
# rate-limited, we set this timestamp to (now + cooldown). All future
# FB calls — search, detail, comp lookup, probe — read this and raise
# FacebookRateLimited if it's still in the future. This stops a single
# rate-limit hit from triggering N follow-on requests (comp lookups,
# detail fetches) that compound the block.
#
# Lives at module scope (not on _RateGate) so the search client and
# detail client share one block — they're separate Sessions but talk
# to the same FB endpoint and share the same per-IP quota.
_global_block_lock = threading.Lock()
_global_blocked_until: float = 0.0
# Initial hard-stop on first detection — bumped from "implicit 60s
# coordinator cooldown" because the user observed the cooldown wasn't
# stopping comp/detail requests fast enough. 90s gives FB clear breathing
# room before we touch the endpoint again.
HARD_STOP_DURATION_S = 90.0


def mark_globally_rate_limited(reason: str = "fb_rate_limit") -> None:
    """Called by any FB-touching code that observes a rate-limit
    response. Sets a process-wide block so the next ~90s of FB requests
    fail fast (raise FacebookRateLimited) instead of going to the wire.

    The duration is intentionally NOT exponential here — that's the
    coordinator's job (in scheduler/jobs.py). This is a shorter,
    aggressive 'stop bleeding right now' fence."""
    global _global_blocked_until
    with _global_block_lock:
        deadline = time.monotonic() + HARD_STOP_DURATION_S
        if deadline > _global_blocked_until:
            _global_blocked_until = deadline
            logger.warning(
                "fb-block: hard-stop %ds (reason=%s) — all FB requests will "
                "raise FacebookRateLimited until then",
                int(HARD_STOP_DURATION_S), reason,
            )


def check_global_block() -> None:
    """Raise FacebookRateLimited if we're in the process-wide block.
    Called by the rate gate at the start of every wait()."""
    with _global_block_lock:
        remaining = _global_blocked_until - time.monotonic()
    if remaining > 0:
        raise FacebookRateLimited(remaining, "global block active")


def global_block_remaining_s() -> float:
    """Read-only access for the dashboard / coordinator. Returns 0
    when not blocked."""
    with _global_block_lock:
        return max(0.0, _global_blocked_until - time.monotonic())


class _RateGate:
    """Simple sleep-based rate gate. Thread-safe; cheap enough for the
    pipeline's modest QPS.

    On every wait(), checks the process-wide rate-limit block FIRST.
    If blocked, raises FacebookRateLimited immediately rather than
    sleeping or proceeding to a request that we KNOW will fail."""

    def __init__(self, min_interval_s: float):
        self._min = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        # Process-wide hard-stop first. This must come BEFORE the
        # min-interval sleep so a blocked period doesn't burn idle
        # threads waiting on the lock.
        check_global_block()
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._min - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Default: at most 1 search per 8 seconds. Override per-instance via
# `FacebookSearchClient(rate_interval_s=...)`.
#
# History: 2.0s and 5.0s both produced 100%+ rate-limit hits with 49
# active watches polling every 60s. FB's actual tolerance for a single
# unauthenticated IP is more like 7-8/min once we factor in any
# heuristic anti-bot scoring. 8.0s gives us a 7.5/min ceiling.
#
# Tradeoff: with N watches and 60s poll interval, each watch effectively
# polls every max(60s, 8s * N). At N=49 that's ~6.5 minutes between
# polls. Painful but the system actually works at this rate; at higher
# rates it's blocked >50% of the time and produces fewer effective polls.
# Pause watches you don't actively need on the dashboard's Manage tab
# to lower N and get faster polling on the ones that matter.
_DEFAULT_SEARCH_INTERVAL_S = 8.0


# --- Client ---------------------------------------------------------------

class FacebookSearchClient:
    """Reusable search client. Owns its session, rate gate, and retry
    policy. Construct once per process and call .search() many times."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        rate_interval_s: float = _DEFAULT_SEARCH_INTERVAL_S,
        max_retries: int = 3,
        backoff_base_s: float = 1.0,
        timeout_s: int = 30,
        headers: dict[str, str] | None = None,
    ):
        # curl_cffi.requests.Session takes `impersonate` and (via curl)
        # speaks HTTP/2 with browser-correct frame ordering and TLS
        # ClientHello permutation. Same .post() / .get() / .close() API
        # as plain requests.
        self._session = session or requests.Session(impersonate=_IMPERSONATE_TARGET)
        self._gate = _RateGate(rate_interval_s)
        self._max_retries = max_retries
        self._backoff = backoff_base_s
        self._timeout = timeout_s
        self._headers = {**DEFAULT_HEADERS, **(headers or {})}

    # --- public ----------------------------------------------------------

    def search(self, params: SearchParams) -> SearchPage:
        """Run one search request and return a parsed SearchPage.

        Raises curl_cffi.requests.exceptions.RequestException on
        terminal network failure (after all retries exhausted) or
        json.JSONDecodeError on a body we can't parse.
        """
        payload = self._build_payload(params)
        body = self._post_with_retry(payload)
        return self._parse_page(body)

    def probe_marketplace_root(self, *, timeout_s: float = 10.0) -> str:
        """Cheap health probe for the half-open circuit breaker.

        Hits the public Marketplace HTML root, NOT the GraphQL search
        endpoint. The HTML root is far less aggressively rate-limited
        because real users hit it constantly, so it gives us a
        low-signal way to test "is FB up and willing to respond to
        my IP/fingerprint?" without burning a full search request.

        Returns one of:
          'ok'      — 200 response, body looks like Marketplace HTML
          'blocked' — 403/429 or HTML 200 that contains a known
                      anti-bot signature
          'down'    — 5xx, network error, or unparseable response

        Uses the SAME session as search() so it shares cookies, TLS
        fingerprint, and HTTP/2 connection pool — meaning a 'ok' here
        tells us our actual session is unblocked, not just "FB
        responds to anyone."
        """
        try:
            self._gate.wait()
            resp = self._session.get(
                "https://www.facebook.com/marketplace/",
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Upgrade-Insecure-Requests": "1",
                },
                timeout=timeout_s,
            )
        except Exception as e:  # noqa: BLE001 — probe must never crash
            logger.debug("probe network error: %s", e)
            return "down"

        if 500 <= resp.status_code < 600:
            return "down"
        if resp.status_code in (403, 429):
            return "blocked"
        if resp.status_code != 200:
            return "down"

        # Cheap heuristic: real Marketplace HTML mentions 'marketplace'
        # somewhere in the body. A logged-out interstitial / block page
        # typically lacks it, OR contains 'temporarily blocked' /
        # 'unusual activity'. Body up to ~50 KB is plenty.
        body = (resp.text or "")[:50_000].lower()
        if "temporarily blocked" in body or "unusual activity" in body:
            return "blocked"
        if "marketplace" in body:
            return "ok"
        # Got a 200 but it doesn't look like marketplace — probably a
        # checkpoint or login wall. Treat as soft block.
        return "blocked"

    # --- internals -------------------------------------------------------

    def _build_payload(self, p: SearchParams) -> dict[str, str]:
        browse: dict[str, Any] = {
            "filter_location_latitude": p.lat,
            "filter_location_longitude": p.lng,
            "filter_radius_km": p.radius_km,
            "commerce_search_and_rp_available": True,
        }
        if p.price_min is not None:
            browse["filter_price_lower_bound"] = p.price_min
        if p.price_max is not None:
            browse["filter_price_upper_bound"] = p.price_max
        if p.last_24h_only:
            browse["commerce_search_and_rp_ctime_days"] = "19062;19061"
        if p.category_id:
            # Restrict comps/searches to one Marketplace category. Without
            # this a "Honda Civic 2015" search returns dash-cam comps.
            browse["filter_category_id"] = str(p.category_id)

        variables: dict[str, Any] = {
            "params": {
                "bqf": {
                    "callsite": "COMMERCE_MKTPLACE_WWW",
                    "query": p.keyword,
                },
                "browse_request_params": browse,
                "custom_request_params": {"surface": "SEARCH"},
            },
            "count": 24,
        }
        if p.cursor:
            variables["cursor"] = p.cursor

        return {
            "doc_id": LISTING_SEARCH_DOC_ID,
            "variables": json.dumps(variables, separators=(",", ":")),
        }

    def _post_with_retry(self, payload: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            self._gate.wait()
            try:
                resp = self._session.post(
                    FB_GRAPHQL_URL,
                    headers=self._headers,
                    data=payload,
                    timeout=self._timeout,
                )
            except cffi_exc.RequestException as e:
                last_exc = e
                logger.warning(
                    "fb-search network error attempt=%d/%d: %s",
                    attempt + 1, self._max_retries + 1, e,
                )
                self._sleep_backoff(attempt)
                continue

            # 5xx: retry. 4xx: surface immediately.
            if 500 <= resp.status_code < 600:
                last_exc = cffi_exc.HTTPError(
                    f"{resp.status_code} from FB GraphQL", response=resp,
                )
                logger.warning(
                    "fb-search 5xx attempt=%d/%d: %s",
                    attempt + 1, self._max_retries + 1, resp.status_code,
                )
                self._sleep_backoff(attempt)
                continue
            if resp.status_code >= 400:
                logger.error(
                    "fb-search HTTP %d body=%s",
                    resp.status_code, resp.text[:300],
                )
                resp.raise_for_status()

            # Success: parse and return
            return _decode_fb_json(resp.text)

        # Exhausted retries.
        assert last_exc is not None
        raise last_exc

    def _sleep_backoff(self, attempt: int) -> None:
        delay = self._backoff * (2 ** attempt)
        time.sleep(delay)

    def _parse_page(self, body: dict[str, Any]) -> SearchPage:
        rate_limited = False
        error_message: str | None = None
        if "errors" in body:
            logger.warning(
                "fb-search GraphQL errors: %s",
                json.dumps(body["errors"])[:500],
            )
            try:
                from ..db.events import record_event
                for err in body.get("errors") or []:
                    msg = (err.get("message") or "")
                    code = err.get("code")
                    is_rate_limit = "rate limit" in msg.lower() or code == 1675004
                    record_event(
                        "fb_rate_limit" if is_rate_limit else "fb_graphql_error",
                        code=code,
                        message=msg[:200],
                        severity=err.get("severity"),
                    )
                    if is_rate_limit:
                        rate_limited = True
                        error_message = error_message or msg[:200]
                    elif error_message is None:
                        error_message = msg[:200]
                # The moment a rate-limit comes back, stop ALL further
                # FB requests across the process. This includes comp
                # lookups and detail fetches that would otherwise
                # compound the block in the next minute.
                if rate_limited:
                    mark_globally_rate_limited(reason="fb_rate_limit")
            except Exception:  # noqa: BLE001 — never crash the scraper
                pass

        edges, end_cursor = _walk_search_edges(body)

        listings: list[SearchListing] = []
        for edge in edges:
            node = edge.get("node") if isinstance(edge, dict) else edge
            sl = _node_to_listing(node)
            if sl is not None:
                listings.append(sl)

        return SearchPage(
            listings=listings,
            end_cursor=end_cursor,
            has_more=bool(end_cursor),
            rate_limited=rate_limited,
            error_message=error_message,
        )


# --- Module-level helpers -------------------------------------------------

def _decode_fb_json(text: str) -> dict[str, Any]:
    """Strip FB's anti-hijack prefix and decode. Falls back to NDJSON line
    parsing if the body isn't a single JSON object."""
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise


def _walk_search_edges(body: dict) -> tuple[list[dict], str | None]:
    """Pull listing edges + end_cursor from the response. Resilient to
    shape drift via a deep-scan fallback."""
    edges: list[dict] = []
    end_cursor: str | None = None

    container = _safe_get(body, "data", "marketplace_search", "feed_units")
    if isinstance(container, dict):
        raw_edges = container.get("edges")
        if isinstance(raw_edges, list):
            edges = raw_edges
        end_cursor = _safe_get(container, "page_info", "end_cursor")

    if not edges:
        edges = _deep_find_listings(body)

    return edges, end_cursor


def _deep_find_listings(obj: Any) -> list[dict]:
    """Recursively find any dict with marketplace_listing_title."""
    out: list[dict] = []

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "marketplace_listing_title" in x:
                out.append({"node": x})
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return out


_PRICE_NUM_RE = re.compile(r"(\d+(?:[\d,]*\d)?(?:\.\d+)?)")


def _parse_price_amount(formatted: str | None) -> float | None:
    """Pull a numeric value out of e.g. 'CA$260' or '$1,200.00'."""
    if not formatted:
        return None
    m = _PRICE_NUM_RE.search(formatted.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _node_to_listing(node: Any) -> SearchListing | None:
    if not isinstance(node, dict):
        return None
    listing = node.get("listing") if isinstance(node.get("listing"), dict) else node
    title = listing.get("marketplace_listing_title")
    listing_id = (
        listing.get("id")
        or listing.get("legacy_id")
        or listing.get("ent_id")
    )
    if not title or not listing_id:
        return None

    formatted = _safe_get(listing, "listing_price", "formatted_amount")
    return SearchListing(
        id=str(listing_id),
        title=title,
        price_amount=_parse_price_amount(formatted),
        price_formatted=formatted,
        previous_price=_safe_get(listing, "strikethrough_price", "formatted_amount"),
        is_pending=bool(listing.get("is_pending") or listing.get("is_sold")),
        photo_url=_safe_get(listing, "primary_listing_photo", "image", "uri"),
        seller_location=(
            _safe_get(listing, "location", "reverse_geocode", "city_page", "display_name")
            or _safe_get(listing, "location_text", "text")
            or _safe_get(listing, "location", "reverse_geocode", "city")
        ),
        listing_url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        category_id=_safe_get_str(listing, "marketplace_listing_category_id"),
    )


def _safe_get_str(d, key) -> str | None:
    v = d.get(key) if isinstance(d, dict) else None
    return str(v) if v is not None else None


def _safe_get(d: Any, *keys: str, default: Any = None) -> Any:
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


# --- Convenience API ------------------------------------------------------

_DEFAULT_CLIENT: FacebookSearchClient | None = None


def get_default_client() -> FacebookSearchClient:
    """Singleton client for callers that don't need custom config."""
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = FacebookSearchClient()
    return _DEFAULT_CLIENT


def search_listings(params: SearchParams) -> SearchPage:
    """One-call convenience wrapper around the default client."""
    return get_default_client().search(params)
