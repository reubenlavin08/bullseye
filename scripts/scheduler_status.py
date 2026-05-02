"""Quick health check on the autonomous pipeline.

Run any time to see:
  * Active searches and when each was last polled (inferred from listings)
  * Total / appraised / unscoreable / rejected counts
  * The current appraisal queue depth
  * Top recent deals (score >= 70)

Usage:
    python scripts/scheduler_status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.db.connection import get_conn  # noqa: E402


def main() -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            print("=== Active searches ===")
            cur.execute(
                """SELECT us.id, us.keyword, us.radius_km, us.active,
                          (SELECT MAX(scraped_at) FROM listings l
                             WHERE l.search_id = us.id) AS last_scrape,
                          (SELECT COUNT(*) FROM listings l
                             WHERE l.search_id = us.id) AS total
                   FROM user_searches us
                   ORDER BY us.active DESC, us.id""",
            )
            rows = cur.fetchall()
            if not rows:
                print("(none — add some via scripts/searches.py add)")
            else:
                print(f"{'id':>3}  {'on':<3}  {'kw':<25}  {'r':>5}  "
                      f"{'last_scrape':<19}  {'total':>6}")
                for sid, kw, rkm, active, last, total in rows:
                    on = "yes" if active else "no"
                    last_s = last.strftime("%Y-%m-%d %H:%M:%S") if last else "(never)"
                    print(f"{sid:>3}  {on:<3}  {(kw or '')[:25]:<25}  "
                          f"{rkm:>5}  {last_s:<19}  {total:>6}")

            print("\n=== Pipeline counts ===")
            cur.execute(
                """SELECT
                      COUNT(*) AS total,
                      COUNT(*) FILTER (WHERE rejected) AS rejected,
                      COUNT(*) FILTER (WHERE appraised AND deal_score IS NULL) AS unscoreable,
                      COUNT(*) FILTER (WHERE appraised AND deal_score IS NOT NULL) AS scored,
                      COUNT(*) FILTER (
                          WHERE NOT appraised AND NOT rejected
                      ) AS pending_appraisal
                   FROM listings""",
            )
            t, rej, unsc, sco, pend = cur.fetchone()
            print(f"  total persisted:    {t}")
            print(f"  rejected:           {rej}")
            print(f"  unscoreable:        {unsc}")
            print(f"  scored:             {sco}")
            print(f"  pending appraisal:  {pend}  <- safety-net target")

            print("\n=== Top recent deals (score >= 70, last 24h) ===")
            cur.execute(
                """SELECT id, deal_score, fair_value, price, title,
                          appraised_at
                   FROM listings
                   WHERE deal_score >= 70
                     AND appraised_at >= NOW() - INTERVAL '24 hours'
                   ORDER BY deal_score DESC, appraised_at DESC
                   LIMIT 10""",
            )
            top = cur.fetchall()
            if not top:
                print("(none yet)")
            else:
                print(f"  {'score':>5}  {'asking':>9}  {'fair':>9}  title")
                for lid, score, fair, price, title, _ in top:
                    fair_s = f"${fair:.0f}" if fair else "-"
                    price_s = f"${price:.0f}" if price else "-"
                    print(f"  {score:>5}  {price_s:>9}  {fair_s:>9}  "
                          f"{(title or '')[:60]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
