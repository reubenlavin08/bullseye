"""eBay Finding API client — sold comps via findCompletedItems.

Why eBay matters: Marketplace asking-prices have systemic upward bias
(sellers inflate ~20%, buyers negotiate). eBay's `findCompletedItems`
returns the actual sale price of items that ACTUALLY SOLD, in real
auctions or BIN listings. That's ground-truth comp data — no asking-
vs-sold guessing.

Why Finding API specifically: it's the simplest path that gets sold
data. Auth is just an App ID in a header (no OAuth dance). Rate limit
is 5000 calls/day for free tier — plenty for 46 watches × few comp
fetches each.

Status as of 2026: Finding API is "deprecated but functional" — eBay
keeps the endpoint up and serving real data, but doesn't add features.
That's fine for our use case. If it ever breaks, swap to Browse API
(`item_summary/search`) plus Marketplace Insights API (`buy/browse/v1`)
for sold data — those need OAuth.

Public API:
    EbayClient(app_id="ABC...").find_completed_items(keywords="arduino uno")

Returns a list of CompObservation suitable for db.comps.insert_comps.

Env config (read at module load):
    EBAY_APP_ID         (required)
    EBAY_GLOBAL_ID      default 'EBAY-US' (use 'EBAY-ENCA' for Canada eBay)
    EBAY_ENABLED        '0' to disable; default '1' if APP_ID is set
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

# Same curl_cffi we use for FB. eBay doesn't need fingerprint
# impersonation, but using one HTTP library across the codebase keeps
# things consistent and cheap.
from curl_cffi import requests  # type: ignore[import-untyped]
from curl_cffi.requests import exceptions as cffi_exc  # type: ignore[import-untyped]

from ..db.comps import CompObservation

logger = logging.getLogger(__name__)


# --- Constants ------------------------------------------------------------

FINDING_URL = "https://svcs.ebay.com/services/search/FindingService/v1"

# Browse API (modern OAuth-based replacement for the deprecated Finding
# API). findCompletedItems on the legacy Finding API is rate-limited to
# ~zero on new keysets, so we use Browse for the actual fetches.
BROWSE_BASE_URL = "https://api.ebay.com"
BROWSE_OAUTH_URL = f"{BROWSE_BASE_URL}/identity/v1/oauth2/token"
BROWSE_SEARCH_URL = f"{BROWSE_BASE_URL}/buy/browse/v1/item_summary/search"

# `EBAY-US` is the global U.S. site; `EBAY-ENCA` is the Canadian site
# (English). Most Canadian users want EBAY-ENCA so prices come back in
# CAD for direct comparison with Marketplace Vancouver listings.
# For Browse API, this maps to the X-EBAY-C-MARKETPLACE-ID header.
DEFAULT_GLOBAL_ID = os.environ.get("EBAY_GLOBAL_ID", "EBAY-US")

# Map Finding-API GLOBAL-ID values to Browse-API marketplace IDs.
_GLOBAL_ID_TO_MARKETPLACE = {
    "EBAY-US": "EBAY_US",
    "EBAY-ENCA": "EBAY_CA",
    "EBAY-GB": "EBAY_GB",
    "EBAY-DE": "EBAY_DE",
    "EBAY-AU": "EBAY_AU",
    "EBAY-FR": "EBAY_FR",
    "EBAY-IT": "EBAY_IT",
    "EBAY-ES": "EBAY_ES",
}

# Master enable: defaults true if APP_ID present, else false. Set
# EBAY_ENABLED=0 to force off even with a key (useful during outages).
def _is_enabled() -> bool:
    if os.environ.get("EBAY_ENABLED", "").strip() == "0":
        return False
    return bool(os.environ.get("EBAY_APP_ID", "").strip())


def is_ebay_enabled() -> bool:
    """Public probe — used by callers (e.g. appraisal pipeline) to
    decide whether to even try eBay. Re-evaluates env every call so a
    config change without restart picks up."""
    return _is_enabled()


# --- Rate gate ------------------------------------------------------------

class _RateGate:
    """Same simple sleep gate as the FB client. eBay's documented limit
    is 5000 reqs/day = ~1 req every 17s sustained, but the API tolerates
    bursts. We use 1s/req as a safe conservative default."""

    def __init__(self, min_interval_s: float):
        self._min = min_interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        if self._min <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._last + self._min - now
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()


_DEFAULT_INTERVAL_S = 1.0


def _record_ebay_api_call(*, operation: str, status: str, keywords: str | None = None) -> None:
    """Persist an 'ebay_api' event so the dashboard can show a daily
    call counter. Best-effort; never raises (an observability bug
    should never break a real comp fetch)."""
    try:
        from ..db.events import record_event
        record_event(
            "ebay_api",
            operation=operation,
            status=status,
            keywords=(keywords or "")[:60],
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("could not record ebay_api event: %s", e)


# --- Client ---------------------------------------------------------------

@dataclass
class EbayCompResult:
    """Slim wrapper for findCompletedItems output. `price_amount` is the
    final selling price in `currency`. Items that didn't sell (auction
    ended without a winner) are filtered out by the SoldItemsOnly
    filter so every result here represents a real transaction."""
    item_id: str
    title: str
    price_amount: float
    currency: str
    end_time_iso: str | None
    view_url: str | None
    location: str | None


class EbayClient:
    """Finding API client. Construct once per process; safe across
    threads thanks to the rate gate's lock."""

    def __init__(
        self,
        *,
        app_id: str | None = None,
        cert_id: str | None = None,
        global_id: str | None = None,
        rate_interval_s: float = _DEFAULT_INTERVAL_S,
        timeout_s: int = 20,
        session: requests.Session | None = None,
    ):
        self._app_id = app_id or os.environ.get("EBAY_APP_ID", "").strip()
        if not self._app_id:
            raise ValueError(
                "EBAY_APP_ID not set. Get one at developer.ebay.com → "
                "My Account → Application Keysets."
            )
        # Cert ID is optional — only required for Browse API OAuth.
        # Finding API doesn't need it. We tolerate either being absent
        # so a user can run with whichever path is available.
        self._cert_id = cert_id or os.environ.get("EBAY_CERT_ID", "").strip() or None
        self._global_id = global_id or DEFAULT_GLOBAL_ID
        self._marketplace_id = _GLOBAL_ID_TO_MARKETPLACE.get(
            self._global_id, "EBAY_US",
        )
        self._gate = _RateGate(rate_interval_s)
        self._timeout = timeout_s
        self._session = session or requests.Session()
        # OAuth token cache for Browse API. Tokens are 7200s (2h) TTL;
        # we refresh ~5 min before expiry to avoid races.
        self._oauth_token: str | None = None
        self._oauth_expires_at: float = 0.0
        self._oauth_lock = threading.Lock()

    # --- Browse API (OAuth) — primary path -------------------------------

    def _get_oauth_token(self) -> str:
        """Fetch & cache a Browse API access token. Uses the
        client-credentials grant: app+cert IDs are exchanged for a
        bearer token valid ~2h. We re-fetch ~5 min before expiry.

        Raises ValueError when EBAY_CERT_ID isn't configured (Browse API
        requires both App ID + Cert ID; Finding API only needs App ID).
        """
        if not self._cert_id:
            raise ValueError(
                "EBAY_CERT_ID not set — required for Browse API. "
                "Get it at developer.ebay.com next to your App ID "
                "(labeled 'Cert ID (Client Secret)')."
            )

        with self._oauth_lock:
            now = time.monotonic()
            if self._oauth_token and now < self._oauth_expires_at - 300:
                return self._oauth_token

            import base64
            basic = base64.b64encode(
                f"{self._app_id}:{self._cert_id}".encode("utf-8"),
            ).decode("ascii")

            self._gate.wait()
            resp = self._session.post(
                BROWSE_OAUTH_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    # Public Browse API scope — read-only, no user data
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
                timeout=self._timeout,
            )
            if resp.status_code >= 400:
                raise ValueError(
                    f"eBay OAuth token request failed (HTTP {resp.status_code}): "
                    f"{resp.text[:300]}"
                )
            body = resp.json()
            token = body.get("access_token")
            expires_in = int(body.get("expires_in", 7200))
            if not token:
                raise ValueError(
                    f"eBay OAuth response missing access_token: {body}"
                )
            self._oauth_token = token
            self._oauth_expires_at = now + expires_in
            return token

    def find_active_items(
        self,
        *,
        keywords: str,
        limit: int = 50,
        condition_filter: str | None = None,
        price_min: float | None = None,
        price_max: float | None = None,
    ) -> list[EbayCompResult]:
        """Browse API search — returns ACTIVE listings (asking prices).

        This is the modern replacement for findCompletedItems. eBay
        rate-limited the legacy 'sold items' endpoint to nearly zero
        on new keysets, so we use Browse for the actual data. The
        prices are asking-prices, but eBay listings are far cleaner
        than FB Marketplace (less spam, BIN prices that often = sold,
        explicit condition filtering).

        condition_filter: an eBay filter expression like 'NEW|USED' or
        'USED'. Default None = no filter (all conditions).
        """
        if not keywords or not keywords.strip():
            return []

        token = self._get_oauth_token()

        params: dict[str, Any] = {
            "q": keywords.strip(),
            "limit": min(max(limit, 1), 200),
        }
        filter_parts = []
        if condition_filter:
            filter_parts.append(f"conditions:{{{condition_filter}}}")
        if price_min is not None or price_max is not None:
            lo = f"{price_min:.2f}" if price_min is not None else ""
            hi = f"{price_max:.2f}" if price_max is not None else ""
            filter_parts.append(f"price:[{lo}..{hi}],priceCurrency:USD")
        if filter_parts:
            params["filter"] = ",".join(filter_parts)

        self._gate.wait()
        try:
            resp = self._session.get(
                BROWSE_SEARCH_URL,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-EBAY-C-MARKETPLACE-ID": self._marketplace_id,
                    "Accept": "application/json",
                },
                timeout=self._timeout,
            )
        except cffi_exc.RequestException as e:
            logger.warning("eBay browse-api network error: %s", e)
            _record_ebay_api_call(operation="browse_search", status="network_error",
                                  keywords=keywords)
            raise

        if resp.status_code in (401, 403):
            # Token may have just expired — clear cache and retry once
            with self._oauth_lock:
                self._oauth_token = None
                self._oauth_expires_at = 0
            _record_ebay_api_call(operation="browse_search",
                                  status=f"auth_{resp.status_code}",
                                  keywords=keywords)
            raise ValueError(
                f"eBay Browse API auth rejected ({resp.status_code}): "
                f"{resp.text[:200]}"
            )
        if resp.status_code >= 400:
            logger.warning(
                "eBay Browse HTTP %d: %s", resp.status_code, resp.text[:300],
            )
            _record_ebay_api_call(operation="browse_search",
                                  status=f"http_{resp.status_code}",
                                  keywords=keywords)
            resp.raise_for_status()

        _record_ebay_api_call(operation="browse_search", status="ok",
                              keywords=keywords)

        body = resp.json()
        return _parse_browse_items(body)

    def find_completed_items(
        self,
        *,
        keywords: str,
        entries_per_page: int = 50,
        condition_ids: tuple[int, ...] = (),
    ) -> list[EbayCompResult]:
        """Run findCompletedItems for one keyword. Filters to SOLD items
        only (auction ended in a sale OR Buy-It-Now completed).

        condition_ids: optional eBay condition codes. 1000=New, 1500=New
        Other, 2000=Manufacturer Refurbished, 2500=Seller Refurbished,
        3000=Used, 4000=Very Good, 5000=Good, 6000=Acceptable,
        7000=For Parts. Empty tuple = no filter (all conditions).

        Returns possibly-empty list. Raises ValueError on auth issues
        and curl_cffi.requests.exceptions.RequestException on terminal
        network failure.
        """
        if not keywords or not keywords.strip():
            return []

        params: dict[str, Any] = {
            "OPERATION-NAME": "findCompletedItems",
            "SERVICE-VERSION": "1.13.0",
            "SECURITY-APPNAME": self._app_id,
            "GLOBAL-ID": self._global_id,
            "RESPONSE-DATA-FORMAT": "JSON",
            "REST-PAYLOAD": "true",
            "keywords": keywords.strip(),
            "paginationInput.entriesPerPage": min(max(entries_per_page, 1), 100),
            "paginationInput.pageNumber": 1,
            # Filter 0: only sold items
            "itemFilter(0).name": "SoldItemsOnly",
            "itemFilter(0).value": "true",
        }
        if condition_ids:
            params["itemFilter(1).name"] = "Condition"
            for i, cid in enumerate(condition_ids):
                params[f"itemFilter(1).value({i})"] = str(cid)

        self._gate.wait()
        try:
            resp = self._session.get(
                FINDING_URL, params=params, timeout=self._timeout,
            )
        except cffi_exc.RequestException as e:
            logger.warning("eBay finding-api network error: %s", e)
            raise

        if resp.status_code == 401 or resp.status_code == 403:
            raise ValueError(
                f"eBay rejected the App ID (HTTP {resp.status_code}). "
                f"Check EBAY_APP_ID is valid + production-credentialed: "
                f"{resp.text[:200]}"
            )
        if resp.status_code >= 400:
            logger.warning(
                "eBay HTTP %d: %s", resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()

        body = resp.json()
        return _parse_completed_items(body)


# --- Response parsing -----------------------------------------------------

def _parse_browse_items(body: dict[str, Any]) -> list[EbayCompResult]:
    """Walk Browse API's `item_summary/search` JSON. Cleaner shape than
    the legacy Finding API — fields are direct strings/objects, no
    array-everywhere wrapping."""
    results: list[EbayCompResult] = []
    items = body.get("itemSummaries") or []
    if not isinstance(items, list):
        return []

    for it in items:
        try:
            sold = _extract_browse(it)
            if sold is not None:
                results.append(sold)
        except Exception as e:  # noqa: BLE001
            logger.debug("skipping browse item, parse error: %s", e)
            continue
    return results


def _extract_browse(it: dict[str, Any]) -> EbayCompResult | None:
    """Pull the comp fields out of a Browse API item_summary entry."""
    item_id = it.get("itemId") or it.get("legacyItemId")
    title = it.get("title")
    if not item_id or not title:
        return None
    price_obj = it.get("price")
    if not isinstance(price_obj, dict):
        return None
    try:
        price_amount = float(price_obj.get("value"))
    except (TypeError, ValueError):
        return None
    currency = price_obj.get("currency") or "USD"

    location = None
    item_loc = it.get("itemLocation")
    if isinstance(item_loc, dict):
        # Build a "City, State, Country" string from whatever the API
        # returned. Most listings include city + country at minimum.
        parts = [
            item_loc.get(k) for k in ("city", "stateOrProvince", "country")
            if item_loc.get(k)
        ]
        location = ", ".join(parts) if parts else None

    return EbayCompResult(
        item_id=str(item_id),
        title=str(title),
        price_amount=price_amount,
        currency=str(currency),
        end_time_iso=None,    # Browse summaries don't include end time
        view_url=it.get("itemWebUrl"),
        location=location,
    )


def _parse_completed_items(body: dict[str, Any]) -> list[EbayCompResult]:
    """Walk the (deeply-nested, array-everywhere) Finding API JSON and
    extract sold items. eBay's JSON wraps every value in a single-element
    array, so every field needs `[0]` to unwrap.
    """
    results: list[EbayCompResult] = []

    fcir = _safe(body, "findCompletedItemsResponse")
    if isinstance(fcir, list):
        fcir = fcir[0] if fcir else {}

    # Surface API-level errors so we don't silently return empty
    ack = _safe(fcir, "ack")
    if isinstance(ack, list) and ack and ack[0] in ("Failure", "PartialFailure"):
        err = _safe(fcir, "errorMessage")
        logger.warning("eBay API ack=%s err=%s", ack[0], str(err)[:300])

    items_container = _safe(fcir, "searchResult")
    if isinstance(items_container, list):
        items_container = items_container[0] if items_container else {}
    items = _safe(items_container, "item") or []
    if not isinstance(items, list):
        return []

    for it in items:
        try:
            sold = _extract_sold(it)
            if sold is not None:
                results.append(sold)
        except Exception as e:  # noqa: BLE001
            logger.debug("skipping eBay item, parse error: %s", e)
            continue

    return results


def _extract_sold(it: dict[str, Any]) -> EbayCompResult | None:
    """Pull (id, title, sold_price, currency, end_time, url, location)
    from one item dict. Returns None if essential fields are missing."""
    item_id = _first(it.get("itemId"))
    title = _first(it.get("title"))
    if not item_id or not title:
        return None

    selling = it.get("sellingStatus")
    if isinstance(selling, list) and selling:
        selling = selling[0]
    elif not isinstance(selling, dict):
        return None

    price_obj = selling.get("currentPrice")
    if isinstance(price_obj, list) and price_obj:
        price_obj = price_obj[0]
    elif not isinstance(price_obj, dict):
        # Sometimes the API returns convertedCurrentPrice instead
        price_obj = selling.get("convertedCurrentPrice")
        if isinstance(price_obj, list) and price_obj:
            price_obj = price_obj[0]
    if not isinstance(price_obj, dict):
        return None

    try:
        price_amount = float(price_obj.get("__value__"))
    except (TypeError, ValueError):
        return None
    currency = price_obj.get("@currencyId") or "USD"

    listing_info = it.get("listingInfo")
    if isinstance(listing_info, list) and listing_info:
        listing_info = listing_info[0]
    end_time = _first(listing_info.get("endTime")) if isinstance(listing_info, dict) else None

    view_url = _first(it.get("viewItemURL"))
    location = _first(it.get("location"))

    return EbayCompResult(
        item_id=str(item_id),
        title=str(title),
        price_amount=price_amount,
        currency=str(currency),
        end_time_iso=end_time,
        view_url=view_url,
        location=location,
    )


def _safe(d: Any, key: str) -> Any:
    if not isinstance(d, dict):
        return None
    return d.get(key)


def _first(v: Any) -> Any:
    """Finding API wraps everything in single-element arrays. Unwrap."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def to_comp_observations(results: list[EbayCompResult]) -> list[CompObservation]:
    """Adapt EbayCompResult → CompObservation for db.comps.insert_comps.
    The DB layer is source-agnostic; we just tag the row with
    source='ebay' on insert."""
    obs: list[CompObservation] = []
    for r in results:
        obs.append(CompObservation(
            price=r.price_amount,
            title=r.title,
            listing_url=r.view_url,
            location=r.location,
        ))
    return obs


# --- Module-level singleton ----------------------------------------------

_DEFAULT: EbayClient | None = None


def get_default_client() -> EbayClient | None:
    """Returns a reusable client, or None if eBay is disabled / no
    APP_ID. Callers should treat None as 'eBay path unavailable, use
    Marketplace fallback'."""
    global _DEFAULT
    if not is_ebay_enabled():
        return None
    if _DEFAULT is None:
        try:
            _DEFAULT = EbayClient()
        except ValueError as e:
            logger.warning("eBay client unavailable: %s", e)
            return None
    return _DEFAULT
