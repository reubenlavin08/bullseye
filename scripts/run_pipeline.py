"""End-to-end pipeline runner.

Searches Facebook Marketplace, fetches each listing's detail,
applies price extraction + rejection filtering, prints a summary
table and optionally a JSON dump.

Run:
    python scripts/run_pipeline.py "electric scooter"
    python scripts/run_pipeline.py "iphone 14" --radius 60 --max-detail 5
    python scripts/run_pipeline.py "snowboard" --no-details   # search only
    python scripts/run_pipeline.py "ps5" --json out.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

# Make src/ importable when run directly without `pip install -e`
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.scraper.facebook import SearchParams  # noqa: E402
from deal_finder.scraper.pipeline import process_search  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("keyword")
    p.add_argument("--lat", type=float, default=49.2827)
    p.add_argument("--lng", type=float, default=-123.1207)
    p.add_argument("--radius", type=int, default=40, dest="radius_km")
    p.add_argument("--price-min", type=int, default=None)
    p.add_argument("--price-max", type=int, default=None)
    p.add_argument("--no-details", action="store_true",
                   help="Skip per-listing detail fetches (faster, no description).")
    p.add_argument("--max-detail", type=int, default=None,
                   help="Limit detail fetches to first N listings (debug throttle).")
    p.add_argument("--json", type=Path, default=None,
                   help="Dump full results as JSON to this path.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    params = SearchParams(
        keyword=args.keyword,
        lat=args.lat,
        lng=args.lng,
        radius_km=args.radius_km,
        price_min=args.price_min,
        price_max=args.price_max,
    )

    if args.max_detail is not None:
        # Hack: do a search-only run, then run a second pass with a
        # client and call detail manually for the top N. Simpler: just
        # patch the pipeline call by truncating after the search.
        # Implemented inline so we don't bloat pipeline.py with a debug knob.
        from deal_finder.scraper.facebook import get_default_client as gsc
        from deal_finder.scraper.facebook_detail import get_default_client as gdc
        from deal_finder.scraper.pipeline import _combine

        page = gsc().search(params)
        sub = page.listings[: args.max_detail]
        results = []
        for sl in sub:
            detail = gdc().fetch(sl.id)
            results.append(_combine(sl, detail))
        # And tack on the rest with no detail (so the table is complete).
        for sl in page.listings[args.max_detail:]:
            results.append(_combine(sl, None))
    else:
        results = process_search(params, fetch_details=not args.no_details)

    _print_table(results)

    if args.json:
        args.json.write_text(
            json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"\nfull results written -> {args.json}", file=sys.stderr)

    return 0 if results else 1


def _print_table(results) -> None:
    if not results:
        print("(no results)")
        return

    n = len(results)
    rejected = sum(1 for r in results if r.rejected)
    extracted = sum(1 for r in results if r.price_extracted_from_description)
    with_desc = sum(1 for r in results if r.description)

    print(f"\n{n} listings | {with_desc} with description | "
          f"{extracted} prices extracted | {rejected} rejected\n")

    print(f"  {'PRICE':>9}  {'TITLE':<48}  {'LOC':<22}  STATUS")
    print(f"  {'-'*9}  {'-'*48}  {'-'*22}  {'-'*30}")

    for r in results:
        title = (r.title or "")[:48]
        loc = (r.seller_location or "")[:22]
        price = r.price_formatted or "—"
        status_bits = []
        if r.rejected:
            status_bits.append(f"REJECTED ({r.rejection_reason})")
        if r.price_extracted_from_description:
            status_bits.append(f"$={r.resolved_price:.2f} (raw={r.raw_price:.2f})")
        if r.detail_source:
            status_bits.append(f"detail={r.detail_source}")
        elif r.description is None:
            status_bits.append("no-detail")
        status = " · ".join(status_bits) or "ok"
        print(f"  {price:>9}  {title:<48}  {loc:<22}  {status}")


if __name__ == "__main__":
    raise SystemExit(main())
