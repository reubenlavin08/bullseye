"""Localhost Flask test UI for the deal_finder scraper.

Purpose: visually verify that the scraper pulls real Marketplace data and
that the price-extraction + rejection-filter logic behaves correctly,
before any DB or LLM is wired up.

Routes:
  GET  /              -- render the search form
  POST /search        -- run the scraper, render result cards
  GET  /detail/<id>   -- fetch one listing's description (PDP primary,
                         HTML fallback), return JSON for inline rendering

Run:
  cd deal_finder
  .venv\\Scripts\\python.exe -m webapp.app
  # then visit http://127.0.0.1:5000

This is dev-only, single-process, no auth, no rate limiting beyond the
scraper's own throttle. Don't expose to the internet.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path

# Load .env BEFORE any module that reads os.environ at import time.
# override=True forces .env values over pre-set Windows/shell env vars
# — critical when a stale key in the shell environment masks the
# updated value the user just put in .env.
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)

from flask import Flask, jsonify, render_template, request

# Make the package src/ tree importable when running without `pip install -e`.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from deal_finder.scraper.facebook import (  # noqa: E402
    SearchParams,
    get_default_client as get_search_client,
)
from deal_finder.scraper.facebook_detail import (  # noqa: E402
    get_default_client as get_detail_client,
)
from deal_finder.scraper.price_extraction import resolve_price  # noqa: E402
from deal_finder.scraper.rejection import evaluate as evaluate_rejection  # noqa: E402
from deal_finder.db.connection import get_conn  # noqa: E402


app = Flask(__name__, template_folder="templates", static_folder="static")

# Default geographic anchor — Vancouver (UBC area). Users can override
# per-search via the form.
DEFAULT_LAT = 49.2827
DEFAULT_LNG = -123.1207
DEFAULT_RADIUS_KM = 40


# --- Helpers --------------------------------------------------------------

def _enrich_listing(sl) -> dict:
    """Take a SearchListing from facebook.py and add pipeline annotations.

    Search results don't include descriptions, so price extraction here
    only fires off raw_price (cannot recover hidden prices). The
    /detail/<id> endpoint re-runs both filters once the description is
    fetched.

    Also looks up any existing appraisal in the DB (deal_score, note,
    confidence) so the UI can show the LLM's verdict without re-running
    the appraiser from the search page.
    """
    base = asdict(sl)
    raw_price = sl.price_amount or 0.0

    pr = resolve_price(raw_price, "")
    rj = evaluate_rejection(sl.title or "", description="")

    base["raw_price_numeric"] = raw_price
    base["resolved_price"] = pr.price
    base["price_extracted"] = pr.extracted
    base["rejected"] = rj.rejected
    base["rejection_reason"] = rj.reason

    # Pull any existing appraisal + posting metadata from Postgres.
    base["deal_score"] = None
    base["fair_value"] = None
    base["appraisal_note"] = None
    base["comp_median"] = None
    base["comp_sample_size"] = None
    base["comp_search_term"] = None
    base["comp_source"] = None
    base["appraisal_breakdown"] = None
    base["listed_at"] = None
    base["scraped_at"] = None
    base["posted_relative"] = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT deal_score, fair_value, appraisal_note,
                              comp_median, comp_sample_size,
                              comp_search_term, comp_source,
                              appraisal_breakdown,
                              listed_at, scraped_at
                       FROM listings WHERE id = %s""",
                    (sl.id,),
                )
                row = cur.fetchone()
                if row:
                    base["deal_score"] = row[0]
                    base["fair_value"] = float(row[1]) if row[1] is not None else None
                    base["appraisal_note"] = row[2]
                    base["comp_median"] = float(row[3]) if row[3] is not None else None
                    base["comp_sample_size"] = row[4]
                    base["comp_search_term"] = row[5]
                    base["comp_source"] = row[6]
                    base["appraisal_breakdown"] = row[7]
                    base["listed_at"] = row[8].isoformat() if row[8] else None
                    base["scraped_at"] = row[9].isoformat() if row[9] else None
                    # Prefer FB-listed-at; fall back to our scraped-at.
                    ts = row[8] or row[9]
                    if ts:
                        base["posted_relative"] = _humanize_age(ts)
    except Exception:  # noqa: BLE001
        # DB might be down; the rest of the UI should still render.
        pass

    return base


