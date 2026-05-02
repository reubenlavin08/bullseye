"""Run the digest-email cycle once and exit.

Useful for:
  - manually triggering an immediate send
  - smoke-testing the digest output (use ALERT_BACKEND=console to print
    to stdout instead of sending mail)
  - cron-style usage if you don't want the long-running scheduler

Usage:
    python scripts/send_digests.py
    ALERT_BACKEND=console python scripts/send_digests.py    # dry-run
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.alerts.digest import send_pending_digests  # noqa: E402


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    stats = send_pending_digests()
    print(
        f"\nsent={stats['sent']} skipped_empty={stats['skipped_empty']} "
        f"failed={stats['failed']} total_listings={stats['total_listings']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
