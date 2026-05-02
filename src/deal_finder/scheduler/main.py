"""APScheduler entry point.

Runs the deal_finder pipeline autonomously:

  - One polling job per active user_search row, every POLL_INTERVAL_S
    seconds. Each tick scrapes Marketplace for that keyword and runs
    the full per-listing pipeline on any new IDs.

  - One safety-net appraisal sweep every SAFETY_NET_INTERVAL_S seconds
    to catch anything the polling jobs left behind.

Configuration knobs (env-overridable):

  POLL_INTERVAL_S          default 60   - seconds between scrapes per search
  SAFETY_NET_INTERVAL_S    default 600  - seconds between safety drains
  WARMUP_LLM_ON_BOOT       default 1    - preload Ollama models at start

Restart-safe: on boot we ALWAYS query active searches fresh from the
DB and (re)schedule them. New searches added via the CLI take effect
on the next reload tick (every RELOAD_INTERVAL_S seconds).
"""
from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import datetime, timedelta, timezone

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from ..appraisal.worker import warmup_models
from ..db.events import record_event
from .jobs import (
    drain_appraisal_safety_net,
    list_active_search_ids,
    poll_search,
    send_daily_summary_emails,
    send_digest_emails,
)

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "60"))
SAFETY_NET_INTERVAL_S = int(os.environ.get("SAFETY_NET_INTERVAL_S", "600"))
# Reload tick — how fast newly-saved searches get picked up. Was 300s
# (5 min) which felt sluggish. 20s gives near-instant pickup without
# hammering the DB.
RELOAD_INTERVAL_S = int(os.environ.get("RELOAD_INTERVAL_S", "20"))
DIGEST_INTERVAL_S = int(os.environ.get("DIGEST_INTERVAL_S", "15"))
DAILY_SUMMARY_INTERVAL_S = int(os.environ.get("DAILY_SUMMARY_INTERVAL_S", "3600"))
WARMUP_LLM_ON_BOOT = os.environ.get("WARMUP_LLM_ON_BOOT", "1") not in ("0", "")


def _job_id(search_id: int) -> str:
    return f"poll_search_{search_id}"


def reload_searches(scheduler: BlockingScheduler) -> None:
    """Sync the scheduler's job set to whatever's in user_searches.active=TRUE.

    Adds jobs for newly-active searches; removes jobs whose searches
    were disabled or deleted. Idempotent.
    """
    desired = set(list_active_search_ids())
    current = {
        j.id for j in scheduler.get_jobs() if j.id.startswith("poll_search_")
    }
    desired_ids = {_job_id(sid) for sid in desired}

    removed_ids: list[int] = []
    added_ids: list[int] = []

    # Remove jobs for searches that are no longer active
    for jid in current - desired_ids:
        scheduler.remove_job(jid)
        logger.info("removed job %s", jid)
        try:
            removed_ids.append(int(jid.rsplit("_", 1)[-1]))
        except ValueError:
            pass

    # Add jobs for newly-active searches
    for sid in desired:
        jid = _job_id(sid)
        if jid in current:
            continue
        # First run: a tiny stagger so adding 20 searches doesn't
        # fire 20 scrapes at the same instant. Capped at 12s, so a
        # newly-saved watch starts polling within seconds — not a
        # full POLL_INTERVAL_S delay.
        first_run_offset = min((sid * 3) % 12, 12)
        scheduler.add_job(
            poll_search,
            args=[sid],
            trigger="interval",
            seconds=POLL_INTERVAL_S,
            id=jid,
            name=f"poll search {sid}",
            max_instances=1,
            coalesce=True,
            next_run_time=(
                datetime.now(timezone.utc) + timedelta(seconds=first_run_offset)
            ),
            misfire_grace_time=POLL_INTERVAL_S,
        )
        logger.info("scheduled %s every %ds (first run +%ds)",
                    jid, POLL_INTERVAL_S, first_run_offset)
        added_ids.append(sid)

    if added_ids or removed_ids:
        record_event(
            "reload",
            added=added_ids,
            removed=removed_ids,
            total_active=len(desired),
        )


def _reload_tick(scheduler: BlockingScheduler) -> None:
    try:
        reload_searches(scheduler)
    except Exception as e:  # noqa: BLE001
        logger.exception("reload_searches failed: %s", e)


def run_forever() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    if WARMUP_LLM_ON_BOOT:
        try:
            logger.info("warming up Ollama models...")
            warmup_models()
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup failed (continuing anyway): %s", e)

    # ThreadPool size 4 — one search can run in parallel with the
    # safety net + reload + an extra slot. We don't go higher because
    # local Ollama serializes requests anyway.
    scheduler = BlockingScheduler(
        executors={"default": ThreadPoolExecutor(4)},
        timezone="UTC",
    )

    # Initial population
    reload_searches(scheduler)

    # Periodic resync so newly-added or disabled searches are picked up
    scheduler.add_job(
        _reload_tick,
        args=[scheduler],
        trigger="interval",
        seconds=RELOAD_INTERVAL_S,
        id="reload_searches",
        name="reload active searches",
        max_instances=1,
        coalesce=True,
    )

    # Safety-net appraisal drain
    scheduler.add_job(
        drain_appraisal_safety_net,
        trigger="interval",
        seconds=SAFETY_NET_INTERVAL_S,
        id="safety_net",
        name="safety net appraisal drain",
        max_instances=1,
        coalesce=True,
    )

    # Instant alert worker — runs every DIGEST_INTERVAL_S (default 15s).
    # Sends ONE digest per subscriber per tick whenever any of their
    # watches has a not-yet-notified, above-threshold listing.
    # Effectively 'instant' from the user's perspective.
    scheduler.add_job(
        send_digest_emails,
        trigger="interval",
        seconds=DIGEST_INTERVAL_S,
        id="instant_alerts",
        name="send pending instant-alert emails",
        max_instances=1,
        coalesce=True,
    )

    # Daily summary — rolling 24h roundup of scored-but-below-threshold
    # listings. Job ticks hourly; only fires for subscribers whose last
    # summary was 23+ hours ago.
    scheduler.add_job(
        send_daily_summary_emails,
        trigger="interval",
        seconds=DAILY_SUMMARY_INTERVAL_S,
        id="daily_summary",
        name="send rolling daily-summary emails",
        max_instances=1,
        coalesce=True,
    )

    n_searches = len(list_active_search_ids())
    logger.info(
        "scheduler running: %d active search(es), poll=%ds, safety=%ds",
        n_searches, POLL_INTERVAL_S, SAFETY_NET_INTERVAL_S,
    )
    record_event(
        "scheduler_boot",
        pid=os.getpid(),
        n_active_searches=n_searches,
        poll_interval_s=POLL_INTERVAL_S,
        safety_net_interval_s=SAFETY_NET_INTERVAL_S,
        reload_interval_s=RELOAD_INTERVAL_S,
        digest_interval_s=DIGEST_INTERVAL_S,
        alert_backend=os.environ.get("ALERT_BACKEND", "console"),
    )
    if n_searches == 0:
        logger.warning(
            "NO active searches in user_searches table. "
            "Add some via: python scripts/searches.py add \"<keyword>\" ...",
        )

    # Graceful shutdown on Ctrl+C
    def _shutdown(signum, frame):
        logger.info("received shutdown signal, stopping scheduler...")
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(run_forever())