def _humanize_age(ts) -> str:
    """Turn a timestamp into 'Posted 3h ago', 'Posted yesterday', 'Posted Apr 28'."""
    from datetime import datetime, timezone
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    delta = now - ts
    secs = delta.total_seconds()
    if secs < 60:
        return "Just posted"
    if secs < 3600:
        m = int(secs // 60)
        return f"Posted {m}m ago"
    if secs < 86400:
        h = int(secs // 3600)
        return f"Posted {h}h ago"
    if secs < 86400 * 2:
        return "Posted yesterday"
    if secs < 86400 * 7:
        d = int(secs // 86400)
        return f"Posted {d}d ago"
    return f"Posted {ts.strftime('%b %-d')}" if hasattr(ts, "strftime") else "Posted earlier"


# --- Routes ---------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        defaults={
            "keyword": "",
            "lat": DEFAULT_LAT,
            "lng": DEFAULT_LNG,
            "radius_km": DEFAULT_RADIUS_KM,
            "price_min": "",
            "price_max": "",
        },
        results=None,
        meta=None,
        error=None,
    )


@app.route("/search", methods=["POST"])
def search():
    keyword = (request.form.get("keyword") or "").strip()
    if not keyword:
        return render_template(
            "index.html",
            defaults=_form_to_defaults(request.form),
            results=None,
            meta=None,
            error="Enter a search keyword.",
        ), 400

    try:
        lat = float(request.form.get("lat") or DEFAULT_LAT)
        lng = float(request.form.get("lng") or DEFAULT_LNG)
        radius_km = int(request.form.get("radius_km") or DEFAULT_RADIUS_KM)
        price_min_str = (request.form.get("price_min") or "").strip()
        price_max_str = (request.form.get("price_max") or "").strip()
        price_min = int(price_min_str) if price_min_str else None
        price_max = int(price_max_str) if price_max_str else None
    except ValueError:
        return render_template(
            "index.html",
            defaults=_form_to_defaults(request.form),
            results=None,
            meta=None,
            error="Bad numeric input — check lat/lng/radius/price fields.",
        ), 400

    t0 = time.perf_counter()
    try:
        page = get_search_client().search(SearchParams(
            keyword=keyword,
            lat=lat,
            lng=lng,
            radius_km=radius_km,
            price_min=price_min,
            price_max=price_max,
        ))
    except Exception as e:  # noqa: BLE001 -- surface anything to the UI
        return render_template(
            "index.html",
            defaults=_form_to_defaults(request.form),
            results=None,
            meta=None,
            error=f"Scrape failed: {type(e).__name__}: {e}",
        ), 502

    enriched = [_enrich_listing(sl) for sl in page.listings]
    elapsed_ms = int((time.perf_counter() - t0) * 1000)

    # Surface rate-limit / GraphQL errors to the user so an empty result
    # set isn't silently confusing. The scheduler hammers FB for poll
    # cycles; while it's running heavy, ad-hoc Test searches can be
    # blocked. Tell the user that explicitly.
    error = None
    if page.rate_limited:
        error = (
            "Facebook is rate-limiting our requests. The background "
            "scheduler is polling your saved watches, which uses our "
            "shared FB quota. Pause non-priority watches in the right "
            "panel (Manage tab) and try again in a minute."
        )
    elif page.error_message and not enriched:
        error = f"Facebook returned an error: {page.error_message}"

    meta = {
        "count": len(enriched),
        "elapsed_ms": elapsed_ms,
        "has_more": page.has_more,
        "rejected_count": sum(1 for e in enriched if e["rejected"]),
        "keyword": keyword,
    }

    return render_template(
        "index.html",
        defaults=_form_to_defaults(request.form),
        results=enriched,
        meta=meta,
        error=error,
    )


@app.route("/detail/<listing_id>")
def detail(listing_id: str):
    """Fetch one listing's description (PDP primary, HTML fallback) and
    re-run the pipeline filters with the description in hand."""
    detail_obj = get_detail_client().fetch(listing_id)

    pipeline = None
    if detail_obj.description is not None:
        title = request.args.get("title", "")
        raw_price = float(request.args.get("raw_price") or 0.0)
        pr = resolve_price(raw_price, detail_obj.description)
        rj = evaluate_rejection(title, description=detail_obj.description)
        pipeline = {
            "resolved_price": pr.price,
            "raw_price": pr.raw_price,
            "price_extracted": pr.extracted,
            "rejected": rj.rejected,
            "rejection_reason": rj.reason,
        }

    error = " | ".join(detail_obj.errors) if detail_obj.errors else None

    return jsonify({
        "listing_id": listing_id,
        "description": detail_obj.description,
        "source": detail_obj.source,
        "error": error,
        "pipeline": pipeline,
    })


_SCHEDULER_LOG_PATH = _REPO / "logs" / "scheduler.log"


@app.after_request
def _no_cache_for_live_endpoints(resp):
    """Stop browsers (and proxies) from caching live API responses.

    Without this, navigating away and back to /dashboard can show stale
    JSON because the URL is identical and the browser serves the
    cached body. Affects only /api/* responses — static files keep
    their default caching.
    """
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/dashboard")
def dashboard_page():
    """Render the observability dashboard. The page shells out to the
    /api/dashboard/* endpoints for data so the markup stays static and
    the JS just polls + repaints."""
    return render_template("dashboard.html")


def _compute_poll_timer() -> dict:
    """Compute the user-visible 'when does the next FB poll happen?' state.

    Mirrors the runtime logic in scheduler/jobs.py:
      - Cooldown: exponential by #rate-limit events in last 30 min,
        starting at 60s, capped at 600s, anchored to most recent
        rate-limit. While cooldown > 0, we skip ticks.
      - Slow-start: if SLOW_START_MODE is on, we also enforce a min
        interval between attempts that decays toward COORDINATOR_TICK_S
        as we go without rate-limits. Tracked in scheduler memory but
        we approximate from the most recent slow_start_ramp event.
      - Coordinator tick: fires every COORDINATOR_TICK_S regardless,
        so the ABSOLUTE upper bound on next-attempt is one tick.

    Returns a dict the dashboard renders:
      state: 'cooldown' | 'slow_start' | 'tick' | 'idle'
      next_attempt_in_s: seconds until next attempt (best estimate)
      last_attempt_iso: ISO timestamp of the most recent poll/blocked attempt
      cooldown:
        active: bool
        remaining_s: int
        total_s: int  (the full cooldown duration we're inside of)
        rate_limits_in_window: int (count over 30 min that drove the cooldown)
      slow_start:
        active: bool   (any slow_start_ramp event seen → assume on)
        min_interval_s: int  (current effective interval)
        elapsed_since_last_attempt_s: int
      coordinator_tick_s: int  (the floor — what cadence ticks fire at)
    """
    BASE = 60
    MAX = 600
    WINDOW = 1800
    DEFAULT_TICK = 20
    DEFAULT_INITIAL = 60
    DEFAULT_FLOOR = 20

    with get_conn() as conn:
        with conn.cursor() as cur:
            # Most recent rate-limit and how many in the last WINDOW seconds.
            # Pull the epoch too so we can mirror the jitter the
            # scheduler applies (deterministic per timestamp).
            cur.execute(
                f"""SELECT
                       COUNT(*),
                       MAX(created_at),
                       EXTRACT(EPOCH FROM MAX(created_at))
                   FROM scheduler_events
                   WHERE event_type = 'fb_rate_limit'
                     AND created_at >= NOW() - INTERVAL '{WINDOW} seconds'""",
            )
            rl_n, rl_last, rl_last_epoch = cur.fetchone()

            # Most recent attempt of any kind (poll OR rate-limit hit).
            cur.execute(
                """SELECT MAX(created_at) FROM scheduler_events
                   WHERE event_type IN ('poll','fb_rate_limit')""",
            )
            last_attempt = cur.fetchone()[0]

            # Most recent slow_start_ramp event tells us the current
            # effective slow-start min interval. Empty → use default.
            cur.execute(
                """SELECT detail->>'to_s', detail->>'direction'
                   FROM scheduler_events
                   WHERE event_type = 'slow_start_ramp'
                   ORDER BY created_at DESC
                   LIMIT 1""",
            )
            ss_row = cur.fetchone()

            # Most recent fb_probe event tells us whether FB itself is
            # healthy (down vs blocking us). 'ok' = closed circuit,
            # 'blocked' = our IP/fingerprint is flagged but FB is up,
            # 'down' = FB itself looks broken.
            cur.execute(
                """SELECT detail->>'result', created_at
                   FROM scheduler_events
                   WHERE event_type = 'fb_probe'
                   ORDER BY created_at DESC
                   LIMIT 1""",
            )
            probe_row = cur.fetchone()

    now = datetime.now(timezone.utc)

    # --- Cooldown ---
    # Mirror jobs.py::_compute_cooldown_remaining_s exactly, including
    # the decorrelated jitter (seeded per (n, last_epoch) so the value
    # is stable across dashboard polls within a single cooldown window).
    cooldown_remaining = 0
    cooldown_total = 0
    if rl_n and rl_last:
        nominal = min(BASE * (2 ** min(int(rl_n) - 1, 4)), MAX)
        import random as _r
        jitter_seed = int((rl_last_epoch or 0) * 1000) ^ int(rl_n) << 8
        rng = _r.Random(jitter_seed)
        cooldown_total = int(nominal * rng.uniform(0.85, 1.15))
        elapsed = (now - rl_last).total_seconds()
        cooldown_remaining = max(0, int(cooldown_total - elapsed))

    # --- Slow-start ---
    slow_start_active = ss_row is not None
    if ss_row and ss_row[0] is not None:
        try:
            slow_start_min = int(ss_row[0])
        except (TypeError, ValueError):
            slow_start_min = DEFAULT_INITIAL
    else:
        slow_start_min = DEFAULT_INITIAL

    seconds_since_last_attempt = (
        int((now - last_attempt).total_seconds()) if last_attempt else 999_999
    )

    # --- Resolve to a single state + next-attempt countdown ---
    if cooldown_remaining > 0:
        state = "cooldown"
        next_in = cooldown_remaining
    elif slow_start_active and seconds_since_last_attempt < slow_start_min:
        state = "slow_start"
        next_in = max(0, slow_start_min - seconds_since_last_attempt)
    else:
        # 'tick' state — waiting for the next coordinator firing.
        # APScheduler's interval trigger fires at fixed offsets from
        # job start, so the next tick is roughly:
        #   tick_s - (seconds_since_last_attempt mod tick_s)
        # Using last_attempt (latest poll OR rate-limit) as the
        # anchor: the coordinator only emits one of those each tick
        # it actually fires, so this approximates next_run_time
        # without reaching into APScheduler across processes. Returns
        # a value that DECREASES across syncs, so the JS countdown
        # anchor stays put and we don't get the 20→16→20 bounce.
        state = "tick"
        if last_attempt is not None:
            into_tick = seconds_since_last_attempt % DEFAULT_TICK
            # If we're exactly on a tick boundary (rare), call it 0
            # so the JS shows "now" rather than a misleading full tick.
            next_in = (DEFAULT_TICK - into_tick) if into_tick > 0 else 0
        else:
            next_in = DEFAULT_TICK

    # FB health classification — combines the most recent HTML probe
    # result with whether GraphQL is currently rate-limiting. The two
    # endpoints have DIFFERENT WAF policies:
    #   * HTML root: lenient. Real users hit it constantly so blocks
    #     here mean we're severely flagged.
    #   * GraphQL search: strict per-IP quota. Common to be gated here
    #     even when HTML is fine.
    # So 'probe ok + recent rate-limit' is its own state.
    probe_result = probe_row[0] if probe_row else None
    probe_at = probe_row[1].isoformat() if probe_row else None

    # "Recent" rate-limit = within the last 5 min. If we got rate-limited
    # AFTER the most recent probe (or no probe yet), GraphQL is still
    # gated even if HTML probe says 'ok'.
    rl_after_probe = (
        rl_last is not None and
        (probe_row is None or rl_last > probe_row[1])
    )
    very_recent_rl = rl_last is not None and (now - rl_last).total_seconds() < 300

    if probe_result == "blocked":
        fb_health = "blocked"           # HTML probe rejected — severely flagged
    elif probe_result == "down":
        fb_health = "fb_down"           # FB itself looks broken
    elif probe_result == "ok" and very_recent_rl and rl_after_probe:
        fb_health = "graphql_gated"     # HTML clear but GraphQL still rate-limited
    elif probe_result == "ok":
        fb_health = "ok"                # circuit fully closed
    else:
        # No probe yet. If we have very recent rate-limits, it's gated.
        fb_health = "graphql_gated" if very_recent_rl else "unknown"

    return {
        "state": state,
        "next_attempt_in_s": int(next_in),
        "last_attempt_iso": last_attempt.isoformat() if last_attempt else None,
        "fb_health": fb_health,
        "last_probe_at": probe_at,
        "cooldown": {
            "active": cooldown_remaining > 0,
            "remaining_s": int(cooldown_remaining),
            "total_s": int(cooldown_total),
            "rate_limits_in_window": int(rl_n or 0),
            "window_minutes": WINDOW // 60,
        },
        "slow_start": {
            "active": slow_start_active,
            "min_interval_s": int(slow_start_min),
            "elapsed_since_last_attempt_s": int(seconds_since_last_attempt)
                if last_attempt else None,
            "floor_s": DEFAULT_FLOOR,
            "initial_s": DEFAULT_INITIAL,
        },
        "coordinator_tick_s": DEFAULT_TICK,
    }


@app.route("/api/dashboard/summary")
def api_dashboard_summary():
    """Status strip + pipeline funnel + alive check.

    Light enough to poll every 5s. Aggregates a few counts from
    scheduler_events + listings.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # Is the scheduler alive? -> any 'poll' or 'reload' event in the
            # last 90 seconds.
            # Alive check: any event-type the scheduler emits qualifies
            # as a heartbeat. During an extended cooldown, polls and
            # reloads can both go quiet (poll won't fire, reload only
            # emits when the active-watch count CHANGES) — but the
            # scheduler is still healthy, just dutifully skipping ticks.
            # rate_limit_backoff, fb_probe, scheduler_heartbeat,
            # slow_start_ramp, fb_rate_limit are all proof-of-life.
            cur.execute(
                """SELECT MAX(created_at) FROM scheduler_events
                   WHERE event_type IN (
                       'poll','reload','safety_drain','scheduler_boot',
                       'rate_limit_backoff','fb_probe','scheduler_heartbeat',
                       'slow_start_ramp','fb_rate_limit'
                   )""",
            )
            last_event = cur.fetchone()[0]

            # Most-recent scheduler_boot — anchors the "since boot" counts
            # so a restart doesn't make it look like history was lost. The
            # DB still has everything from prior boots; this just gives a
            # natural "this run" framing.
            cur.execute(
                """SELECT MAX(created_at) FROM scheduler_events
                   WHERE event_type = 'scheduler_boot'""",
            )
            last_boot = cur.fetchone()[0]
            # Fallback for the first-ever-run case where there's no
            # scheduler_boot yet — use first event timestamp.
            if last_boot is None:
                cur.execute("SELECT MIN(created_at) FROM scheduler_events")
                last_boot = cur.fetchone()[0]

            cur.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE event_type='poll'
                            AND created_at >= NOW() - INTERVAL '1 hour'),
                       COUNT(*) FILTER (WHERE event_type='poll'
                            AND created_at >= COALESCE(%s, '-infinity'::timestamptz)),
                       COUNT(*) FILTER (WHERE event_type='fb_rate_limit'
                            AND created_at >= NOW() - INTERVAL '1 hour'),
                       COUNT(*) FILTER (WHERE event_type='fb_rate_limit'
                            AND created_at >= NOW() - INTERVAL '24 hours'),
                       COUNT(*) FILTER (WHERE event_type='fb_rate_limit'
                            AND created_at >= COALESCE(%s, '-infinity'::timestamptz)),
                       COUNT(*) FILTER (WHERE event_type='fb_rate_limit'),
                       COUNT(*) FILTER (WHERE event_type='email_sent'
                            AND created_at >= NOW()::date),
                       COUNT(*) FILTER (WHERE event_type='email_sent'),
                       COUNT(*) FILTER (WHERE event_type='email_failed'
                            AND created_at >= NOW()::date),
                       COUNT(*) FILTER (WHERE event_type='pipeline_error'
                            AND created_at >= NOW() - INTERVAL '24 hours')
                   FROM scheduler_events""",
                (last_boot, last_boot),
            )
            (polls_1h, polls_since_boot,
             rate_1h, rate_24h, rate_since_boot, rate_total,
             emails_today, emails_total,
             email_fail_today, errors_24h) = cur.fetchone()

            cur.execute(
                """SELECT
                       COUNT(*) FILTER (WHERE scraped_at >= NOW()::date) AS scraped,
                       COUNT(*) FILTER (WHERE rejected = TRUE
                            AND scraped_at >= NOW()::date) AS rejected,
                       COUNT(*) FILTER (WHERE appraised = TRUE
                            AND scraped_at >= NOW()::date) AS appraised,
                       COUNT(*) FILTER (WHERE deal_score IS NOT NULL
                            AND deal_score >= 70 AND rejected = FALSE
                            AND scraped_at >= NOW()::date) AS over_threshold,
                       COUNT(*) FILTER (WHERE notified = TRUE
                            AND notified_at >= NOW()::date) AS notified,
                       COUNT(*) FILTER (WHERE deal_score IS NOT NULL
                            AND deal_score >= 70 AND rejected = FALSE
                            AND notified = FALSE) AS pending
                   FROM listings""",
            )
            scraped, rej, appr, over, notif, pending = cur.fetchone()

            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE active=TRUE), COUNT(*) FROM user_searches",
            )
            active_w, total_w = cur.fetchone()

    poll_timer = _compute_poll_timer()

    return jsonify({
        "alive": _is_alive(last_event),
        "last_event_iso": last_event.isoformat() if last_event else None,
        "active_watches": active_w,
        "total_watches": total_w,
        "rates": {
            "polls_last_1h": polls_1h,
            "polls_since_boot": polls_since_boot,
            "rate_limits_last_1h": rate_1h,
            "rate_limits_last_24h": rate_24h,
            "rate_limits_since_boot": rate_since_boot,
            "rate_limits_total": rate_total,
            "emails_today": emails_today,
            "emails_total": emails_total,
            "email_failures_today": email_fail_today,
            "pipeline_errors_24h": errors_24h,
        },
        "scheduler_booted_at": last_boot.isoformat() if last_boot else None,
        "poll_timer": poll_timer,
        "funnel_today": {
            "scraped": scraped,
            "rejected": rej,
            "appraised": appr,
            "over_threshold": over,
            "notified": notif,
            "pending_unsent": pending,
        },
    })


