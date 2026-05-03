"""Per-listing detail fetcher.

Search-endpoint results are slim cards (no description, no seller). To
score a deal we need the description text. This module exposes one entry
point — `fetch_detail(listing_id)` — that:

  1. Tries the official PDP GraphQL query (faster, more structured).
  2. Falls back to scraping the public listing HTML page if PDP returns
     no description (rare but possible — e.g. PDP shape drift).

If both fail, returns a Detail with description=None and a populated
`errors` list so the caller can decide how to handle it.

If both PDP and HTML start returning empty descriptions consistently,
recapture the PDP doc_id from DevTools (Marketplace -> click any
listing -> Network tab -> filter "graphql" -> look for
fb_api_req_friendly_name=MarketplacePDPContainerQuery).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi import requests  # type: ignore[import-untyped]
from curl_cffi.requests import exceptions as cffi_exc  # type: ignore[import-untyped]

# Reuse the same rate-gate + global-block helpers used by the search
# client. PDP fetches and search calls share the same per-IP quota at
# FB so any rate-limit observed by either should pause both.
from .facebook import (
    FacebookRateLimited,
    check_global_block,
    mark_globally_rate_limited,
)

logger = logging.getLogger(__name__)

_IMPERSONATE_TARGET = "chrome131"


# --- Constants ------------------------------------------------------------

FB_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
PDP_DOC_ID = "26284600011241990"
PDP_FRIENDLY_NAME = "MarketplacePDPContainerQuery"
LISTING_HTML_URL = "https://www.facebook.com/marketplace/item/{id}/"

PDP_HEADERS: dict[str, str] = {
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
    "X-FB-Friendly-Name": PDP_FRIENDLY_NAME,
}

HTML_HEADERS: dict[str, str] = {
    "User-Agent": PDP_HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Required Relay provider flags captured 2026-05-01 from a real DevTools
# request. Without these the PDP query is rejected.
RELAY_PROVIDERS: dict[str, bool | str] = {
    "__relay_internal__pv__ShouldUpdateMarketplaceBoostListingBoostedStatusrelayprovider": False,
    "__relay_internal__pv__CometUFIShareActionMigrationrelayprovider": True,
    "__relay_internal__pv__GHLShouldChangeSponsoredDataFieldNamerelayprovider": False,
    "__relay_internal__pv__GHLShouldChangeAdIdFieldNamerelayprovider": False,
    "__relay_internal__pv__CometUFI_dedicated_comment_routable_dialog_gkrelayprovider": True,
    "__relay_internal__pv__CometUFICommentAutoTranslationTyperelayprovider": "ORIGINAL",
    "__relay_internal__pv__CometUFICommentAvatarStickerAnimatedImagerelayprovider": False,
    "__relay_internal__pv__CometUFICommentActionLinksRewriteEnabledrelayprovider": False,
    "__relay_internal__pv__IsWorkUserrelayprovider": False,
    "__relay_internal__pv__CometUFIReactionsEnableShortNamerelayprovider": False,
    "__relay_internal__pv__CometUFISingleLineUFIrelayprovider": False,
}


# --- Public dataclasses ---------------------------------------------------

@dataclass
class Detail:
    """Listing detail. Any field except `id` may be None if the source(s)
    don't return it. `source` records which fetch path produced the
    description ('pdp' | 'html' | None)."""
    id: str
    title: str | None = None
    description: str | None = None
    price_formatted: str | None = None
    seller_name: str | None = None
    seller_type: str | None = None
    location: str | None = None
    listed_at_unix: int | None = None
    is_pending: bool | None = None
    photo_urls: list[str] = field(default_factory=list)
    source: str | None = None
    errors: list[str] = field(default_factory=list)


# --- Rate gate ------------------------------------------------------------

class _RateGate:
    """Same shape as the search-client gate, but also checks the
    process-wide rate-limit block before sleeping. Detail fetches
    happen RIGHT AFTER a search returns listings — exactly the pattern
    that compounds rate-limits if not gated."""

    def __init__(self, min_interval_s: float):
        self._min = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        check_global_block()  # raises FacebookRateLimited if blocked
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._min - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


# Default: at most one detail fetch per second. The orchestrator can
# override per-instance.
_DEFAULT_DETAIL_INTERVAL_S = 1.0


# --- Client ---------------------------------------------------------------

class FacebookDetailClient:
    """PDP-first detail fetcher with HTML fallback."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        rate_interval_s: float = _DEFAULT_DETAIL_INTERVAL_S,
        max_retries: int = 2,
        backoff_base_s: float = 1.0,
        timeout_s: int = 30,
    ):
        self._session = session or requests.Session(impersonate=_IMPERSONATE_TARGET)
        self._gate = _RateGate(rate_interval_s)
        self._max_retries = max_retries
        self._backoff = backoff_base_s
        self._timeout = timeout_s

    def fetch(self, listing_id: str) -> Detail:
        out = Detail(id=str(listing_id))

        # Stage 1: PDP
        try:
            self._gate.wait()
            payload = self._post_pdp(listing_id)
            self._fill_from_pdp(out, payload)
            if out.description:
                out.source = "pdp"
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning("pdp fetch failed for %s: %s", listing_id, e)
            out.errors.append(f"pdp: {type(e).__name__}: {e}")

        # Stage 2: HTML fallback
        try:
            self._gate.wait()
            html = self._get_html(listing_id)
            self._fill_from_html(out, html, listing_id)
            if out.description:
                out.source = "html"
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning("html fetch failed for %s: %s", listing_id, e)
            out.errors.append(f"html: {type(e).__name__}: {e}")

        return out

    # --- PDP -----------------------------------------------------------

    def _post_pdp(self, listing_id: str) -> dict:
        variables = {
            "enableJobEmployerActionBar": False,
            "enableJobSeekerActionBar": False,
            "feedbackSource": 56,
            "feedLocation": "MARKETPLACE_MEGAMALL",
            "referralCode": "marketplace_top_picks",
            "referralSurfaceString": "browse_tab",
            "scale": 1,
            "targetId": str(listing_id),
            "useDefaultActor": False,
            **RELAY_PROVIDERS,
        }
        payload = {
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": PDP_FRIENDLY_NAME,
            "doc_id": PDP_DOC_ID,
            "server_timestamps": "true",
            "variables": json.dumps(variables, separators=(",", ":")),
        }
        text = self._post_with_retry(FB_GRAPHQL_URL, PDP_HEADERS, payload)
        return _decode_fb_json(text)

    def _fill_from_pdp(self, out: Detail, payload: dict) -> None:
        node = _find_first(
            payload,
            lambda d: (
                "marketplace_listing_title" in d
                and ("redacted_description" in d or "description" in d)
            ),
        ) or _find_first(
            payload, lambda d: "marketplace_listing_title" in d,
        )
        if not isinstance(node, dict):
            return

        out.title = out.title or node.get("marketplace_listing_title")
        desc = node.get("redacted_description") or node.get("description")
        if isinstance(desc, dict):
            out.description = out.description or desc.get("text")
        elif isinstance(desc, str):
            out.description = out.description or desc

        out.price_formatted = out.price_formatted or _safe_get(
            node, "listing_price", "formatted_amount",
        )
        out.seller_name = out.seller_name or _safe_get(
            node, "marketplace_listing_seller", "name",
        )
        out.seller_type = out.seller_type or _safe_get(
            node, "marketplace_listing_seller", "__typename",
        )
        out.location = out.location or (
            _safe_get(node, "location", "reverse_geocode", "display_name")
            or _safe_get(node, "location", "reverse_geocode", "city")
            or _safe_get(node, "location_text", "text")
        )
        if isinstance(node.get("creation_time"), int):
            out.listed_at_unix = out.listed_at_unix or node["creation_time"]
        if isinstance(node.get("is_pending"), bool):
            out.is_pending = node["is_pending"] if out.is_pending is None else out.is_pending

        photos = node.get("listing_photos") or []
        if isinstance(photos, list) and not out.photo_urls:
            for p in photos:
                uri = (
                    _safe_get(p, "image", "uri")
                    if isinstance(p, dict) else None
                )
                if uri:
                    out.photo_urls.append(uri)

    # --- HTML ----------------------------------------------------------

    def _get_html(self, listing_id: str) -> str:
        url = LISTING_HTML_URL.format(id=listing_id)
        return self._get_with_retry(url, HTML_HEADERS)

    def _fill_from_html(self, out: Detail, html: str, listing_id: str) -> None:
        for blob in _iter_script_jsons(html):
            node = _find_first(
                blob,
                lambda d: (
                    isinstance(d.get("id"), str)
                    and d["id"] == listing_id
                    and (
                        "redacted_description" in d
                        or "description" in d
                        or "marketplace_listing_title" in d
                    )
                ),
            )
            if node is None:
                continue
            out.title = out.title or node.get("marketplace_listing_title")
            desc = node.get("redacted_description") or node.get("description")
            if isinstance(desc, dict):
                out.description = out.description or desc.get("text")
            elif isinstance(desc, str):
                out.description = out.description or desc
            if out.description:
                break

        if not out.description:
            m = re.search(
                r'<meta\s+name="description"\s+content="([^"]+)"', html,
            )
            if m:
                out.description = m.group(1)

        if not out.title:
            m = re.search(r"<title>([^<]+)</title>", html)
            if m:
                out.title = m.group(1).replace(
                    " | Facebook Marketplace", "",
                ).strip()

    # --- HTTP plumbing -------------------------------------------------

    def _post_with_retry(self, url, headers, data) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.post(
                    url, headers=headers, data=data, timeout=self._timeout,
                )
            except cffi_exc.RequestException as e:
                last_exc = e
                self._sleep_backoff(attempt)
                continue
            if 500 <= resp.status_code < 600:
                last_exc = cffi_exc.HTTPError(
                    f"{resp.status_code} from {url}", response=resp,
                )
                self._sleep_backoff(attempt)
                continue
            if resp.status_code >= 400:
                resp.raise_for_status()
            return resp.text
        assert last_exc is not None
        raise last_exc

    def _get_with_retry(self, url, headers) -> str:
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(
                    url, headers=headers, timeout=self._timeout,
                )
            except cffi_exc.RequestException as e:
                last_exc = e
                self._sleep_backoff(attempt)
                continue
            if 500 <= resp.status_code < 600:
                last_exc = cffi_exc.HTTPError(
                    f"{resp.status_code} from {url}", response=resp,
                )
                self._sleep_backoff(attempt)
                continue
            if resp.status_code >= 400:
                resp.raise_for_status()
            return resp.text
        assert last_exc is not None
        raise last_exc

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self._backoff * (2 ** attempt))


# --- Module-level helpers -------------------------------------------------

def _decode_fb_json(text: str) -> dict:
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


_SCRIPT_JSON_RE = re.compile(
    r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _iter_script_jsons(html: str):
    for m in _SCRIPT_JSON_RE.finditer(html):
        raw = m.group(1)
        if not raw or len(raw) < 50:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _find_first(obj, predicate):
    if isinstance(obj, dict):
        if predicate(obj):
            return obj
        for v in obj.values():
            r = _find_first(v, predicate)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_first(v, predicate)
            if r is not None:
                return r
    return None


def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


# --- Convenience API ------------------------------------------------------

_DEFAULT_CLIENT: FacebookDetailClient | None = None


def get_default_client() -> FacebookDetailClient:
    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = FacebookDetailClient()
    return _DEFAULT_CLIENT


def fetch_detail(listing_id: str) -> Detail:
    """Convenience wrapper. Returns a Detail with description filled in
    if either PDP or HTML succeeded; check `.source` to know which."""
    return get_default_client().fetch(listing_id)
