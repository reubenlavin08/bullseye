"""Re-evaluate rejection filter against every listing in the DB.

Useful after editing rejection_patterns.txt or rejection_keywords.txt:
existing rows keep whatever rejected/rejection_reason they had at scrape
time, so a config change doesn't apply retroactively unless we run this.

What it does:
  1. For every listing where description IS NOT NULL, re-run the
     rejection filter on (title + description).
  2. If the verdict changed from rejected=False -> rejected=True, mark
     the row rejected and clear any appraisal it had (it shouldn't have
     been scored).
  3. Print before/after counts so you can see what just got caught.

Run:
    python scripts/rescan_rejections.py
    python scripts/rescan_rejections.py --dry-run   (preview only)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.db.connection import get_conn  # noqa: E402
from deal_finder.scraper import rejection  # noqa: E402
from deal_finder.scraper.rejection import evaluate  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes; don't update the DB.")
    args = p.parse_args()

    rejection.reset_cache()  # ensure fresh config load

    flips = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, title, description, rejected, rejection_reason
                   FROM listings
                   WHERE description IS NOT NULL
                   ORDER BY scraped_at DESC""",
            )
            rows = cur.fetchall()

        for listing_id, title, description, was_rejected, old_reason in rows:
            r = evaluate(title or "", description or "")
            if r.rejected and not was_rejected:
                flips.append(("FLAG", listing_id, title, r.reason))
            elif not r.rejected and was_rejected:
                flips.append(("UNFLAG", listing_id, title, old_reason))

        print(f"scanned {len(rows)} listings with descriptions")
        print(f"verdicts changed: {len(flips)}")
        for kind, lid, title, reason in flips:
            print(f"  {kind:<6}  {lid}  {title[:60]:<60}  ({reason})")

        if args.dry_run:
            print("\n(dry run — no DB changes)")
            return 0

        if not flips:
            return 0

        with conn:
            with conn.cursor() as cur:
                for kind, lid, _, reason in flips:
                    if kind == "FLAG":
                        # Mark rejected and clear any appraisal that may
                        # have been computed before the new patterns landed.
                        cur.execute(
                            """UPDATE listings SET
                                  rejected = TRUE,
                                  rejection_reason = %s,
                                  appraised = FALSE,
                                  deal_score = NULL,
                                  fair_value = NULL,
                                  appraisal_note = NULL,
                                  appraisal_breakdown = NULL
                               WHERE id = %s""",
                            (reason, lid),
                        )
                    else:
                        cur.execute(
                            """UPDATE listings SET
                                  rejected = FALSE,
                                  rejection_reason = NULL
                               WHERE id = %s""",
                            (lid,),
                        )
        print(f"\nupdated {len(flips)} rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