def _is_alive(last_event) -> bool:
    """Heuristic: scheduler is 'alive' if it produced ANY event in the
    last 120 seconds. Coordinator ticks fire every 20s and even when
    skipping for cooldown they emit a scheduler_heartbeat at least
    every 60s. 120s covers a single missed heartbeat without a false
    alarm."""
    if last_event is None:
        return False
    age = (datetime.now(timezone.utc) - last_event).total_seconds()
    return age < 120


@app.route("/api/dashboard/events")
def api_dashboard_events():
    """Event tail for the live feed. ?since=<id> returns only newer rows
    so the UI can do incremental updates."""
    try:
        since = int(request.args.get("since", 0))
    except (TypeError, ValueError):
        since = 0
    try:
        limit = int(request.args.get("limit", 80))
    except (TypeError, ValueError):
        limit = 80
    limit = max(1, min(limit, 200))

    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT e.id, e.event_type, e.search_id, e.duration_ms,
                          e.detail, e.created_at, us.keyword
                   FROM scheduler_events e
                   LEFT JOIN user_searches us ON us.id = e.search_id
                   WHERE e.id > %s
                   ORDER BY e.id DESC
                   LIMIT %s""",
                (since, limit),
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r[0],
                    "event_type": r[1],
                    "search_id": r[2],
                    "duration_ms": r[3],
                    "detail": r[4],
                    "created_at": r[5].isoformat() if r[5] else None,
                    "keyword": r[6],
                })
    return jsonify({"events": rows})


@app.route("/api/dashboard/per-watch")
def api_dashboard_per_watch():
    """Per-watch performance table: polls (24h), success rate, average
    listings returned, hits, last scrape. Joins user_searches with the
    last 24h of poll events + listings counts."""
    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                       us.id, us.keyword, us.active,
                       COUNT(*) FILTER (
                            WHERE e.event_type='poll'
                              AND e.created_at >= NOW() - INTERVAL '24 hours'
                       ) AS polls_24h,
                       COUNT(*) FILTER (
                            WHERE e.event_type='fb_rate_limit'
                              AND e.created_at >= NOW() - INTERVAL '24 hours'
                       ) AS rate_lims_24h,
                       AVG((e.detail->>'raw_count')::int) FILTER (
                            WHERE e.event_type='poll'
                              AND e.created_at >= NOW() - INTERVAL '24 hours'
                       ) AS avg_raw,
                       (SELECT COUNT(*) FROM listings l
                            WHERE l.search_id=us.id
                              AND l.deal_score IS NOT NULL
                              AND l.deal_score >= 70
                              AND l.rejected = FALSE
                              AND l.scraped_at >= NOW() - INTERVAL '24 hours'
                       ) AS hits_24h,
                       (SELECT COUNT(*) FROM listings l
                            WHERE l.search_id=us.id
                              AND l.scraped_at >= NOW() - INTERVAL '24 hours'
                       ) AS scraped_24h,
                       (SELECT MAX(l.scraped_at) FROM listings l
                            WHERE l.search_id=us.id
                       ) AS last_scrape
                   FROM user_searches us
                   LEFT JOIN scheduler_events e ON e.search_id = us.id
                   GROUP BY us.id, us.keyword, us.active
                   ORDER BY us.active DESC, us.id""",
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r[0],
                    "keyword": r[1],
                    "active": bool(r[2]),
                    "polls_24h": int(r[3] or 0),
                    "rate_limits_24h": int(r[4] or 0),
                    "avg_raw": float(r[5]) if r[5] else None,
                    "hits_24h": int(r[6] or 0),
                    "scraped_24h": int(r[7] or 0),
                    "last_scrape_iso": r[8].isoformat() if r[8] else None,
                })
    return jsonify({"watches": rows})


