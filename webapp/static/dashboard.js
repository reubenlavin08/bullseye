// dashboard.js — polls /api/dashboard/* and repaints sections.
//
// Polling cadences (chosen to balance freshness vs. DB churn):
//   summary      — every 5s  (status strip + funnel + alive pill)
//   events       — every 2s  (live tail; uses ?since=<id> for incremental)
//   log tail     — every 2s when 'Raw log' tab is active
//   per-watch    — every 30s (24h aggregates change slowly)
//   histogram    — every 30s (24h distribution changes slowly)

(function () {
    "use strict";

    const ev = (tag, cls, content) => {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (content !== undefined) e.textContent = content;
        return e;
    };
    const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString());
    const escapeHtml = (s) => {
        const d = document.createElement("div");
        d.textContent = s;
        return d.innerHTML;
    };

    function timeAgo(iso) {
        if (!iso) return "never";
        const t = new Date(iso).getTime();
        const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
        if (s < 60) return s + "s ago";
        const m = Math.floor(s / 60);
        if (m < 60) return m + "m ago";
        const h = Math.floor(m / 60);
        if (h < 24) return h + "h ago";
        return Math.floor(h / 24) + "d ago";
    }

    // Compact "Nh Mm" / "Nd Hh" uptime formatter for the status strip.
    function formatUptime(iso) {
        if (!iso) return "—";
        const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
        const m = Math.floor(s / 60);
        const h = Math.floor(m / 60);
        const d = Math.floor(h / 24);
        if (d > 0) return `${d}d ${h % 24}h`;
        if (h > 0) return `${h}h ${m % 60}m`;
        if (m > 0) return `${m}m`;
        return `${s}s`;
    }

    // --- 1 + 2: summary (status strip + funnel) -----------------------

    // --- Poll timer (the prominent "next attempt in X" widget) -----------
    //
    // Data lives on /api/dashboard/summary as data.poll_timer. The summary
    // endpoint is hit every 5s. Between resyncs we tick the countdown
    // down once a second locally so it feels alive.
    let pollTimerData = null;
    let pollTimerLastSync = 0;

    function formatCountdown(s) {
        if (s <= 0) return "now";
        const m = Math.floor(s / 60);
        const r = s % 60;
        if (m > 0) return `${m}m ${String(r).padStart(2, "0")}s`;
        return `${r}s`;
    }

    function renderPollTimer() {
        if (!pollTimerData) return;
        const t = pollTimerData;

        // Local-tick: subtract elapsed seconds since the last server resync.
        const elapsed = Math.floor((Date.now() - pollTimerLastSync) / 1000);
        const remaining = Math.max(0, t.next_attempt_in_s - elapsed);

        const block = document.getElementById("poll-timer-block");
        const wrap  = block.querySelector(".poll-timer");
        const icon  = document.getElementById("poll-timer-icon");
        const state = document.getElementById("poll-timer-state");
        const detail= document.getElementById("poll-timer-detail");
        const countdown = document.getElementById("poll-timer-countdown");
        const bar   = document.getElementById("poll-timer-bar");

        // State styling. The block's data-state drives the CSS color band.
        wrap.dataset.state = t.state;

        let label, glyph, detailText;
        switch (t.state) {
            case "cooldown": {
                glyph = "⏸";
                const cd = t.cooldown || {};
                label = "RATE-LIMIT COOLDOWN";
                detailText =
                    `${cd.rate_limits_in_window} rate-limits in last ${cd.window_minutes}m → ` +
                    `cooldown ${cd.total_s}s · ${cd.remaining_s}s left when last synced`;
                break;
            }
            case "slow_start": {
                glyph = "◷";
                const ss = t.slow_start || {};
                label = "SLOW-START GATE";
                detailText =
                    `effective interval ${ss.min_interval_s}s · ` +
                    `${ss.elapsed_since_last_attempt_s}s since last attempt · ` +
                    `floor ${ss.floor_s}s, initial ${ss.initial_s}s`;
                break;
            }
            case "tick": {
                glyph = "▸";
                label = "READY · WAITING FOR NEXT TICK";
                detailText =
                    `coordinator tick every ${t.coordinator_tick_s}s · ` +
                    (t.last_attempt_iso
                        ? `last attempt ${timeAgo(t.last_attempt_iso)}`
                        : "no attempts yet this run");
                break;
            }
            default: {
                glyph = "·";
                label = "IDLE";
                detailText = "no recent poll activity";
            }
        }

        icon.textContent = glyph;
        state.textContent = label;
        detail.textContent = detailText;
        countdown.textContent = formatCountdown(remaining);

        // Progress bar — fills from 0% to 100% as we approach next attempt.
        // For cooldown we know the total; for slow_start we use min_interval;
        // for tick state we use coordinator_tick_s. Visual feedback only.
        let total;
        if (t.state === "cooldown")        total = (t.cooldown || {}).total_s || 60;
        else if (t.state === "slow_start") total = (t.slow_start || {}).min_interval_s || 60;
        else                               total = t.coordinator_tick_s || 20;
        const pct = Math.min(100, Math.max(0, ((total - remaining) / total) * 100));
        bar.style.width = pct.toFixed(1) + "%";
    }

    async function refreshSummary() {
        try {
            const data = await (await fetch("/api/dashboard/summary")).json();

            const pill = document.getElementById("alive-pill");
            pill.dataset.state = data.alive ? "alive" : "dead";
            pill.querySelector(".status-text").textContent =
                data.alive ? "scheduler · live" : "scheduler · offline";

            // Poll timer — capture data + resync timestamp; renderPollTimer
            // pulls from these locals + decays remaining via wall-clock.
            if (data.poll_timer) {
                pollTimerData = data.poll_timer;
                pollTimerLastSync = Date.now();
                renderPollTimer();
            }

            const r = data.rates || {};
            document.getElementById("stat-watches").textContent =
                `${data.active_watches}/${data.total_watches}`;

            // Polls — show "since boot" as the primary number, with last-1h
            // and total in the tooltip so nothing feels like it disappears.
            const pollsEl = document.getElementById("stat-polls");
            pollsEl.textContent = fmtNum(r.polls_since_boot);
            pollsEl.parentElement.title =
                `polls this run: ${fmtNum(r.polls_since_boot)}\n` +
                `last 1h: ${fmtNum(r.polls_last_1h)}`;

            // Rate-limits — primary is "this run" so leaving the page can't
            // make it look like rate-limits got dropped from history.
            const rateEl = document.getElementById("stat-rate");
            rateEl.textContent = fmtNum(r.rate_limits_since_boot);
            rateEl.parentElement.title =
                `rate-limits this run: ${fmtNum(r.rate_limits_since_boot)}\n` +
                `last 1h: ${fmtNum(r.rate_limits_last_1h)}\n` +
                `last 24h: ${fmtNum(r.rate_limits_last_24h)}\n` +
                `total in DB: ${fmtNum(r.rate_limits_total)}`;

            const emailsEl = document.getElementById("stat-emails");
            emailsEl.textContent = fmtNum(r.emails_today);
            emailsEl.parentElement.title =
                `emails today: ${fmtNum(r.emails_today)}\n` +
                `total ever sent: ${fmtNum(r.emails_total)}`;

            document.getElementById("stat-errors").textContent = fmtNum(r.pipeline_errors_24h);
            document.getElementById("stat-uptime").textContent =
                data.scheduler_booted_at ? formatUptime(data.scheduler_booted_at) : "—";
            document.getElementById("stat-last").textContent = timeAgo(data.last_event_iso);

            // Funnel
            const f = data.funnel_today || {};
            const map = {
                scraped: f.scraped, rejected: f.rejected, appraised: f.appraised,
                over_threshold: f.over_threshold, notified: f.notified,
            };
            document.querySelectorAll(".funnel-step").forEach((step) => {
                const k = step.dataset.step;
                step.querySelector(".funnel-num").textContent = fmtNum(map[k]);
            });
            const pending = document.getElementById("funnel-pending");
            if (f.pending_unsent > 0) {
                pending.textContent = `+ ${f.pending_unsent} pending unsent (above threshold, not yet emailed)`;
                pending.style.display = "";
            } else {
                pending.style.display = "none";
            }
        } catch (err) {
            // Soft fail — keep last values, banner the error briefly
            console.warn("summary refresh failed:", err);
        }
    }

    // --- 3: live tail (events OR raw log) ------------------------------

    let activeTailSource = "events";
    let lastEventId = 0;

    function eventLine(e) {
        const row = ev("div", "feed-row feed-" + e.event_type);
        const ts = ev("span", "feed-ts", new Date(e.created_at).toLocaleTimeString());
        const type = ev("span", "feed-type", e.event_type);
        const body = ev("span", "feed-body");
        const detail = e.detail || {};
        const kw = e.keyword ? ` (${e.keyword})` : "";

        let summary = "";
        switch (e.event_type) {
            case "poll":
                summary = `${detail.new_count ?? 0} new of ${detail.raw_count ?? 0}${kw}`;
                if (e.duration_ms) summary += ` · ${(e.duration_ms / 1000).toFixed(1)}s`;
                if (detail.appraised_count) summary += ` · ${detail.appraised_count} appraised`;
                if (detail.rejected_count) summary += ` · ${detail.rejected_count} rejected`;
                break;
            case "fb_rate_limit":
                summary = `code ${detail.code ?? "?"} · ${detail.message ?? ""}`;
                break;
            case "fb_graphql_error":
                summary = `code ${detail.code ?? "?"} · ${detail.message ?? ""}`;
                break;
            case "email_sent":
                summary = `→ ${detail.recipient ?? "?"} · ${detail.backend ?? "?"} · ${detail.subject ?? ""}`;
                break;
            case "email_failed":
                summary = `× ${detail.recipient ?? "?"} · ${detail.backend ?? "?"} · ${detail.error ?? ""}`;
                break;
            case "safety_drain":
                summary = `seen ${detail.seen} · appraised ${detail.appraised} · skipped ${detail.skipped_no_score}`;
                break;
            case "reload":
                summary = `+${(detail.added || []).length} -${(detail.removed || []).length} · total active ${detail.total_active}`;
                break;
            case "scheduler_boot":
                summary = `pid ${detail.pid} · ${detail.n_active_searches} watches · backend ${detail.alert_backend}`;
                break;
            case "pipeline_error":
                summary = `${detail.error_type ?? "Error"}: ${detail.error ?? ""} (listing ${detail.listing_id})`;
                break;
            default:
                summary = JSON.stringify(detail);
        }
        body.textContent = summary;

        row.appendChild(ts);
        row.appendChild(type);
        row.appendChild(body);
        return row;
    }

    async function refreshEvents() {
        if (activeTailSource !== "events") return;
        try {
            // Initial load grabs 250 (covers a multi-hour gap); incremental
            // ticks afterward only fetch new ones via since=lastEventId so
            // this is cheap.
            const limit = lastEventId === 0 ? 250 : 80;
            const url = "/api/dashboard/events?since=" + lastEventId + "&limit=" + limit;
            const data = await (await fetch(url)).json();
            const events = data.events || [];
            if (events.length === 0) return;

            const feed = document.getElementById("event-feed");
            // First load: replace contents.
            if (lastEventId === 0) feed.innerHTML = "";
            // The API returns newest-first ([N, N-1, ..., N-k]). Each
            // insertBefore prepends, which inverts iteration order — so we
            // must iterate OLDEST-FIRST for the final order to be
            // newest-at-top. The previous bug was a straight forEach which
            // produced reverse-chronological with the OLDEST event ending
            // up at the top of the feed.
            events.slice().reverse().forEach((e) => {
                feed.insertBefore(eventLine(e), feed.firstChild);
                if (e.id > lastEventId) lastEventId = e.id;
            });
            // Cap displayed rows so the DOM doesn't grow unbounded.
            // 500 covers ~2-3h of busy traffic without scrolling falling off.
            while (feed.children.length > 500) {
                feed.removeChild(feed.lastChild);
            }
        } catch (err) {
            console.warn("events refresh failed:", err);
        }
    }

    async function refreshRawLog() {
        if (activeTailSource !== "log") return;
        try {
            const data = await (await fetch("/api/dashboard/log/tail?n=500")).json();
            const feed = document.getElementById("event-feed");
            if (!data.exists) {
                feed.innerHTML =
                    '<div class="muted">' +
                    'no scheduler.log file yet. start the scheduler with: ' +
                    '<code>python -m deal_finder.scheduler.main</code>' +
                    '</div>';
                return;
            }
            const lines = data.lines || [];
            if (lines.length === 0) {
                feed.innerHTML = '<div class="muted">log is empty</div>';
                return;
            }
            // Render newest at top to match the structured-event view.
            feed.innerHTML = lines.slice().reverse().map((line) => {
                const cls = /\bERROR\b|Traceback|Rate limit|failed/i.test(line)
                    ? "log-line log-error" :
                    /\bWARNING\b|warning/i.test(line)
                    ? "log-line log-warn"
                    : "log-line";
                return `<div class="${cls}">${escapeHtml(line)}</div>`;
            }).join("");
        } catch (err) {
            console.warn("log refresh failed:", err);
        }
    }

    function setupTailTabs() {
        document.querySelectorAll(".dash-tab").forEach((btn) => {
            btn.addEventListener("click", () => {
                document.querySelectorAll(".dash-tab").forEach((b) =>
                    b.classList.toggle("is-active", b === btn)
                );
                activeTailSource = btn.dataset.source;
                lastEventId = 0;       // force full reload
                document.getElementById("event-feed").innerHTML =
                    '<div class="muted">loading…</div>';
                if (activeTailSource === "events") refreshEvents();
                else refreshRawLog();
            });
        });
    }

    // --- 4: per-watch table -------------------------------------------

    let perWatchData = [];
    let sortKey = "polls_24h";
    let sortDesc = true;

    async function refreshPerWatch() {
        try {
            const data = await (await fetch("/api/dashboard/per-watch")).json();
            perWatchData = data.watches || [];
            renderPerWatch();
        } catch (err) {
            console.warn("per-watch refresh failed:", err);
        }
    }

    function renderPerWatch() {
        const body = document.getElementById("per-watch-body");
        if (perWatchData.length === 0) {
            body.innerHTML = '<tr><td colspan="6" class="muted">no watches</td></tr>';
            return;
        }
        const sorted = perWatchData.slice().sort((a, b) => {
            const av = a[sortKey], bv = b[sortKey];
            if (av == null && bv == null) return 0;
            if (av == null) return 1;
            if (bv == null) return -1;
            if (typeof av === "string") return sortDesc ? bv.localeCompare(av) : av.localeCompare(bv);
            return sortDesc ? bv - av : av - bv;
        });
        body.innerHTML = sorted.map((w) => {
            const stale = w.active && w.polls_24h === 0 ? "stale" : "";
            return `<tr class="${w.active ? "" : "row-paused"} ${stale}">
                <td class="watch-kw">${w.active ? "" : "<span class='paused-tag'>paused</span> "}${escapeHtml(w.keyword)}</td>
                <td>${w.polls_24h}</td>
                <td>${w.avg_raw != null ? w.avg_raw.toFixed(1) : "—"}</td>
                <td class="${w.hits_24h > 0 ? "good" : ""}">${w.hits_24h}</td>
                <td class="${w.rate_limits_24h > 0 ? "warn" : ""}">${w.rate_limits_24h}</td>
                <td class="muted">${timeAgo(w.last_scrape_iso)}</td>
            </tr>`;
        }).join("");
    }

    function setupPerWatchSort() {
        document.querySelectorAll("#per-watch-table th[data-sort]").forEach((th) => {
            th.addEventListener("click", () => {
                const k = th.dataset.sort;
                if (k === sortKey) sortDesc = !sortDesc;
                else { sortKey = k; sortDesc = true; }
                renderPerWatch();
            });
        });
    }

    // --- 4b: appraisal feed -------------------------------------------

    let appraisalFilter = "all";

    function setupAppraisalFilters() {
        const wrap = document.getElementById("appraisal-filters");
        if (!wrap) return;
        wrap.querySelectorAll(".dash-tab").forEach((btn) => {
            btn.addEventListener("click", () => {
                wrap.querySelectorAll(".dash-tab").forEach((b) =>
                    b.classList.remove("is-active"));
                btn.classList.add("is-active");
                appraisalFilter = btn.dataset.filter || "all";
                refreshAppraisalFeed();
            });
        });
    }

    function statusBadge(status) {
        const labels = {
            emailed: "EMAILED",
            passed:  "≥ THRESH",
            scored:  "SCORED",
            unscoreable: "NO SCORE",
            rejected: "REJECTED",
            pending:  "PENDING",
        };
        return `<span class="apr-status apr-status-${status}">${labels[status] || status}</span>`;
    }

    function scoreBadge(score, status) {
        if (score == null) return `<span class="apr-score apr-score-none">—</span>`;
        let cls = "apr-score-low";
        if (score >= 70) cls = "apr-score-high";
        else if (score >= 50) cls = "apr-score-mid";
        return `<span class="apr-score ${cls}">${score}</span>`;
    }

    function fmtPrice(p) {
        if (p == null) return "—";
        return "$" + Math.round(p).toLocaleString();
    }

    async function refreshAppraisalFeed() {
        try {
            // 200 listings keeps a multi-hour history visible. The feed
            // itself is scrollable; the API caps at 200 anyway.
            const url = `/api/dashboard/appraisal-feed?filter=${appraisalFilter}&limit=200`;
            const data = await (await fetch(url)).json();
            const wrap = document.getElementById("appraisal-feed");
            const listings = data.listings || [];
            if (!listings.length) {
                wrap.innerHTML = `<div class="muted">no listings yet for filter '${appraisalFilter}'</div>`;
                return;
            }
            const threshold = data.threshold || 70;
            wrap.innerHTML = listings.map((l) => {
                const ts = l.appraised_at || l.scraped_at;
                const ago = timeAgo(ts);
                const tail = l.rejected
                    ? `<span class="apr-tail bad">rejected: ${escapeHtml(l.rejection_reason || "n/a")}</span>`
                    : (l.deal_score != null
                        ? `<span class="apr-tail">${escapeHtml(l.appraisal_note || "")} · n=${l.comp_sample_size ?? "?"} · fair $${l.fair_value != null ? Math.round(l.fair_value) : "?"}</span>`
                        : `<span class="apr-tail muted">${escapeHtml(l.appraisal_note || "no score")}</span>`);
                const href = l.listing_url || "#";
                return `
                    <a class="apr-row apr-row-${l.status}" href="${href}" target="_blank" rel="noopener">
                        ${scoreBadge(l.deal_score, l.status)}
                        <div class="apr-main">
                            <div class="apr-title-row">
                                <span class="apr-title">${escapeHtml(l.title || "")}</span>
                                ${statusBadge(l.status)}
                            </div>
                            <div class="apr-meta">
                                <span class="apr-kw">${escapeHtml(l.keyword || "—")}</span>
                                <span class="apr-price">${fmtPrice(l.price)}</span>
                                <span class="apr-ago">${ago}</span>
                                ${tail}
                            </div>
                        </div>
                    </a>
                `;
            }).join("");
        } catch (err) {
            console.warn("appraisal feed refresh failed:", err);
        }
    }

    // --- 5: histogram --------------------------------------------------

    async function refreshHistogram() {
        try {
            const data = await (await fetch("/api/dashboard/score-histogram")).json();
            const buckets = data.buckets || [];
            const max = Math.max(1, ...buckets.map((b) => b.count));
            const wrap = document.getElementById("score-histogram");
            wrap.innerHTML = buckets.map((b) => {
                const pct = (b.count / max) * 100;
                const cls = b.label === "100" || parseInt(b.label) >= 70 ? "hist-bar good" :
                           parseInt(b.label) >= 50 ? "hist-bar warn" : "hist-bar";
                return `
                    <div class="hist-row">
                        <div class="hist-label">${b.label}</div>
                        <div class="hist-track">
                            <div class="${cls}" style="width:${pct.toFixed(1)}%"></div>
                        </div>
                        <div class="hist-count">${b.count}</div>
                    </div>`;
            }).join("");
            if (buckets.every((b) => b.count === 0)) {
                wrap.innerHTML = '<div class="muted">no scored listings in the last 24h yet</div>';
            }
        } catch (err) {
            console.warn("histogram refresh failed:", err);
        }
    }

    // --- bootstrap -----------------------------------------------------

    function start() {
        setupTailTabs();
        setupPerWatchSort();
        setupAppraisalFilters();

        refreshSummary();
        refreshEvents();
        refreshAppraisalFeed();
        refreshPerWatch();
        refreshHistogram();

        // Re-render the poll timer 1x/sec so the countdown ticks down
        // visibly between server resyncs.
        setInterval(renderPollTimer,   1000);
        setInterval(refreshSummary,   5000);
        setInterval(() => {
            if (activeTailSource === "events") refreshEvents();
            else refreshRawLog();
        }, 2000);
        setInterval(refreshAppraisalFeed, 4000);
        setInterval(refreshPerWatch,  30000);
        setInterval(refreshHistogram, 30000);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", start);
    } else {
        start();
    }
})();
