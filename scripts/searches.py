"""CLI for managing saved searches (the user_searches table).

The scheduler iterates over `active=TRUE` rows in user_searches and polls
each. This script lets you add / list / toggle searches without writing
SQL by hand.

Usage:
    python scripts/searches.py add "honda civic" --radius 40
    python scripts/searches.py add "iphone 14" --price-max 800
    python scripts/searches.py list
    python scripts/searches.py disable 3
    python scripts/searches.py enable 3
    python scripts/searches.py delete 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.db.connection import get_conn  # noqa: E402

# Vancouver default — same as everywhere else for now.
DEFAULT_LAT = 49.2827
DEFAULT_LNG = -123.1207


def cmd_add(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO user_searches
                       (keyword, latitude, longitude, radius_km,
                        price_min, price_max, active)
                       VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                       RETURNING id""",
                    (args.keyword, args.lat, args.lng, args.radius_km,
                     args.price_min, args.price_max),
                )
                new_id = cur.fetchone()[0]
    print(f"added search id={new_id}: {args.keyword!r}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, keyword, radius_km, price_min, price_max,
                          active, created_at
                   FROM user_searches
                   ORDER BY id""",
            )
            rows = cur.fetchall()

    if not rows:
        print("(no searches saved)")
        return 0

    print(f"{'id':>3}  {'on':<3}  {'kw':<35}  {'r_km':>5}  "
          f"{'min':>6}  {'max':>6}  created")
    print("-" * 80)
    for r in rows:
        rid, kw, rkm, pmin, pmax, active, created = r
        on = "yes" if active else "no"
        kw_s = (kw or "")[:35]
        pmin_s = f"${pmin}" if pmin else "-"
        pmax_s = f"${pmax}" if pmax else "-"
        c = created.strftime("%Y-%m-%d") if created else "-"
        print(f"{rid:>3}  {on:<3}  {kw_s:<35}  {rkm:>5}  "
              f"{pmin_s:>6}  {pmax_s:>6}  {c}")
    return 0


def cmd_set_active(args: argparse.Namespace, value: bool) -> int:
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE user_searches SET active = %s WHERE id = %s",
                    (value, args.id),
                )
                if cur.rowcount == 0:
                    print(f"no search with id={args.id}", file=sys.stderr)
                    return 1
    print(f"search {args.id} {'enabled' if value else 'disabled'}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_searches WHERE id = %s", (args.id,),
                )
                if cur.rowcount == 0:
                    print(f"no search with id={args.id}", file=sys.stderr)
                    return 1
    print(f"search {args.id} deleted")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add", help="Add a new saved search")
    add.add_argument("keyword")
    add.add_argument("--lat", type=float, default=DEFAULT_LAT)
    add.add_argument("--lng", type=float, default=DEFAULT_LNG)
    add.add_argument("--radius", type=int, default=40, dest="radius_km")
    add.add_argument("--price-min", type=int, default=None)
    add.add_argument("--price-max", type=int, default=None)

    sub.add_parser("list", help="List all saved searches")

    en = sub.add_parser("enable", help="Mark a search active=true")
    en.add_argument("id", type=int)

    di = sub.add_parser("disable", help="Mark a search active=false")
    di.add_argument("id", type=int)

    de = sub.add_parser("delete", help="Hard-delete a search row")
    de.add_argument("id", type=int)

    args = p.parse_args()
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "enable":
        return cmd_set_active(args, True)
    if args.cmd == "disable":
        return cmd_set_active(args, False)
    if args.cmd == "delete":
        return cmd_delete(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