@app.route("/api/dashboard/score-histogram")
def api_dashboard_histogram():
    """Histogram of deal scores from listings appraised in the last 24h.
    Buckets of 10 (0-9, 10-19, ..., 90-100). Used by the dashboard's
    score distribution chart."""
    buckets = [0] * 11   # 0-9, 10-19, ..., 90-100
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT deal_score FROM listings
                   WHERE deal_score IS NOT NULL
                     AND rejected = FALSE
                     AND appraised_at >= NOW() - INTERVAL '24 hours'""",
            )
            for (score,) in cur.fetchall():
                idx = min(int(score) // 10, 10)
                buckets[idx] += 1
    labels = [f"{i*10}-{i*10+9}" if i < 10 else "100" for i in range(11)]
    return jsonify({
        "buckets": [{"label": l, "count": c} for l, c in zip(labels, buckets)],
    })


@app.route("/api/dashboard/appraisal-feed")
def api_dashboard_appraisal_feed():
    """Per-listing live feed of everything flowing through the pipeline.

    Returns the most-recently-touched listings (newest first) with full
    pipeline status: rejected? scored? above-threshold? emailed?

    Query params:
      since_id  — return only listings with internal cursor > this value
                  (uses scraped_at + id for stable ordering across ticks)
      limit     — page size (default 60, max 200)
      filter    — one of: all (default), passed, scored, rejected, unscoreable

    Rows include:
      id, title, price, deal_score, fair_value, watch_keyword,
      rejected, rejection_reason, appraisal_note, listing_url, photo_url,
      scraped_at, appraised_at, notified, status
        ("emailed" | "passed" | "scored" | "unscoreable" | "rejected" | "pending")

    `status` is what the UI uses to color-code each row.
    """
    try:
        limit = int(request.args.get("limit", 60))
    except (TypeError, ValueError):
        limit = 60
    limit = max(1, min(limit, 200))
    filt = (request.args.get("filter") or "all").lower()
    since_id = request.args.get("since_id") or None

    where_clauses = []
    params: list = []

    if filt == "passed":
        where_clauses.append(
            "l.appraised = TRUE AND l.rejected = FALSE "
            "AND l.deal_score >= %s"
        )
        params.append(_default_threshold())
    elif filt == "scored":
        where_clauses.append("l.appraised = TRUE AND l.rejected = FALSE "
                             "AND l.deal_score IS NOT NULL")
    elif filt == "rejected":
        where_clauses.append("l.rejected = TRUE")
    elif filt == "unscoreable":
        where_clauses.append("l.appraised = TRUE AND l.deal_score IS NULL "
                             "AND l.rejected = FALSE")
    # 'all' adds no filter

    if since_id:
        # since_id is the listing's PK (TEXT). For pagination of "newer than"
        # we use the scraped_at + id tuple. Simplest: filter by id != since_id
        # and rely on ordering. Since id is text, the cleanest "newer" is
        # by scraped_at - we'll just request all rows scraped after the given
        # id's scraped_at.
        where_clauses.append(
            "(l.scraped_at, l.id) > "
            "(SELECT scraped_at, id FROM listings WHERE id = %s)"
        )
        params.append(since_id)

    where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
    threshold = _default_threshold()

    rows: list[dict] = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT l.id, l.title, l.price, l.deal_score, l.fair_value,
                          l.rejected, l.rejection_reason, l.appraisal_note,
                          l.listing_url, l.photo_url,
                          l.scraped_at, l.appraised_at,
                          l.notified, l.appraised,
                          l.comp_sample_size, l.comp_median,
                          l.comp_search_term, l.comp_source,
                          us.keyword,
                          l.seller_location,
                          us.latitude, us.longitude, us.radius_km
                   FROM listings l
                   LEFT JOIN user_searches us ON us.id = l.search_id
                   WHERE {where_sql}
                   ORDER BY l.scraped_at DESC, l.id DESC
                   LIMIT %s""",
                (*params, limit),
            )
            from deal_finder.db.geo import geocode_city, haversine_km
            for r in cur.fetchall():
                (lid, title, price, score, fair, rejected, rej_reason,
                 appraisal_note, listing_url, photo_url,
                 scraped_at, appraised_at, notified, appraised,
                 comp_n, comp_median, comp_term, comp_source, keyword,
                 seller_location, watch_lat, watch_lng, watch_radius) = r

                # Compute distance from the watch's home to the
                # listing's geocoded city centroid. Cheap — geocode_city
                # is DB-cached and unique cities repeat heavily across
                # the feed. None when we can't resolve location.
                distance_km = None
                if seller_location and watch_lat is not None and watch_lng is not None:
                    coords = geocode_city(seller_location)
                    if coords is not None:
                        distance_km = round(
                            haversine_km(
                                float(watch_lat), float(watch_lng),
                                coords[0], coords[1],
                            ),
                            1,
                        )

                # Status classification — the row's color tag in the UI.
                if rejected:
                    status = "rejected"
                elif notified:
                    status = "emailed"
                elif appraised and score is not None and score >= threshold:
                    status = "passed"
                elif appraised and score is not None:
                    status = "scored"
                elif appraised and score is None:
                    status = "unscoreable"
                else:
                    status = "pending"

                rows.append({
                    "id": lid,
                    "title": title,
                    "price": float(price) if price is not None else None,
                    "deal_score": int(score) if score is not None else None,
                    "fair_value": float(fair) if fair is not None else None,
                    "rejected": bool(rejected),
                    "rejection_reason": rej_reason,
                    "appraisal_note": appraisal_note,
                    "listing_url": listing_url,
                    "photo_url": photo_url,
                    "scraped_at": scraped_at.isoformat() if scraped_at else None,
                    "appraised_at": appraised_at.isoformat() if appraised_at else None,
                    "notified": bool(notified),
                    "appraised": bool(appraised),
                    "comp_sample_size": int(comp_n) if comp_n is not None else None,
                    "comp_median": float(comp_median) if comp_median is not None else None,
                    "comp_search_term": comp_term,
                    "comp_source": comp_source,
                    "keyword": keyword,
                    "status": status,
                    "seller_location": seller_location,
                    "distance_km": distance_km,
                    "watch_radius_km": int(watch_radius) if watch_radius is not None else None,
                })

    return jsonify({"listings": rows, "threshold": threshold, "filter": filt})


def _default_threshold() -> int:
    """Threshold used to color-code 'passed' rows on the appraisal feed.
    Pulls from env (ALERT_SCORE_THRESHOLD) — the same default that
    governs new subscribers. Per-subscriber thresholds still apply for
    actual email sending; this is just for visual consistency on the
    dashboard."""
    try:
        return int(os.environ.get("ALERT_SCORE_THRESHOLD", "70"))
    except (TypeError, ValueError):
        return 70


