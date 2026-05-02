"""Phase 1 spike: hit Facebook Marketplace's internal GraphQL endpoint and
print real listing data.

This script is the GO/NO-GO gate for the entire pipeline. If this returns
real listings against your IP and your search params, the rest of the
pipeline is worth building. If it returns empty / 401 / blocked, fix that
first — open Marketplace in Chrome DevTools, capture a live request from
the Network tab, and update the doc_id values below.

Usage:
    python scripts/spike_fb_scraper.py
    python scripts/spike_fb_scraper.py --keyword "electric scooter"
    python scripts/spike_fb_scraper.py --keyword "iphone 14" --lat 49.2827 --lng -123.1207 --radius 40

Default search: "electric scooter" near Vancouver, 40 km radius.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Facebook internal GraphQL constants ----------------------------------
# Per the architecture doc. If these break, capture fresh ones from
# Marketplace > Chrome DevTools > Network tab > filter "graphql".
FB_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
LISTING_SEARCH_DOC_ID = "7111939778879383"
# Location search doc_id ("5585904654783609") is unused for now — we accept
# lat/lng directly from the caller.

# Headers chosen to mimic a logged-out Mac/Chrome browser. Public listing
# search works without cookies; if a future change requires them, capture
# from DevTools and add a --cookie flag.
DEFAULT_HEADERS = {
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
    "X-FB-Friendly-Name": "CometMarketplaceSearchContentContainerQuery",
}


# --- Output shape ---------------------------------------------------------

@dataclass
class RawListing:
    """Minimal extraction shape for the spike. The real scraper will
    return a richer dataclass; this is just enough to prove the endpoint
    works."""
    id: str
    title: str
    price_amount: float | None
    price_formatted: str | None
    previous_price: str | None
    is_pending: bool
    photo_url: str | None
    seller_name: str | None
    seller_location: str | None
    seller_type: str | None
    description: str
    listing_url: str


# --- Core fetch -----------------------------------------------------------

def build_variables(
    keyword: str,
    lat: float,
    lng: float,
    radius_km: int = 40,
    price_min: int | None = None,
    price_max: int | None = None,
    last_24h_only: bool = False,
    cursor: str | None = None,
) -> dict:
    """Build the `variables` JSON for the GraphQL POST.

    Prices are passed in dollars to FB's `filter_price_*_bound` (not cents,
    despite the doc's note — confirm against a live request if results look
    off)."""
    params = {
        "filter_location_latitude": lat,
        "filter_location_longitude": lng,
        "filter_radius_km": radius_km,
        "commerce_search_and_rp_available": True,
    }
    if price_min is not None:
        params["filter_price_lower_bound"] = price_min
    if price_max is not None:
        params["filter_price_upper_bound"] = price_max
    if last_24h_only:
        # 19062;19061 = "last 24h" per doc. May need refresh.
        params["commerce_search_and_rp_ctime_days"] = "19062;19061"

    variables: dict = {
        "params": {
            "bqf": {"callsite": "COMMERCE_MKTPLACE_WWW", "query": keyword},
            "browse_request_params": params,
            "custom_request_params": {"surface": "SEARCH"},
        },
        "count": 24,
    }
    if cursor:
        variables["cursor"] = cursor
    return variables


def fetch_listings_page(
    keyword: str,
    lat: float,
    lng: float,
    radius_km: int = 40,
    price_min: int | None = None,
    price_max: int | None = None,
    cursor: str | None = None,
    timeout: int = 30,
) -> dict:
    """POST a single page request and return the parsed JSON body."""
    variables = build_variables(
        keyword=keyword,
        lat=lat,
        lng=lng,
        radius_km=radius_km,
        price_min=price_min,
        price_max=price_max,
        cursor=cursor,
    )
    payload = {
        "doc_id": LISTING_SEARCH_DOC_ID,
        "variables": json.dumps(variables, separators=(",", ":")),
    }
    resp = requests.post(
        FB_GRAPHQL_URL,
        headers=DEFAULT_HEADERS,
        data=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.text
    # FB sometimes prepends "for (;;);" anti-JSON-hijack prefix.
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Some responses are NDJSON (one JSON object per line). Take the
        # first line that parses.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise


# --- Extraction -----------------------------------------------------------

def _safe_get(d: dict | None, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def extract_listings(payload: dict) -> tuple[list[RawListing], str | None]:
    """Walk the response and pull listing nodes out of the search edges.

    Returns (listings, end_cursor). end_cursor is None when there's no
    next page.

    The exact path is fragile — FB's GraphQL response shape changes. We
    try a couple of known shapes and fall back to a recursive scan that
    looks for objects with marketplace_listing_title.
    """
    edges: list[dict] = []
    end_cursor: str | None = None

    # Known path #1
    container = _safe_get(
        payload, "data", "marketplace_search", "feed_units", "edges",
    )
    if isinstance(container, list):
        edges = container
        end_cursor = _safe_get(
            payload, "data", "marketplace_search", "feed_units",
            "page_info", "end_cursor",
        )

    # Fallback: deep scan for any node that smells like a listing.
    if not edges:
        edges = _deep_find_listings(payload)

    listings: list[RawListing] = []
    for edge in edges:
        node = edge.get("node") if isinstance(edge, dict) else edge
        listing = _node_to_raw_listing(node)
        if listing is not None:
            listings.append(listing)

    return listings, end_cursor


def _deep_find_listings(obj) -> list[dict]:
    """Recursively find any dict with a marketplace_listing_title field.

    Defensive against shape drift — if the known path breaks but the
    listing nodes still exist somewhere in the payload, this finds them.
    """
    out: list[dict] = []

    def walk(x):
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


def _node_to_raw_listing(node: dict | None) -> RawListing | None:
    if not isinstance(node, dict):
        return None
    # Some edges wrap the listing one level deeper.
    listing = node.get("listing") if isinstance(node.get("listing"), dict) else node
    title = listing.get("marketplace_listing_title")
    listing_id = listing.get("id") or listing.get("legacy_id") or listing.get("ent_id")
    if not title or not listing_id:
        return None

    price_amount = None
    price_formatted = _safe_get(listing, "listing_price", "formatted_amount")
    raw_amount = _safe_get(listing, "listing_price", "amount")
    if raw_amount is not None:
        try:
            price_amount = float(raw_amount)
        except (TypeError, ValueError):
            price_amount = None

    previous_price = _safe_get(listing, "strikethrough_price", "formatted_amount")
    is_pending = bool(listing.get("is_pending") or listing.get("is_sold"))
    photo_url = _safe_get(listing, "primary_listing_photo", "image", "uri")
    seller_name = _safe_get(listing, "marketplace_listing_seller", "name")
    seller_type = _safe_get(listing, "marketplace_listing_seller", "__typename")
    seller_location = (
        _safe_get(listing, "location", "reverse_geocode", "city_page", "display_name")
        or _safe_get(listing, "location_text", "text")
        or _safe_get(listing, "location", "reverse_geocode", "city")
    )
    description = (
        _safe_get(listing, "redacted_description", "text")
        or _safe_get(listing, "description", "text")
        or ""
    )
    listing_url = f"https://www.facebook.com/marketplace/item/{listing_id}/"

    return RawListing(
        id=str(listing_id),
        title=title,
        price_amount=price_amount,
        price_formatted=price_formatted,
        previous_price=previous_price,
        is_pending=is_pending,
        photo_url=photo_url,
        seller_name=seller_name,
        seller_location=seller_location,
        seller_type=seller_type,
        description=description,
        listing_url=listing_url,
    )


# --- CLI ------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--keyword", default="electric scooter")
    p.add_argument("--lat", type=float, default=49.2827, help="default: Vancouver")
    p.add_argument("--lng", type=float, default=-123.1207)
    p.add_argument("--radius", type=int, default=40, dest="radius_km")
    p.add_argument("--price-min", type=int, default=None)
    p.add_argument("--price-max", type=int, default=None)
    p.add_argument(
        "--save-raw",
        action="store_true",
        help="Dump raw GraphQL response to scripts/_spike_output_<ts>.json",
    )
    args = p.parse_args()

    print(f"[spike] keyword={args.keyword!r} lat={args.lat} lng={args.lng} "
          f"radius_km={args.radius_km}", file=sys.stderr)

    try:
        payload = fetch_listings_page(
            keyword=args.keyword,
            lat=args.lat,
            lng=args.lng,
            radius_km=args.radius_km,
            price_min=args.price_min,
            price_max=args.price_max,
        )
    except requests.HTTPError as e:
        print(f"[spike] HTTP error: {e}", file=sys.stderr)
        print(f"[spike] body: {e.response.text[:500]}", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"[spike] network error: {e}", file=sys.stderr)
        return 2

    if args.save_raw:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = Path(__file__).parent / f"_spike_output_{ts}.json"
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[spike] raw payload saved -> {out_path}", file=sys.stderr)

    if "errors" in payload:
        print("[spike] GraphQL returned errors:", file=sys.stderr)
        print(json.dumps(payload["errors"], indent=2)[:2000], file=sys.stderr)

    listings, end_cursor = extract_listings(payload)
    print(f"[spike] extracted {len(listings)} listing(s); "
          f"end_cursor={'yes' if end_cursor else 'none'}", file=sys.stderr)

    # Pretty-print first few to stdout for human inspection.
    for i, lst in enumerate(listings[:5]):
        print(f"\n--- listing {i + 1} ---")
        print(json.dumps(asdict(lst), indent=2, ensure_ascii=False))

    if not listings:
        print("\n[spike] NO LISTINGS EXTRACTED.", file=sys.stderr)
        print("        Possible causes:", file=sys.stderr)
        print("          - doc_id is stale (capture fresh from DevTools)", file=sys.stderr)
        print("          - response shape changed (re-run with --save-raw and inspect)", file=sys.stderr)
        print("          - your IP is rate-limited / requires login", file=sys.stderr)
        return 1

    print(f"\n[spike] OK — pulled {len(listings)} listings.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
