"""Drain the appraisal queue once and exit.

Run:
    python scripts/run_appraisal.py
    python scripts/run_appraisal.py --limit 5 -v
    python scripts/run_appraisal.py --warmup     # preload models, do nothing else
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.appraisal.worker import (  # noqa: E402
    drain_queue,
    warmup_models,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--lat", type=float, default=49.2827)
    p.add_argument("--lng", type=float, default=-123.1207)
    p.add_argument("--radius", type=int, default=1500, dest="radius_km",
                   help="Comp-search radius in km. Default 1500 covers "
                        "BC+AB+nearby; widen further if needed.")
    p.add_argument("--warmup", action="store_true",
                   help="Just preload models and exit. Useful before "
                        "the first real run to pay cold-start cost up front.")
    p.add_argument("--no-warmup", action="store_true",
                   help="Skip the warmup call (faster start if models are "
                        "already loaded).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    if args.warmup:
        warmup_models()
        print("models warmed.")
        return 0

    if not args.no_warmup:
        warmup_models()

    stats = drain_queue(
        limit=args.limit,
        lat=args.lat, lng=args.lng, radius_km=args.radius_km,
    )
    print(
        f"\nseen={stats.seen} appraised={stats.appraised} "
        f"skipped_no_score={stats.skipped_no_score} "
        f"errors={stats.skipped_bad_listing} "
        f"elapsed={stats.elapsed_s:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