@app.route("/api/dashboard/breakdown/<listing_id>")
def api_dashboard_breakdown(listing_id: str):
    """Return the full score-computation breakdown for a single listing.

    Used by the dashboard's 'why?' chip that opens a drawer showing
    exactly how a score was derived: percentile rank vs comps,
    confidence interval, condition signals, fair-value calc, plus
    secondary-check verdict if one ran. Source-of-truth for 'is the
    scoring math working as expected' debugging.
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.id, l.title, l.price, l.deal_score, l.fair_value,
                          l.appraisal_note, l.appraisal_breakdown,
                          l.comp_median, l.comp_mean, l.comp_min, l.comp_max,
                          l.comp_sample_size, l.comp_search_term, l.comp_source,
                          l.rejected, l.rejection_reason, l.notified,
                          l.listing_url, l.seller_location,
                          us.keyword, us.latitude, us.longitude, us.radius_km
                   FROM listings l
                   LEFT JOIN user_searches us ON us.id = l.search_id
                   WHERE l.id = %s""",
                (listing_id,),
            )
            row = cur.fetchone()

    if not row:
        return jsonify({"error": "listing not found"}), 404

    (lid, title, price, score, fair, note, breakdown,
     cmed, cmean, cmin, cmax, csize, cterm, csource,
     rejected, rej_reason, notified,
     listing_url, seller_loc, keyword,
     watch_lat, watch_lng, watch_radius) = row

    # Compute distance the same way the appraisal feed does.
    distance_km = None
    if seller_loc and watch_lat is not None and watch_lng is not None:
        from deal_finder.db.geo import geocode_city, haversine_km
        coords = geocode_city(seller_loc)
        if coords is not None:
            distance_km = round(
                haversine_km(
                    float(watch_lat), float(watch_lng),
                    coords[0], coords[1],
                ), 1,
            )

    # Pull the most recent secondary_check event for this listing
    # (if any) so the drawer can show the LLM's verdict + concern.
    sec_check = None
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT detail FROM scheduler_events
                   WHERE event_type = 'secondary_check'
                     AND detail->>'listing_id' = %s
                   ORDER BY created_at DESC LIMIT 1""",
                (listing_id,),
            )
            row2 = cur.fetchone()
            if row2:
                sec_check = row2[0]

    return jsonify({
        "id": lid,
        "title": title,
        "price": float(price) if price is not None else None,
        "deal_score": int(score) if score is not None else None,
        "fair_value": float(fair) if fair is not None else None,
        "appraisal_note": note,
        "breakdown": breakdown or {},
        "comp": {
            "search_term": cterm,
            "source": csource,
            "sample_size": csize,
            "median": float(cmed) if cmed is not None else None,
            "mean": float(cmean) if cmean is not None else None,
            "min": float(cmin) if cmin is not None else None,
            "max": float(cmax) if cmax is not None else None,
        },
        "rejected": bool(rejected),
        "rejection_reason": rej_reason,
        "notified": bool(notified),
        "listing_url": listing_url,
        "keyword": keyword,
        "seller_location": seller_loc,
        "distance_km": distance_km,
        "watch_radius_km": int(watch_radius) if watch_radius is not None else None,
        "secondary_check": sec_check,
    })


@app.route("/api/dashboard/log/tail")
def api_dashboard_log_tail():
    """Return the last N lines of logs/scheduler.log for the live tail.

    Query param: n (default 200, max 1000). The scheduler writes to this
    file via a RotatingFileHandler (10 MB cap, 3 backups). The dashboard
    polls this every 2-3 seconds.

    Reading the last N lines of a file efficiently means seeking from the
    end, but for files in the low-MB range the simpler approach is to
    just read the whole file and slice. We cap N at 1000 to bound the
    response size and the read window.
    """
    try:
        n = int(request.args.get("n", 200))
    except (TypeError, ValueError):
        n = 200
    n = max(1, min(n, 1000))

    if not _SCHEDULER_LOG_PATH.exists():
        return jsonify({
            "lines": [],
            "path": str(_SCHEDULER_LOG_PATH),
            "exists": False,
            "warning": "scheduler hasn't started yet — no log file found",
        })

    try:
        # Seek-from-end approach: read up to ~256 KB from the tail and
        # split. Plenty for 1000 typical log lines (~120 chars each).
        size = _SCHEDULER_LOG_PATH.stat().st_size
        read_window = min(size, 256 * 1024)
        with _SCHEDULER_LOG_PATH.open("rb") as f:
            f.seek(size - read_window)
            chunk = f.read().decode("utf-8", errors="replace")
        # Drop a possibly-truncated first line if we didn't start at 0
        lines = chunk.splitlines()
        if size > read_window and lines:
            lines = lines[1:]
        lines = lines[-n:]
    except OSError as e:
        return jsonify({"lines": [], "error": str(e)}), 500

    return jsonify({
        "lines": lines,
        "path": str(_SCHEDULER_LOG_PATH),
        "exists": True,
        "total_returned": len(lines),
    })


@app.route("/api/searches")
def api_searches():
    """Return active saved searches — used to populate the subscribe
    dropdown so users can pick which alerts they want."""
    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, keyword, radius_km
                   FROM user_searches
                   WHERE active = TRUE
                   ORDER BY id""",
            )
            for r in cur.fetchall():
                rows.append({"id": r[0], "keyword": r[1], "radius_km": r[2]})
    return jsonify({"searches": rows})


# ----- Per-watch dashboard endpoints --------------------------------------
#
# /api/watches      GET   list every watch (active + inactive) with subscriber
#                         info + recent hit count
# /api/watches/<id> PATCH update threshold / active / daily_summary_enabled
#                         / price_min / price_max / radius_km
# /api/watches/<id> DELETE delete the watch + cascade subscribers
#
# A "watch" in UI terms = (user_searches row joined with subscriber row for
# the current single user). For multi-user we'd scope by user_id.

