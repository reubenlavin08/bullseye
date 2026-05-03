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
    //
    // The countdown is anchored to a wall-clock TARGET timestamp, not
    // re-derived from the server's `next_attempt_in_s` on every sync.
    // Without this anchor, integer rounding on the server side caused
    // the displayed seconds to "stutter" (e.g. ...12, 11, 11, 9, 8...) as
    // each 5s sync tweaked the residual by ±1s. Anchor only re-syncs
    // when state changes or the cooldown extends.
    let pollTimerData = null;
    let pollTimerAnchor = null;       // wall-clock ms when next attempt fires
    let pollTimerLastState = null;

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

        // Anchor-based countdown: round CEIL so we don't appear to skip
        // a second when wall-clock crosses a half-second boundary
        // mid-render. Use Math.ceil((target - now) / 1000) so display
        // value is monotone non-increasing between syncs.
        const remaining = pollTimerAnchor === null
            ? t.next_attempt_in_s
            : Math.max(0, Math.ceil((pollTimerAnchor - Date.now()) / 1000));

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

        // FB-health badge — distinguishes "we're blocked" from "FB is
        // down" from "all good" so the user knows what's wrong even
        // before reading the state line.
        const badge = document.getElementById("fb-health-badge");
        const h = t.fb_health || "unknown";
        badge.dataset.health = h;
        badge.textContent =
            h === "ok"            ? "FB OK" :
            h === "graphql_gated" ? "GRAPHQL GATED" :
            h === "blocked"       ? "FULLY FLAGGED" :
            h === "fb_down"       ? "FB DOWN" :
                                    "FB ?";
        badge.title =
            h === "ok"            ? "HTML probe is clean and no recent GraphQL rate-limits — fully unblocked." :
            h === "graphql_gated" ? "HTML probe says FB is up, but GraphQL search is still rate-limiting our IP. Different WAF policies — common during a per-IP quota block. Cooldown will retry." :
            h === "blocked"       ? "HTML probe was rejected — we're severely flagged. curl_cffi fingerprint isn't enough; need a different IP/proxy or wait for the block to age out." :
            h === "fb_down"       ? "Health probe couldn't reach FB; FB itself looks down." :
                                    "No probe has run yet this run.";

        // Progress bar — represents REMAINING time. Starts full and
        // shrinks toward 0 as we approach the next attempt. (Was
        // inverted before — bar grew as time passed, which felt
        // backwards for a countdown.)
        let total;
        if (t.state === "cooldown")        total = (t.cooldown || {}).total_s || 60;
        else if (t.state === "slow_start") total = (t.slow_start || {}).min_interval_s || 60;
        else                               total = t.coordinator_tick_s || 20;
        const pct = Math.min(100, Math.max(0, (remaining / total) * 100));
        bar.style.width = pct.toFixed(1) + "%";
    }

    async function refreshSummary() {
        try {
            const data = await (await fetch("/api/dashboard/summary")).json();

            const pill = document.getElementById("alive-pill");
            pill.dataset.state = data.alive ? "alive" : "dead";
            pill.querySelector(".status-text").textContent =
                data.alive ? "scheduler · live" : "scheduler · offline";

            // Poll timer — capture data + decide whether to re-anchor
            // the countdown target. We re-anchor on state change or
            // when the new target is materially LATER than current
            // (cooldown extended). Small downward drift is ignored so
            // the display ticks smoothly between syncs.
            if (data.poll_timer) {
                pollTimerData = data.poll_timer;
                const newAnchor = Date.now() + data.poll_timer.next_attempt_in_s * 1000;
                if (
                    pollTimerAnchor === null ||
                    data.poll_timer.state !== pollTimerLastState ||
                    newAnchor > pollTimerAnchor + 2000   // cooldown extended
                ) {
                    pollTimerAnchor = newAnchor;
                    pollTimerLastState = data.poll_timer.state;
                }
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
        // Scope to the live-tail tab group ONLY. The appraisal-feed
        // also uses .dash-tab buttons (with data-filter) and a global
        // querySelectorAll would trample each other's handlers. We
        // only react to buttons inside a .dash-tabs container that
        // has data-source children (i.e. the live-tail toggle),
        // never the appraisal-filters group.
        document.querySelectorAll(".dash-tab[data-source]").forEach((btn) => {
            btn.addEventListener("click", () => {
                // Toggle active state ONLY among siblings (other
                // data-source tabs), not all dash-tabs on the page.
                btn.parentElement.querySelectorAll(".dash-tab").forEach((b) =>
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

    // --- Comp drawer: shows the actual comp listings behind a score ---
    //
    // When the user clicks a comp chip on an appraisal row, we fetch
    // /api/comps and render the listings (price, title link, location)
    // in a slide-over panel so they can audit the score themselves.

    async function openCompDrawer(term, source) {
        let drawer = document.getElementById("comp-drawer");
        if (!drawer) {
            drawer = document.createElement("div");
            drawer.id = "comp-drawer";
            drawer.className = "comp-drawer";
            drawer.innerHTML = `
                <div class="comp-drawer-head">
                    <span class="comp-drawer-title">comp data</span>
                    <button class="comp-drawer-close" aria-label="close">×</button>
                </div>
                <div class="comp-drawer-meta muted">loading…</div>
                <div class="comp-drawer-body"></div>`;
            document.body.appendChild(drawer);
            drawer.querySelector(".comp-drawer-close").addEventListener("click", () => {
                drawer.classList.remove("is-open");
            });
        }
        drawer.classList.add("is-open");
        drawer.querySelector(".comp-drawer-meta").textContent = `loading "${term}" (${source})…`;
        drawer.querySelector(".comp-drawer-body").innerHTML = "";

        try {
            const url = `/api/comps?term=${encodeURIComponent(term)}&source=${encodeURIComponent(source)}`;
            const data = await (await fetch(url)).json();
            const rows = data.rows || [];
            if (rows.length === 0) {
                drawer.querySelector(".comp-drawer-meta").innerHTML =
                    `<em>No cached comps for "${escapeHtml(term)}" (${source}). The 12h cache may have expired; appraisals on this term will refetch on next poll.</em>`;
                return;
            }
            drawer.querySelector(".comp-drawer-meta").innerHTML =
                `<strong>${data.sample_size}</strong> comps from <strong>${source}</strong> for ` +
                `<em>${escapeHtml(term)}</em> · median <strong>$${Math.round(data.median)}</strong> · ` +
                `mean $${Math.round(data.mean)} · range $${Math.round(data.min)}–$${Math.round(data.max)}`;
            const max = data.max || 1;
            const median = data.median || 0;
            drawer.querySelector(".comp-drawer-body").innerHTML = rows.map((r) => {
                const pct = (r.price / max) * 100;
                const nearMed = median && Math.abs(r.price - median) / median < 0.15;
                const cls = nearMed ? "comp-drawer-row near-median" : "comp-drawer-row";
                const titleHTML = r.listing_url
                    ? `<a href="${r.listing_url}" target="_blank" rel="noopener">${escapeHtml(r.title || "(no title)")}</a>`
                    : escapeHtml(r.title || "(no title)");
                return `
                    <div class="${cls}">
                        <div class="comp-drawer-bar" style="width:${pct.toFixed(1)}%"></div>
                        <span class="comp-drawer-price">$${Math.round(r.price)}</span>
                        <span class="comp-drawer-title-cell">${titleHTML}</span>
                        <span class="comp-drawer-loc">${escapeHtml(r.location || "")}</span>
                    </div>`;
            }).join("");
        } catch (err) {
            drawer.querySelector(".comp-drawer-meta").textContent = "Failed to load comps: " + err.message;
        }
    }

    function setupCompDrawerDelegation() {
        // Event delegation: comp chips appear/disappear with each
        // refreshAppraisalFeed re-render, so we attach to the parent.
        const wrap = document.getElementById("appraisal-feed");
        if (!wrap) return;
        wrap.addEventListener("click", (e) => {
            const compChip = e.target.closest(".apr-comp-chip[data-comp-term]");
            if (compChip) {
                e.preventDefault();
                openCompDrawer(compChip.dataset.compTerm, compChip.dataset.compSource);
                return;
            }
            const bdChip = e.target.closest(".apr-bd-chip[data-listing-id]");
            if (bdChip) {
                e.preventDefault();
                openBreakdownDrawer(bdChip.dataset.listingId);
                return;
            }
        });
    }

    // --- Breakdown drawer: full score-computation transparency -------

    async function openBreakdownDrawer(listingId) {
        let drawer = document.getElementById("breakdown-drawer");
        if (!drawer) {
            drawer = document.createElement("div");
            drawer.id = "breakdown-drawer";
            drawer.className = "comp-drawer";
            drawer.innerHTML = `
                <div class="comp-drawer-head">
                    <span class="comp-drawer-title">score breakdown</span>
                    <button class="comp-drawer-close" aria-label="close">×</button>
                </div>
                <div class="comp-drawer-body" id="breakdown-body">loading…</div>`;
            document.body.appendChild(drawer);
            drawer.querySelector(".comp-drawer-close").addEventListener("click", () => {
                drawer.classList.remove("is-open");
            });
        }
        drawer.classList.add("is-open");
        drawer.querySelector("#breakdown-body").innerHTML = '<div class="muted">loading…</div>';

        try {
            const data = await (await fetch(`/api/dashboard/breakdown/${encodeURIComponent(listingId)}`)).json();
            if (data.error) {
                drawer.querySelector("#breakdown-body").textContent = data.error;
                return;
            }
            drawer.querySelector("#breakdown-body").innerHTML = renderBreakdown(data);
        } catch (err) {
            drawer.querySelector("#breakdown-body").textContent = "Failed: " + err.message;
        }
    }

    function renderBreakdown(d) {
        const bd = d.breakdown || {};
        const c = d.comp || {};
        const fmtPct = (x) => x != null ? (x * 100).toFixed(1) + "%" : "—";
        const fmtPM = (x) => x != null ? Math.round(x) : "?";
        const ratio = bd.ratio != null ? bd.ratio.toFixed(3) : "—";
        const ratioInfo = bd.ratio != null
            ? ` (asking is ${(bd.ratio * 100).toFixed(0)}% of fair value)` : '';

        // Section: Score
        const scoreSec = `
            <div class="bd-section">
                <h3>Score</h3>
                <div class="bd-row"><span>Final score</span><strong class="bd-score">${d.deal_score ?? '—'}/100</strong></div>
                <div class="bd-row"><span>Confidence</span><span>${bd.confidence_label ?? '—'} ±${fmtPM(bd.confidence_pm)}</span></div>
                <div class="bd-row"><span>Percentile rank of asking</span><span>${fmtPct(bd.percentile_rank)} ${bd.percentile_rank != null ? `(cheaper than ${(100 - bd.percentile_rank * 100).toFixed(0)}% of comps)` : ''}</span></div>
                <div class="bd-row"><span>Asking vs fair value ratio</span><span>${ratio}${ratioInfo}</span></div>
                <div class="bd-row"><span>Asking price</span><span>$${bd.asking_price ?? d.price ?? '?'}</span></div>
                <div class="bd-row"><span>Fair value</span><span>$${bd.fair_value ?? d.fair_value ?? '?'} <em class="muted">(${bd.fair_value_source ?? 'unknown'})</em></span></div>
            </div>`;

        // Section: Comps
        const compSec = `
            <div class="bd-section">
                <h3>Comp data <button class="apr-comp-chip ${c.source ? 'apr-src-' + c.source : ''}"
                                     type="button"
                                     data-comp-term="${escapeHtml(c.search_term || '')}"
                                     data-comp-source="${c.source || 'marketplace'}"
                                     style="margin-left:6px;">view ${c.sample_size ?? '?'} listings</button></h3>
                <div class="bd-row"><span>Source</span><span>${c.source || '—'}</span></div>
                <div class="bd-row"><span>Search term</span><span><em>${escapeHtml(c.search_term || '—')}</em></span></div>
                <div class="bd-row"><span>Sample size</span><span>${c.sample_size ?? '—'}</span></div>
                <div class="bd-row"><span>Median</span><span>$${c.median != null ? Math.round(c.median) : '—'}</span></div>
                <div class="bd-row"><span>Mean</span><span>$${c.mean != null ? Math.round(c.mean) : '—'}</span></div>
                <div class="bd-row"><span>Range</span><span>$${c.min != null ? Math.round(c.min) : '—'} – $${c.max != null ? Math.round(c.max) : '—'}</span></div>
                <div class="bd-row"><span>Trimmed median (Tukey)</span><span>$${bd.trimmed_median != null ? Math.round(bd.trimmed_median) : '—'} <em class="muted">(${bd.outliers_dropped ?? 0} outlier${(bd.outliers_dropped ?? 0) === 1 ? '' : 's'} dropped)</em></span></div>
                <div class="bd-row"><span>IQR / median ratio</span><span>${bd.iqr_ratio != null ? bd.iqr_ratio.toFixed(2) : '—'} ${bd.data_quality_poor ? '<em class="bd-warn">poor (>0.8)</em>' : ''}</span></div>
            </div>`;

        // Section: Adjustments
        const adjSec = `
            <div class="bd-section">
                <h3>Adjustments</h3>
                <div class="bd-row"><span>Condition adjustment</span><span>${bd.condition_adjustment ?? 0} pts</span></div>
                <div class="bd-row"><span>Condition flags fired</span><span>${(bd.condition_flags || []).join(', ') || '—'}</span></div>
                <div class="bd-row"><span>Condition note</span><span><em>${escapeHtml(bd.condition_note || '—')}</em></span></div>
                <div class="bd-row"><span>Outlier-rate penalty</span><span>${bd.outlier_rate_penalty ?? 0} pts</span></div>
                <div class="bd-row"><span>Category confidence floor</span><span>${bd.category_confidence_floor ? '±' + bd.category_confidence_floor : 'n/a'}</span></div>
                ${bd.bimodal_split_used ? `<div class="bd-row"><span>Bimodal split</span><span>used cluster: ${bd.bimodal_cluster_chosen}</span></div>` : ''}
            </div>`;

        // Section: Distance / location
        const distSec = `
            <div class="bd-section">
                <h3>Location</h3>
                <div class="bd-row"><span>Seller location</span><span>${escapeHtml(d.seller_location || '—')}</span></div>
                <div class="bd-row"><span>Distance from watch</span><span>${d.distance_km != null ? d.distance_km + ' km' : '—'} ${d.watch_radius_km && d.distance_km > d.watch_radius_km ? '<em class="bd-warn">over radius!</em>' : ''}</span></div>
                <div class="bd-row"><span>Watch radius</span><span>${d.watch_radius_km ? d.watch_radius_km + ' km' : '—'}</span></div>
                <div class="bd-row"><span>Watch keyword</span><span><em>${escapeHtml(d.keyword || '—')}</em></span></div>
            </div>`;

        // Section: Status / notification path
        const sc = d.secondary_check;
        const statusSec = `
            <div class="bd-section">
                <h3>Pipeline status</h3>
                <div class="bd-row"><span>Rejected</span><span>${d.rejected ? '<em class="bd-warn">YES — ' + escapeHtml(d.rejection_reason || '') + '</em>' : 'no'}</span></div>
                <div class="bd-row"><span>Emailed</span><span>${d.notified ? '✓ yes' : 'no'}</span></div>
                ${sc ? `<div class="bd-row"><span>LLM secondary check</span><span><strong>${sc.verdict}</strong> — ${escapeHtml(sc.concern || '')} <em class="muted">(${sc.confidence ?? '?'} conf, ${sc.elapsed_ms ?? '?'}ms)</em></span></div>` : ''}
                <div class="bd-row"><span>Appraisal note</span><span><em>${escapeHtml(d.appraisal_note || '—')}</em></span></div>
                ${d.listing_url ? `<div class="bd-row"><a href="${d.listing_url}" target="_blank" rel="noopener">→ open listing on Marketplace</a></div>` : ''}
            </div>`;

        return scoreSec + compSec + adjSec + distSec + statusSec;
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
                // Comp summary chip — ALWAYS clickable when there's a
                // search term, so any appraised listing can be audited.
                // Falls back to a non-clickable label if we have a
                // sample size but no term, and is omitted entirely
                // when no comp data exists.
                const compSrc = l.comp_source || null;
                const sourceLabel = compSrc === 'ebay' ? 'eBay'
                                  : compSrc === 'marketplace' ? 'FB Mkt'
                                  : 'comps';
                let compChip = '';
                if (l.comp_search_term) {
                    compChip = `<button class="apr-comp-chip ${compSrc ? 'apr-src-' + compSrc : ''}" type="button"
                              data-comp-term="${escapeHtml(l.comp_search_term)}"
                              data-comp-source="${compSrc || 'marketplace'}"
                              title="Click to see the comp listings driving this score">${sourceLabel} · n=${l.comp_sample_size ?? '?'} · med $${l.comp_median != null ? Math.round(l.comp_median) : '?'}</button>`;
                } else if (l.comp_sample_size) {
                    compChip = `<span class="apr-comp-chip">n=${l.comp_sample_size} · med $${l.comp_median != null ? Math.round(l.comp_median) : '?'}</span>`;
                }

                // Score breakdown chip — clickable, opens a drawer
                // showing the full computation (percentile rank, comp
                // stats, condition signals, fair value) so user can
                // see EXACTLY why a score is what it is.
                const breakdownChip = (l.deal_score != null && l.appraised)
                    ? `<button class="apr-bd-chip" type="button"
                              data-listing-id="${escapeHtml(l.id)}"
                              title="Click to see how this score was computed">why?</button>`
                    : '';

                const tail = l.rejected
                    ? `<span class="apr-tail bad">rejected: ${escapeHtml(l.rejection_reason || "n/a")}</span>`
                    : (l.deal_score != null
                        ? `<span class="apr-tail">${escapeHtml(l.appraisal_note || "")} · fair $${l.fair_value != null ? Math.round(l.fair_value) : "?"}</span>`
                        : `<span class="apr-tail muted">${escapeHtml(l.appraisal_note || "no score")}</span>`);
                const href = l.listing_url || "#";
                return `
                    <div class="apr-row apr-row-${l.status}">
                        <a class="apr-link" href="${href}" target="_blank" rel="noopener">
                            ${scoreBadge(l.deal_score, l.status)}
                        </a>
                        <div class="apr-main">
                            <div class="apr-title-row">
                                <a class="apr-title" href="${href}" target="_blank" rel="noopener">${escapeHtml(l.title || "")}</a>
                                ${statusBadge(l.status)}
                            </div>
                            <div class="apr-meta">
                                <span class="apr-kw">${escapeHtml(l.keyword || "—")}</span>
                                <span class="apr-price">${fmtPrice(l.price)}</span>
                                ${l.distance_km != null
                                    ? `<span class="apr-dist ${l.watch_radius_km && l.distance_km > l.watch_radius_km ? 'apr-dist-bad' : ''}" title="${escapeHtml(l.seller_location || '')} → distance from watch home (radius ${l.watch_radius_km ?? '?'}km)">${l.distance_km}km</span>`
                                    : (l.seller_location ? `<span class="apr-dist apr-dist-unknown" title="couldn't geocode ‘${escapeHtml(l.seller_location)}’">${escapeHtml(l.seller_location)}</span>` : '')}
                                <span class="apr-ago">${ago}</span>
                                ${compChip}
                                ${breakdownChip}
                                ${tail}
                            </div>
                        </div>
                    </div>
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
        setupCompDrawerDelegation();

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
