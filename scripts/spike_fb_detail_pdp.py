"""Phase 2a primary path: hit the MarketplacePDPContainerQuery directly to
get a listing's full detail (description, seller, photos, posted_at).

This is the "official" detail fetch — same query Facebook's own front-end
uses when you click into a listing. Faster and more structured than the
HTML-page fallback (spike_fb_detail_html.py).

doc_id captured from a real DevTools session 2026-05-01. If it stops
returning data, recapture from Network tab > filter "graphql" > click any
listing > look for fb_api_req_friendly_name=MarketplacePDPContainerQuery.

Usage:
    python scripts/spike_fb_detail_pdp.py 1318860066970714
    python scripts/spike_fb_detail_pdp.py <listing_id> --save-raw
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

FB_GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
PDP_DOC_ID = "26284600011241990"
FRIENDLY_NAME = "MarketplacePDPContainerQuery"

# All __relay_internal__pv__* provider flags from the captured request.
# These are required — Relay won't run the query without them.
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
    "X-FB-Friendly-Name": FRIENDLY_NAME,
}


@dataclass
class PdpDetail:
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


def build_variables(target_id: str) -> dict:
    base = {
        "enableJobEmployerActionBar": False,
        "enableJobSeekerActionBar": False,
        "feedbackSource": 56,
        "feedLocation": "MARKETPLACE_MEGAMALL",
        "referralCode": "marketplace_top_picks",
        "referralSurfaceString": "browse_tab",
        "scale": 1,
        "targetId": str(target_id),
        "useDefaultActor": False,
    }
    return {**base, **RELAY_PROVIDERS}


def fetch_pdp(target_id: str, timeout: int = 30) -> dict:
    payload = {
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": FRIENDLY_NAME,
        "doc_id": PDP_DOC_ID,
        "server_timestamps": "true",
        "variables": json.dumps(build_variables(target_id), separators=(",", ":")),
    }
    resp = requests.post(
        FB_GRAPHQL_URL,
        headers=DEFAULT_HEADERS,
        data=payload,
        timeout=timeout,
    )
    resp.raise_for_status()
    text = resp.text
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


def _safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


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


def extract_pdp_detail(payload: dict, listing_id: str) -> PdpDetail:
    """Walk the PDP response for a listing-shaped node.

    The exact path varies by listing type (vehicle vs general goods vs
    rental). We do a structural search for any dict with both
    marketplace_listing_title and either redacted_description or
    description.
    """
    node = _find_first(
        payload,
        lambda d: (
            "marketplace_listing_title" in d
            and ("redacted_description" in d or "description" in d)
        ),
    ) or _find_first(
        payload,
        lambda d: "marketplace_listing_title" in d,
    )

    title = description = price_formatted = None
    seller_name = seller_type = location = None
    listed_at_unix: int | None = None
    is_pending: bool | None = None
    photo_urls: list[str] = []

    if isinstance(node, dict):
        title = node.get("marketplace_listing_title")
        desc = node.get("redacted_description") or node.get("description")
        if isinstance(desc, dict):
            description = desc.get("text")
        elif isinstance(desc, str):
            description = desc

        price_formatted = _safe_get(node, "listing_price", "formatted_amount")

        seller_name = _safe_get(node, "marketplace_listing_seller", "name")
        seller_type = _safe_get(node, "marketplace_listing_seller", "__typename")

        location = (
            _safe_get(node, "location", "reverse_geocode", "display_name")
            or _safe_get(node, "location", "reverse_geocode", "city")
            or _safe_get(node, "location_text", "text")
        )

        if isinstance(node.get("creation_time"), int):
            listed_at_unix = node["creation_time"]

        if isinstance(node.get("is_pending"), bool):
            is_pending = node["is_pending"]
        elif isinstance(node.get("is_sold"), bool) and node["is_sold"]:
            is_pending = True

        photos = node.get("listing_photos") or []
        if isinstance(photos, list):
            for p in photos:
                uri = (
                    _safe_get(p, "image", "uri")
                    if isinstance(p, dict)
                    else None
                )
                if uri:
                    photo_urls.append(uri)

    return PdpDetail(
        id=listing_id,
        title=title,
        description=description,
        price_formatted=price_formatted,
        seller_name=seller_name,
        seller_type=seller_type,
        location=location,
        listed_at_unix=listed_at_unix,
        is_pending=is_pending,
        photo_urls=photo_urls[:10],
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("listing_id")
    p.add_argument("--save-raw", action="store_true",
                   help="Dump raw GraphQL response next to this script.")
    args = p.parse_args()

    print(f"[pdp-spike] fetching listing {args.listing_id}", file=sys.stderr)
    try:
        payload = fetch_pdp(args.listing_id)
    except requests.HTTPError as e:
        print(f"[pdp-spike] HTTP error: {e}", file=sys.stderr)
        print(f"[pdp-spike] body: {e.response.text[:500]}", file=sys.stderr)
        return 2
    except requests.RequestException as e:
        print(f"[pdp-spike] network error: {e}", file=sys.stderr)
        return 2

    if args.save_raw:
        out = Path(__file__).parent / f"_pdp_raw_{args.listing_id}.json"
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[pdp-spike] raw payload saved -> {out}", file=sys.stderr)

    if "errors" in payload:
        print("[pdp-spike] GraphQL returned errors:", file=sys.stderr)
        print(json.dumps(payload["errors"], indent=2)[:1500], file=sys.stderr)

    detail = extract_pdp_detail(payload, args.listing_id)
    print(json.dumps(asdict(detail), indent=2, ensure_ascii=False))

    if not detail.description:
        print("\n[pdp-spike] WARNING: no description extracted.", file=sys.stderr)
        return 1

    print(f"\n[pdp-spike] OK — description extracted "
          f"({len(detail.description)} chars), "
          f"{len(detail.photo_urls)} photo(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