@app.route("/api/watches", methods=["GET"])
def api_watches_list():
    """List every saved watch with subscriber prefs + recent activity stats."""
    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
                       us.id, us.keyword, us.radius_km, us.price_min, us.price_max,
                       us.latitude, us.longitude, us.active, us.created_at,
                       us.must_include, us.must_exclude,
                       s.email, s.score_threshold, s.daily_summary_enabled,
                       (SELECT COUNT(*) FROM listings l
                          WHERE l.search_id = us.id) AS total_seen,
                       (SELECT COUNT(*) FROM listings l
                          WHERE l.search_id = us.id
                            AND l.deal_score IS NOT NULL
                            AND l.deal_score >= COALESCE(s.score_threshold, 70)
                            AND l.rejected = FALSE) AS hit_count,
                       (SELECT MAX(l.scraped_at) FROM listings l
                          WHERE l.search_id = us.id) AS last_scrape
                   FROM user_searches us
                   LEFT JOIN subscribers s ON s.search_id = us.id
                   ORDER BY us.active DESC, us.id DESC""",
            )
            for r in cur.fetchall():
                rows.append({
                    "id": r[0],
                    "keyword": r[1],
                    "radius_km": r[2],
                    "price_min": r[3],
                    "price_max": r[4],
                    "latitude": float(r[5]) if r[5] is not None else None,
                    "longitude": float(r[6]) if r[6] is not None else None,
                    "active": bool(r[7]),
                    "created_at": r[8].isoformat() if r[8] else None,
                    "must_include": r[9],
                    "must_exclude": r[10],
                    "email": r[11],
                    "score_threshold": r[12] if r[12] is not None else None,
                    "daily_summary_enabled": bool(r[13]) if r[13] is not None else False,
                    "total_seen": int(r[14] or 0),
                    "hit_count": int(r[15] or 0),
                    "last_scrape": r[16].isoformat() if r[16] else None,
                })
    return jsonify({"watches": rows})


@app.route("/api/watches/<int:watch_id>", methods=["PATCH"])
def api_watches_patch(watch_id: int):
    """Update one watch's prefs. Form-encoded or JSON body, partial updates ok.

    Editable fields:
      active                  bool — turn the watch on/off (also pauses alerts)
      score_threshold         int 0-100 (subscriber row)
      daily_summary_enabled   bool (subscriber row)
      radius_km               int 1-500
      price_min               int or null
      price_max               int or null
    """
    data = request.form if request.form else (request.get_json(silent=True) or {})

    # Coerce inputs. Allow null/'' to mean "unset" for nullable fields.
    def _opt_int(name: str) -> int | None:
        v = data.get(name)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer")

    def _opt_bool(name: str) -> bool | None:
        if name not in data:
            return None
        v = data.get(name)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    # us_* updates the user_searches row, sub_* updates subscribers
    us_updates: dict[str, object] = {}
    sub_updates: dict[str, object] = {}

    try:
        if "active" in data:
            us_updates["active"] = _opt_bool("active")
            # Mirror to subscribers so the digest worker also stops emailing
            sub_updates["active"] = us_updates["active"]
        if "radius_km" in data:
            r_km = _opt_int("radius_km")
            if r_km is None or not (1 <= r_km <= 500):
                return jsonify({"ok": False, "error": "radius_km must be 1-500"}), 400
            us_updates["radius_km"] = r_km
        if "price_min" in data:
            us_updates["price_min"] = _opt_int("price_min")
        if "price_max" in data:
            us_updates["price_max"] = _opt_int("price_max")
        if "must_include" in data:
            v = (data.get("must_include") or "").strip()
            us_updates["must_include"] = v if v else None
        if "must_exclude" in data:
            v = (data.get("must_exclude") or "").strip()
            us_updates["must_exclude"] = v if v else None
        if "score_threshold" in data:
            t = _opt_int("score_threshold")
            if t is None or not (0 <= t <= 100):
                return jsonify({"ok": False, "error": "score_threshold must be 0-100"}), 400
            sub_updates["score_threshold"] = t
        if "daily_summary_enabled" in data:
            sub_updates["daily_summary_enabled"] = _opt_bool("daily_summary_enabled")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not us_updates and not sub_updates:
        return jsonify({"ok": False, "error": "no valid fields to update"}), 400

    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                # Confirm the watch exists
                cur.execute("SELECT 1 FROM user_searches WHERE id = %s", (watch_id,))
                if cur.fetchone() is None:
                    return jsonify({"ok": False, "error": "watch not found"}), 404

                if us_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in us_updates)
                    vals = list(us_updates.values()) + [watch_id]
                    cur.execute(
                        f"UPDATE user_searches SET {set_clause} WHERE id = %s",
                        vals,
                    )

                if sub_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in sub_updates)
                    vals = list(sub_updates.values()) + [watch_id]
                    cur.execute(
                        f"UPDATE subscribers SET {set_clause} WHERE search_id = %s",
                        vals,
                    )

    return jsonify({"ok": True, "id": watch_id, "updated": {**us_updates, **sub_updates}})


@app.route("/api/watches/bulk-update", methods=["POST"])
def api_watches_bulk_update():
    """Apply the same field update to EVERY watch (or a filtered subset).

    Body fields (any subset; same semantics as PATCH /api/watches/<id>):
      active, score_threshold, daily_summary_enabled,
      radius_km, price_min, price_max,
      must_include, must_exclude

    Optional filter:
      only_active=true    -> apply only to currently-active watches
                             (skip paused ones, useful for "set thresholds"
                             without un-pausing things)

    Empty body or no recognized fields -> 400. Always returns the
    counts of rows actually touched on user_searches and subscribers.
    """
    data = request.form if request.form else (request.get_json(silent=True) or {})

    def _opt_int(name: str) -> int | None:
        v = data.get(name)
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be an integer")

    def _opt_bool(name: str) -> bool | None:
        if name not in data:
            return None
        v = data.get(name)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    us_updates: dict[str, object] = {}
    sub_updates: dict[str, object] = {}

    try:
        if "active" in data:
            us_updates["active"] = _opt_bool("active")
            sub_updates["active"] = us_updates["active"]
        if "radius_km" in data:
            r_km = _opt_int("radius_km")
            if r_km is None or not (1 <= r_km <= 500):
                return jsonify({"ok": False, "error": "radius_km must be 1-500"}), 400
            us_updates["radius_km"] = r_km
        if "price_min" in data:
            us_updates["price_min"] = _opt_int("price_min")
        if "price_max" in data:
            us_updates["price_max"] = _opt_int("price_max")
        if "must_include" in data:
            v = (data.get("must_include") or "").strip()
            us_updates["must_include"] = v if v else None
        if "must_exclude" in data:
            v = (data.get("must_exclude") or "").strip()
            us_updates["must_exclude"] = v if v else None
        if "score_threshold" in data:
            t = _opt_int("score_threshold")
            if t is None or not (0 <= t <= 100):
                return jsonify({"ok": False, "error": "score_threshold must be 0-100"}), 400
            sub_updates["score_threshold"] = t
        if "daily_summary_enabled" in data:
            sub_updates["daily_summary_enabled"] = _opt_bool("daily_summary_enabled")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not us_updates and not sub_updates:
        return jsonify({"ok": False, "error": "no valid fields to update"}), 400

    only_active = str(data.get("only_active", "")).strip().lower() in ("1", "true", "yes", "on")

    us_filter = " WHERE active = TRUE" if only_active else ""
    sub_filter_join = (
        " AND search_id IN (SELECT id FROM user_searches WHERE active = TRUE)"
        if only_active else ""
    )

    us_count = 0
    sub_count = 0
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                if us_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in us_updates)
                    cur.execute(
                        f"UPDATE user_searches SET {set_clause}{us_filter}",
                        list(us_updates.values()),
                    )
                    us_count = cur.rowcount
                if sub_updates:
                    set_clause = ", ".join(f"{k} = %s" for k in sub_updates)
                    cur.execute(
                        f"UPDATE subscribers SET {set_clause} WHERE TRUE{sub_filter_join}",
                        list(sub_updates.values()),
                    )
                    sub_count = cur.rowcount

    return jsonify({
        "ok": True,
        "watches_updated": us_count,
        "subscribers_updated": sub_count,
        "fields": {**us_updates, **sub_updates},
        "only_active": only_active,
    })


@app.route("/api/watches/<int:watch_id>", methods=["DELETE"])
def api_watches_delete(watch_id: int):
    """Delete a watch outright. Subscribers cascade via FK ON DELETE CASCADE.

    Listings tied to this search keep search_id NULL'd (FK ON DELETE SET NULL)
    — they stay in the DB for historical comp data but become orphan rows.
    """
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_searches WHERE id = %s RETURNING keyword",
                    (watch_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return jsonify({"ok": False, "error": "watch not found"}), 404
    return jsonify({"ok": True, "id": watch_id, "keyword": row[0]})


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    """Read or upsert the single-user home location.

    GET  -> { home_label, home_latitude, home_longitude, updated_at }
            (any field may be null if never set)

    POST -> form-encoded or JSON with the same fields. Upserts the row
            for user_id=1.
    """
    if request.method == "GET":
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT home_label, home_latitude, home_longitude, updated_at
                       FROM user_settings WHERE user_id = 1""",
                )
                row = cur.fetchone()
        if not row:
            return jsonify({
                "home_label": None,
                "home_latitude": None,
                "home_longitude": None,
                "updated_at": None,
            })
        return jsonify({
            "home_label": row[0],
            "home_latitude": float(row[1]) if row[1] is not None else None,
            "home_longitude": float(row[2]) if row[2] is not None else None,
            "updated_at": row[3].isoformat() if row[3] else None,
        })

    # POST
    data = request.form if request.form else (request.get_json(silent=True) or {})
    label = (data.get("home_label") or "").strip() or None
    try:
        lat = float(data.get("home_latitude")) if data.get("home_latitude") not in (None, "") else None
        lng = float(data.get("home_longitude")) if data.get("home_longitude") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "lat/lng must be numbers"}), 400
    if lat is None or lng is None:
        return jsonify({"ok": False, "error": "home_latitude and home_longitude required"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return jsonify({"ok": False, "error": "lat/lng out of range"}), 400

    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO user_settings (user_id, home_label, home_latitude, home_longitude)
                       VALUES (1, %s, %s, %s)
                       ON CONFLICT (user_id) DO UPDATE SET
                         home_label = EXCLUDED.home_label,
                         home_latitude = EXCLUDED.home_latitude,
                         home_longitude = EXCLUDED.home_longitude,
                         updated_at = NOW()""",
                    (label, lat, lng),
                )
    return jsonify({
        "ok": True,
        "home_label": label,
        "home_latitude": lat,
        "home_longitude": lng,
    })


def _resolve_home_location() -> tuple[float, float]:
    """Get the configured home lat/lng, falling back to Vancouver default
    if user_settings is empty."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT home_latitude, home_longitude FROM user_settings WHERE user_id = 1",
                )
                row = cur.fetchone()
                if row and row[0] is not None and row[1] is not None:
                    return float(row[0]), float(row[1])
    except Exception:  # noqa: BLE001
        pass
    return 49.2827, -123.1207


def _resolve_home_label() -> str | None:
    """Get the configured home label string for friendly emails. None
    if no home location is set."""
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT home_label FROM user_settings WHERE user_id = 1",
                )
                row = cur.fetchone()
                if row and row[0]:
                    # Display names from Nominatim are long. Trim to
                    # the first comma-separated chunk (e.g.
                    # "Vancouver, Metro Vancouver, BC, Canada" -> "Vancouver, BC, Canada"
                    # actually -> just take the first two chunks for brevity).
                    parts = [p.strip() for p in row[0].split(",")]
                    if len(parts) >= 3:
                        return f"{parts[0]}, {parts[-2]}, {parts[-1]}"
                    return row[0]
    except Exception:  # noqa: BLE001
        pass
    return None


