"""Start the autonomous scheduler.

Polls every active saved search at POLL_INTERVAL_S (default 60s) and
fires the per-listing pipeline on any new IDs.

Usage:
    python scripts/run_scheduler.py
    POLL_INTERVAL_S=90 python scripts/run_scheduler.py
    LOG_LEVEL=DEBUG python scripts/run_scheduler.py

Stop with Ctrl+C.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.scheduler.main import run_forever  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run_forever())
