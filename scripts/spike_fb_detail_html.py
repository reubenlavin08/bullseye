"""Phase 2a fallback spike: scrape a Marketplace listing's HTML page and
extract description + seller info from embedded JSON.

The search GraphQL endpoint returns slim cards (no description, no seller).
The proper fix is a second GraphQL call with the listing-detail doc_id,
but capturing that doc_id from DevTools is fiddly and the doc_id rotates.

This spike proves the HTML-page approach works as a fallback. Listing
detail pages embed their data in <script> tags via Relay's prefetched
stream cache. We pull it out, JSON-decode it, and extract description /
seller.

Usage:
    python scripts/spike_fb_detail_html.py 1318860066970714
    python scripts/spike_fb_detail_html.py <listing_id> --save-raw
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

LISTING_URL = "https://www.facebook.com/marketplace/item/{id}/"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass
class ListingDetail:
    id: str
    title: str | None
    description: str | None
    price_formatted: str | None
    seller_name: str | None
    seller_type: str | None
    location: str | None
    listed_at_unix: int | None
    is_pending: bool | None
    photo_urls: list[str]


# --- Description extraction strategies ----------------------------------

def _iter_script_jsons(html: str):
    """Yield decoded JSON objects from every <script type="application/json">
    tag in the page. These hold Relay's prefetched stream cache."""
    pattern = re.compile(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>',
        re.DOTALL,
    )
    for m in pattern.finditer(html):
        raw = m.group(1)
        if not raw or len(raw) < 50:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def _find_first(obj, predicate):
    """DFS for the first dict that satisfies predicate(d)."""
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


def _find_all(obj, predicate, out=None):
    if out is None:
        out = []
    if isinstance(obj, dict):
        if predicate(obj):
            out.append(obj)
        for v in obj.values():
            _find_all(v, predicate, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_all(v, predicate, out)
    return out


def extract_listing_detail(html: str, listing_id: str) -> ListingDetail:
    title = description = price_formatted = None
    seller_name = seller_type = location = None
    listed_at_unix: int | None = None
    is_pending: bool | None = None
    photo_urls: list[str] = []

    # Strategy 1: walk every <script type="application/json"> and find a
    # dict that has both id == listing_id AND a description-shaped field.
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

        title = title or node.get("marketplace_listing_title")

        desc = node.get("redacted_description") or node.get("description")
        if isinstance(desc, dict):
            description = description or desc.get("text")
        elif isinstance(desc, str):
            description = description or desc

        price = node.get("listing_price")
        if isinstance(price, dict):
            price_formatted = price_formatted or price.get("formatted_amount")

        seller = node.get("marketplace_listing_seller")
        if isinstance(seller, dict):
            seller_name = seller_name or seller.get("name")
            seller_type = seller_type or seller.get("__typename")

        loc = node.get("location")
        if isinstance(loc, dict):
            rg = loc.get("reverse_geocode")
            if isinstance(rg, dict):
                location = location or (
                    rg.get("display_name")
                    or rg.get("city")
                )

        if isinstance(node.get("creation_time"), int):
            listed_at_unix = listed_at_unix or node["creation_time"]
        if isinstance(node.get("is_pending"), bool):
            is_pending = node["is_pending"] if is_pending is None else is_pending

        photos = node.get("listing_photos") or []
        if isinstance(photos, list):
            for p in photos:
                uri = (
                    p.get("image", {}).get("uri")
                    if isinstance(p, dict)
                    else None
                )
                if uri:
                    photo_urls.append(uri)

        # If we got the description, we're done — no need to keep scanning.
        if description:
            break

    # Strategy 2: meta tags as last-resort backup (often truncated to ~200 chars)
    if not description:
        m = re.search(
            r'<meta\s+name="description"\s+content="([^"]+)"', html
        )
        if m:
            description = m.group(1)

    if not title:
        m = re.search(r"<title>([^<]+)</title>", html)
        if m:
            title = m.group(1).replace(" | Facebook Marketplace", "").strip()

    return ListingDetail(
        id=listing_id,
        title=title,
        description=description,
        price_formatted=price_formatted,
        seller_name=seller_name,
        seller_type=seller_type,
        location=location,
        listed_at_unix=listed_at_unix,
        is_pending=is_pending,
        photo_urls=photo_urls[:5],
    )


def fetch_listing_html(listing_id: str, timeout: int = 30) -> str:
    url = LISTING_URL.format(id=listing_id)
    resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("listing_id")
    p.add_argument("--save-raw", action="store_true",
                   help="Save the raw HTML next to this script for inspection.")
    args = p.parse_args()

    print(f"[detail-spike] fetching listing {args.listing_id}", file=sys.stderr)
    try:
        html = fetch_listing_html(args.listing_id)
    except requests.RequestException as e:
        print(f"[detail-spike] HTTP error: {e}", file=sys.stderr)
        return 2

    if args.save_raw:
        out = Path(__file__).parent / f"_detail_raw_{args.listing_id}.html"
        out.write_text(html, encoding="utf-8")
        print(f"[detail-spike] raw HTML saved -> {out}", file=sys.stderr)

    detail = extract_listing_detail(html, args.listing_id)
    print(json.dumps(asdict(detail), indent=2, ensure_ascii=False))

    if not detail.description:
        print("\n[detail-spike] WARNING: no description extracted.", file=sys.stderr)
        print("        Re-run with --save-raw and inspect the HTML.", file=sys.stderr)
        return 1

    print(f"\n[detail-spike] OK — description extracted "
          f"({len(detail.description)} chars).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