# In-memory cache for geocoding queries. Nominatim is rate-limited
# (1 req/sec); caching repeated keystrokes saves their server and ours.
_GEOCODE_CACHE: dict[str, list[dict]] = {}
_GEOCODE_CACHE_MAX = 500


@app.route("/api/geocode", methods=["GET"])
def api_geocode():
    """Forward an address-autocomplete query to Nominatim (OpenStreetMap)
    and return a small JSON list of suggestions.

    Query: ?q=<free-text address or place name>
    Response: { results: [ { label, lat, lng }, ... ] }

    Why a server-side proxy rather than calling Nominatim from the
    browser? Nominatim's usage policy requires a real User-Agent
    identifying the app — browsers can't set that header on cross-origin
    requests. Routing through Flask also lets us cache and lightly
    rate-limit requests so we don't get banned.
    """
    import requests  # local import keeps cold start lean

    q = (request.args.get("q") or "").strip()
    if len(q) < 3:
        return jsonify({"results": []})

    if q in _GEOCODE_CACHE:
        return jsonify({"results": _GEOCODE_CACHE[q]})

    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": q,
                "format": "json",
                "limit": 6,
                "addressdetails": 0,
            },
            headers={
                # Nominatim policy: identify the app + a contact route
                "User-Agent": "bullseye-deal-finder/0.1 (github.com/reubenlavin08/bullseye)",
                "Accept-Language": "en",
            },
            timeout=4.0,
        )
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as e:
        return jsonify({"results": [], "error": f"geocoder unreachable: {e}"}), 502

    results = []
    for item in raw[:6]:
        try:
            results.append({
                "label": item.get("display_name") or "",
                "lat": float(item["lat"]),
                "lng": float(item["lon"]),
            })
        except (KeyError, TypeError, ValueError):
            continue

    # Cache + bound the cache size (LRU-ish — drop oldest by insertion order)
    _GEOCODE_CACHE[q] = results
    if len(_GEOCODE_CACHE) > _GEOCODE_CACHE_MAX:
        # Pop the oldest 50 to amortize
        for k in list(_GEOCODE_CACHE.keys())[:50]:
            _GEOCODE_CACHE.pop(k, None)

    return jsonify({"results": results})


@app.route("/api/searches/bulk", methods=["POST"])
def api_searches_bulk():
    """Create many saved searches at once.

    Use case: paste a list of keywords (e.g. produced by Claude when
    asked 'list all electronics components I'd want'). All searches
    get the same shared params (location, radius, price filters).

    Body: form-encoded or JSON
        keywords      newline-or-comma-separated string of keywords
        lat, lng      float (default Vancouver)
        radius_km     int  (default 40)
        price_min     int  (optional)
        price_max     int  (optional)

    Returns the IDs of newly-created searches + which ones were
    duplicates of existing ones (we de-dup on (keyword, lat, lng,
    radius_km) for the user_id).
    """
    data = request.form if request.form else (request.get_json(silent=True) or {})
    raw = (data.get("keywords") or "").strip()
    if not raw:
        return jsonify({"ok": False, "error": "keywords required"}), 400

    # Split on newlines OR commas; trim and dedupe.
    import re
    parts = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
    seen = set()
    keywords: list[str] = []
    for p in parts:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        keywords.append(p)
    if not keywords:
        return jsonify({"ok": False, "error": "no usable keywords"}), 400

    home_lat, home_lng = _resolve_home_location()
    try:
        lat = float(data.get("lat") or home_lat)
        lng = float(data.get("lng") or home_lng)
        radius_km = int(data.get("radius_km") or 40)
        pmin_str = (str(data.get("price_min") or "")).strip()
        pmax_str = (str(data.get("price_max") or "")).strip()
        price_min = int(pmin_str) if pmin_str else None
        price_max = int(pmax_str) if pmax_str else None
        # Optional inline subscribe
        sub_email = (str(data.get("email") or "")).strip()
        sub_name = (str(data.get("name") or "")).strip() or None
        sub_threshold = int(data.get("score_threshold") or 70) if str(data.get("score_threshold") or "").strip() else 70
        sub_threshold = max(0, min(100, sub_threshold))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad numeric input"}), 400

    if sub_email and "@" not in sub_email:
        return jsonify({"ok": False, "error": "invalid email"}), 400

    created: list[dict] = []
    duplicate: list[dict] = []
    subscribed_to: list[int] = []
    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                for kw in keywords:
                    # Dedup: same keyword (case-insensitive) within the
                    # same radius. We use BETWEEN on lat/lng to absorb
                    # REAL-precision rounding (Postgres stores REAL as
                    # 32-bit float; round-tripping a literal Python float
                    # can shift the last bit so == fails).
                    cur.execute(
                        """SELECT id FROM user_searches
                           WHERE LOWER(keyword) = LOWER(%s)
                             AND radius_km = %s
                             AND ABS(latitude - %s::real) < 0.001
                             AND ABS(longitude - %s::real) < 0.001""",
                        (kw, radius_km, lat, lng),
                    )
                    existing = cur.fetchone()
                    if existing:
                        duplicate.append({"keyword": kw, "id": existing[0]})
                        # Re-activate if it was disabled
                        cur.execute(
                            "UPDATE user_searches SET active = TRUE WHERE id = %s",
                            (existing[0],),
                        )
                        continue
                    cur.execute(
                        """INSERT INTO user_searches
                           (keyword, latitude, longitude, radius_km,
                            price_min, price_max, active)
                           VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                           RETURNING id""",
                        (kw, lat, lng, radius_km, price_min, price_max),
                    )
                    new_id = cur.fetchone()[0]
                    created.append({"keyword": kw, "id": new_id})

                # Inline subscribe — applies to ALL touched searches.
                if sub_email:
                    all_ids = [c["id"] for c in created] + [d["id"] for d in duplicate]
                    for sid in all_ids:
                        cur.execute(
                            """INSERT INTO subscribers
                               (name, email, search_id, score_threshold, active)
                               VALUES (%s, %s, %s, %s, TRUE)
                               ON CONFLICT (email, search_id) DO UPDATE SET
                                 name = COALESCE(EXCLUDED.name, subscribers.name),
                                 score_threshold = EXCLUDED.score_threshold,
                                 active = TRUE""",
                            (sub_name, sub_email, sid, sub_threshold),
                        )
                        subscribed_to.append(sid)

    summary_bits = [f"{len(created)} new search(es) created"]
    if duplicate:
        summary_bits.append(f"{len(duplicate)} re-activated")
    if sub_email and subscribed_to:
        summary_bits.append(
            f"alerts to {sub_email} on {len(subscribed_to)} watch(es) "
            f"@ score ≥ {sub_threshold}"
        )

    # Fire a confirmation email so the user sees an immediate "we're
    # watching" receipt instead of a silent panel close. Best-effort —
    # the save has already committed; an email failure shouldn't 500.
    confirmation_status = None
    if sub_email and (created or duplicate):
        try:
            from deal_finder.alerts.digest import send_confirmation_email
            home_label = _resolve_home_label()
            confirmation_status = send_confirmation_email(
                email=sub_email,
                name=sub_name,
                keywords=keywords,
                radius_km=radius_km,
                home_label=home_label,
                score_threshold=sub_threshold,
                price_min=price_min,
                price_max=price_max,
            )
        except Exception as e:  # noqa: BLE001
            confirmation_status = {"ok": False, "message": str(e)}

    return jsonify({
        "ok": True,
        "created": created,
        "duplicate": duplicate,
        "subscribed_to": subscribed_to,
        "summary": " · ".join(summary_bits),
        "confirmation": confirmation_status,
    })


