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
from .jobs import (
    drain_appraisal_safety_net,
    list_active_search_ids,
    poll_search,
    send_digest_emails,
)

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "60"))
SAFETY_NET_INTERVAL_S = int(os.environ.get("SAFETY_NET_INTERVAL_S", "600"))
RELOAD_INTERVAL_S = int(os.environ.get("RELOAD_INTERVAL_S", "300"))
DIGEST_INTERVAL_S = int(os.environ.get("DIGEST_INTERVAL_S", "180"))
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

    # Remove jobs for searches that are no longer active
    for jid in current - desired_ids:
        scheduler.remove_job(jid)
        logger.info("removed job %s", jid)

    # Add jobs for newly-active searches
    for sid in desired:
        jid = _job_id(sid)
        if jid in current:
            continue
        # Spread initial firings so we don't burst N searches at the
        # same instant on boot. Uses the search's id as the offset.
        first_run_offset = (sid * 7) % POLL_INTERVAL_S
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

    # Email digest sender — groups all pending matches per recipient.
    scheduler.add_job(
        send_digest_emails,
        trigger="interval",
        seconds=DIGEST_INTERVAL_S,
        id="digest_emails",
        name="send pending digest emails",
        max_instances=1,
        coalesce=True,
    )

    n_searches = len(list_active_search_ids())
    logger.info(
        "scheduler running: %d active search(es), poll=%ds, safety=%ds",
        n_searches, POLL_INTERVAL_S, SAFETY_NET_INTERVAL_S,
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
