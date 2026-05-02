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
import logging.handlers
import os
import signal
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.blocking import BlockingScheduler

from ..appraisal.worker import warmup_models
from ..db.events import record_event
from .jobs import (
    coordinator_tick,
    drain_appraisal_safety_net,
    list_active_search_ids,
    send_daily_summary_emails,
    send_digest_emails,
)

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = int(os.environ.get("POLL_INTERVAL_S", "60"))  # legacy; ignored under coordinator
SAFETY_NET_INTERVAL_S = int(os.environ.get("SAFETY_NET_INTERVAL_S", "600"))
# Reload tick — how fast newly-saved searches get picked up. Was 300s
# (5 min) which felt sluggish. 20s gives near-instant pickup without
# hammering the DB.
RELOAD_INTERVAL_S = int(os.environ.get("RELOAD_INTERVAL_S", "20"))
DIGEST_INTERVAL_S = int(os.environ.get("DIGEST_INTERVAL_S", "15"))
DAILY_SUMMARY_INTERVAL_S = int(os.environ.get("DAILY_SUMMARY_INTERVAL_S", "3600"))
# Coordinator tick — how often the round-robin coordinator picks the
# stalest watch and polls it. Should be slightly larger than the FB
# client's rate gate (currently 8s) so we never hit the gate's queue.
# At N=49 watches and 9s tick, each watch polls every 49*9 = ~7.4 min.
# Pause watches you don't need to lower N and get faster polling.
COORDINATOR_TICK_S = int(os.environ.get("COORDINATOR_TICK_S", "20"))
WARMUP_LLM_ON_BOOT = os.environ.get("WARMUP_LLM_ON_BOOT", "1") not in ("0", "")


def reload_searches(scheduler: BlockingScheduler) -> None:
    """Recompute the active-watch count + log it.

    Under the coordinator pattern there's no per-watch APScheduler job
    to add/remove — the coordinator picks watches dynamically each
    tick. This function still runs periodically to:
      - report the current active-watch count via a 'reload' event
        (used by the dashboard)
      - log the effective per-watch poll interval given current N
    """
    active = list_active_search_ids()
    n = len(active)
    eff_per_watch_s = COORDINATOR_TICK_S * max(n, 1)
    logger.info(
        "reload: %d active watch(es); coordinator tick %ds; "
        "effective per-watch poll cadence ~%d sec (~%.1f min)",
        n, COORDINATOR_TICK_S, eff_per_watch_s, eff_per_watch_s / 60,
    )
    record_event(
        "reload",
        added=[],
        removed=[],
        total_active=n,
        coordinator_tick_s=COORDINATOR_TICK_S,
        effective_per_watch_s=eff_per_watch_s,
    )


def _reload_tick(scheduler: BlockingScheduler) -> None:
    try:
        reload_searches(scheduler)
    except Exception as e:  # noqa: BLE001
        logger.exception("reload_searches failed: %s", e)


def _setup_logging() -> Path | None:
    """Install handlers for both stdout (live terminal feel) AND a
    rotating file at logs/scheduler.log (so the dashboard can tail
    historical lines after restarts).

    Returns the log file path or None if the file handler couldn't be
    installed (e.g. read-only filesystem).
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    fmt = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

    # Reset root config: basicConfig is idempotent on re-imports but its
    # 'first call wins' semantics fight us when the webapp imports this
    # module before the scheduler starts. Clear handlers explicitly.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)
    formatter = logging.Formatter(fmt)

    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(formatter)
    root.addHandler(stdout_h)

    log_path = Path(__file__).resolve().parents[3] / "logs" / "scheduler.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # 10 MB rotation, keep 3 backups -> ~40 MB total max on disk.
        file_h = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        file_h.setFormatter(formatter)
        root.addHandler(file_h)
        return log_path
    except OSError as e:
        logging.warning("could not open log file %s: %s (stdout only)", log_path, e)
        return None


def run_forever() -> int:
    log_path = _setup_logging()
    if log_path:
        logger.info("scheduler logging to %s", log_path)

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

    # Initial reload tick (logs current active count + records event)
    reload_searches(scheduler)

    # ROUND-ROBIN COORDINATOR — single job that picks the stalest active
    # watch each tick and polls it. Replaces the old per-watch interval
    # jobs which over-saturated FB's rate limit at high N.
    #
    # First run +2s so we don't fire concurrently with reload_searches.
    scheduler.add_job(
        coordinator_tick,
        trigger="interval",
        seconds=COORDINATOR_TICK_S,
        id="coordinator",
        name="round-robin watch coordinator",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=2),
        misfire_grace_time=COORDINATOR_TICK_S,
    )
    logger.info("coordinator tick every %ds", COORDINATOR_TICK_S)

    # Periodic reload tick — under the coordinator pattern this just
    # logs the current active-watch count + emits a 'reload' event.
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