@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    """Record a notification preference. Either form-encoded or JSON.

    Required: email, search_id
    Optional: name, phone, score_threshold (defaults 70)
    """
    data = request.form if request.form else (request.get_json(silent=True) or {})
    email = (data.get("email") or "").strip()
    search_id = data.get("search_id")
    name = (data.get("name") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None
    threshold = data.get("score_threshold") or 70

    if not email or "@" not in email:
        return jsonify({"ok": False, "error": "valid email required"}), 400
    try:
        search_id = int(search_id)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "search_id required"}), 400
    try:
        threshold = max(0, min(100, int(threshold)))
    except (TypeError, ValueError):
        threshold = 70

    with get_conn() as conn:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO subscribers
                       (name, email, phone, search_id, score_threshold)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (email, search_id) DO UPDATE SET
                         name = EXCLUDED.name,
                         phone = EXCLUDED.phone,
                         score_threshold = EXCLUDED.score_threshold,
                         active = TRUE
                       RETURNING id""",
                    (name, email, phone, search_id, threshold),
                )
                sub_id = cur.fetchone()[0]
    return jsonify({
        "ok": True,
        "subscriber_id": sub_id,
        "message": (
            f"Subscribed {email} for deals scoring {threshold}+ "
            f"on search {search_id}."
        ),
    })


@app.route("/api/comps")
def api_comps():
    """Return the recent comp rows that built a given median.

    Query params:
      term   -- the comp_search_term (URL-encoded)
      source -- 'marketplace' (default) or 'ebay' (later)

    Used by the trust UI: user clicks the median number on a card and
    sees exactly which listings the LLM was anchored on.
    """
    term = (request.args.get("term") or "").strip()
    source = (request.args.get("source") or "marketplace").strip()
    ttl_seconds = int(request.args.get("ttl") or 12 * 3600)
    if not term:
        return jsonify({"error": "missing 'term'"}), 400

    rows = []
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT price, title, listing_url, location, fetched_at
                   FROM comps
                   WHERE search_term = %s AND source = %s
                     AND fetched_at >= NOW() - INTERVAL '%s seconds'
                   ORDER BY price ASC""",
                (term, source, ttl_seconds),
            )
            for r in cur.fetchall():
                rows.append({
                    "price": float(r[0]),
                    "title": r[1],
                    "listing_url": r[2],
                    "location": r[3],
                    "fetched_at": r[4].isoformat() if r[4] else None,
                })

    if not rows:
        return jsonify({"term": term, "source": source, "rows": []})

    prices = [r["price"] for r in rows]
    import statistics
    return jsonify({
        "term": term,
        "source": source,
        "sample_size": len(rows),
        "median": statistics.median(prices),
        "mean": statistics.fmean(prices),
        "min": min(prices),
        "max": max(prices),
        "rows": rows,
    })


@app.route("/appraise/<listing_id>", methods=["POST"])
def appraise(listing_id: str):
    """Run the full appraisal flow on a single listing right now.

    Fetches detail (so we have a description), upserts into the listings
    table, runs the deterministic stats-based scorer, and returns the
    full breakdown as JSON.

    Fast: typically ~3-8s when comp sample is sufficient (no LLM
    needed). Slower (~20s) when comps are sparse and the LLM has to
    estimate fair_value.
    """
    from dataclasses import asdict

    from deal_finder.appraisal.condition_signals import extract_condition_signals
    from deal_finder.appraisal.formula import compute_score, llm_needed
    from deal_finder.appraisal.normalizer import normalize_title
    from deal_finder.appraisal.scorer import estimate_fair_value
    from deal_finder.comps.marketplace import get_comps
    from deal_finder.db.listings import (
        update_appraisal,
        update_comps_resolution,
        upsert_processed,
    )
    from deal_finder.scraper.facebook import SearchListing
    from deal_finder.scraper.pipeline import _combine

    title_arg = request.args.get("title", "")
    raw_price_arg = float(request.args.get("raw_price") or 0.0)

    # 1) Fetch detail (description, seller, photos)
    try:
        detail = get_detail_client().fetch(listing_id)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"detail fetch: {e}"}), 502
    if not detail.description:
        return jsonify({
            "ok": False,
            "error": "could not fetch listing description; cannot appraise",
            "errors": detail.errors,
        }), 502

    # 2) Synthesize a SearchListing so we can reuse pipeline._combine
    sl = SearchListing(
        id=listing_id,
        title=detail.title or title_arg,
        price_amount=raw_price_arg or None,
        price_formatted=detail.price_formatted,
        previous_price=None,
        is_pending=bool(detail.is_pending),
        photo_url=detail.photo_urls[0] if detail.photo_urls else None,
        seller_location=detail.location,
        listing_url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
    )
    pl = _combine(sl, detail)

    # 3) Persist the listing row so the appraisal has somewhere to land
    try:
        with get_conn() as conn:
            with conn:
                upsert_processed(conn, pl)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"db upsert: {e}"}), 500

    # 4) Run comps
    try:
        search_term = normalize_title(pl.title) or pl.title
        # Use the normalized title for both keyword search AND embedding
        # similarity — descriptions add noise that drags cosine scores down.
        comp = get_comps(
            search_term=search_term,
            lat=49.2827, lng=-123.1207, radius_km=1500,
            exclude_listing_id=listing_id,
            asking_price=pl.resolved_price,
            target_text=search_term,
            category_id=getattr(pl, "category_id", None),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"comp fetch: {e}"}), 502

    # 5) LLM fair_value estimate ONLY if comps are sparse
    llm_estimate = None
    note = ""
    model_used = "formula-only"
    if llm_needed(comp):
        llm_estimate = estimate_fair_value(
            title=pl.title,
            asking_price=pl.resolved_price,
            description=pl.description,
            location=pl.seller_location,
            comp=comp,
            raw_price=pl.raw_price,
            price_extracted=pl.price_extracted_from_description,
        )
        if llm_estimate is not None:
            note = llm_estimate.note
            model_used = llm_estimate.model

    # 6) Extract condition signals + compute deterministic score
    cond = extract_condition_signals(pl.description)
    try:
        breakdown = compute_score(
            asking_price=pl.resolved_price,
            comp=comp,
            fair_value_from_llm=(
                llm_estimate.fair_value if llm_estimate else None
            ),
            condition_adjustment=cond.score_adjustment,
            condition_flags=cond.flags_fired,
            condition_note=cond.note,
            category_id=getattr(pl, "category_id", None),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": f"formula: {e}"}), 422

    # 7) Persist
    annotated_note = note
    if breakdown.confidence_label:
        annotated_note = (
            f"[{breakdown.confidence_label} ±{breakdown.confidence_pm}] "
            f"{note}".strip()
        )

    try:
        with get_conn() as conn:
            with conn:
                update_comps_resolution(
                    conn, listing_id,
                    search_term=comp.search_term, source=comp.source,
                    median=comp.median, mean=comp.mean,
                    minimum=comp.minimum, maximum=comp.maximum,
                    sample_size=comp.sample_size,
                )
                update_appraisal(
                    conn, listing_id,
                    deal_score=breakdown.deal_score,
                    fair_value=breakdown.fair_value,
                    appraisal_note=annotated_note,
                    appraisal_model=model_used,
                    breakdown=breakdown,
                )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"db update: {e}"}), 500

    return jsonify({
        "ok": True,
        "deal_score": breakdown.deal_score,
        "fair_value": breakdown.fair_value,
        "confidence": breakdown.confidence_label,
        "confidence_pm": breakdown.confidence_pm,
        "note": note or annotated_note,
        "search_term": comp.search_term,
        "comp_median": comp.median,
        "comp_sample_size": comp.sample_size,
        "ratio": breakdown.ratio,
        "fair_value_source": breakdown.fair_value_source,
        "outliers_dropped": breakdown.outliers_dropped,
        "elapsed_s": llm_estimate.elapsed_s if llm_estimate else 0.0,
        "breakdown": asdict(breakdown),
    })


# --- Form helpers ---------------------------------------------------------

def _form_to_defaults(form) -> dict:
    return {
        "keyword": form.get("keyword", ""),
        "lat": form.get("lat", DEFAULT_LAT),
        "lng": form.get("lng", DEFAULT_LNG),
        "radius_km": form.get("radius_km", DEFAULT_RADIUS_KM),
        "price_min": form.get("price_min", ""),
        "price_max": form.get("price_max", ""),
    }


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
