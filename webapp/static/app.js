// Inline detail loader. Click "Load description" -> AJAX to /detail/<id>
// -> render description, source, and re-evaluated pipeline filters in
// place. Keeps the page server-rendered with one targeted JS escape hatch.

(function () {
    "use strict";

    function init() {
        document.querySelectorAll(".fetch-detail-btn").forEach(function (btn) {
            btn.addEventListener("click", onFetchClick);
        });
        document.querySelectorAll(".appraise-btn").forEach(function (btn) {
            btn.addEventListener("click", onAppraiseClick);
        });
        document.querySelectorAll(".comps-toggle").forEach(function (btn) {
            btn.addEventListener("click", onCompsToggleClick);
        });
    }

    async function onCompsToggleClick(e) {
        const btn = e.currentTarget;
        const term = btn.dataset.compTerm;
        const source = btn.dataset.compSource || "marketplace";
        const pane = btn.parentElement.querySelector(".comps-pane");
        const spinner = pane.querySelector(".comps-spinner");
        const content = pane.querySelector(".comps-content");

        // Toggle: if already loaded and visible, hide; if hidden, show.
        if (!pane.hidden && content.dataset.loaded === "1") {
            pane.hidden = true;
            btn.textContent = btn.textContent.replace("▴", "▾");
            return;
        }
        if (pane.hidden && content.dataset.loaded === "1") {
            pane.hidden = false;
            btn.textContent = btn.textContent.replace("▾", "▴");
            return;
        }

        if (!term) {
            content.textContent = "no comp term recorded for this listing";
            pane.hidden = false;
            content.dataset.loaded = "1";
            return;
        }

        pane.hidden = false;
        spinner.hidden = false;
        content.innerHTML = "";
        btn.disabled = true;

        try {
            const url = "/api/comps?term=" + encodeURIComponent(term) +
                        "&source=" + encodeURIComponent(source);
            const res = await fetch(url);
            const data = await res.json();
            renderComps(content, data);
            content.dataset.loaded = "1";
            btn.textContent = btn.textContent.replace("▾", "▴");
        } catch (err) {
            content.textContent = "Failed to load comps: " + err.message;
        } finally {
            spinner.hidden = true;
            btn.disabled = false;
        }
    }

    function renderComps(container, data) {
        if (!data.rows || data.rows.length === 0) {
            container.innerHTML = '<div class="muted">' +
                'No cached comps for "' + escapeHtml(data.term) +
                '". They may have expired (12h TTL); re-appraise to refresh.' +
                '</div>';
            return;
        }
        const max = data.max || 1;
        const median = data.median || 0;

        const header = el("div", "comps-summary",
            data.sample_size + " comp(s) · " +
            "median $" + Math.round(data.median) + " · " +
            "mean $" + Math.round(data.mean) + " · " +
            "range $" + Math.round(data.min) + "-$" + Math.round(data.max)
        );
        container.appendChild(header);

        const list = document.createElement("ul");
        list.className = "comps-list";
        data.rows.forEach(function (row) {
            const li = document.createElement("li");
            li.className = "comp-row";
            const isNearMedian = Math.abs(row.price - median) / median < 0.15;
            if (isNearMedian) li.classList.add("near-median");

            const bar = document.createElement("div");
            bar.className = "comp-bar";
            bar.style.width = ((row.price / max) * 100).toFixed(1) + "%";

            const price = document.createElement("span");
            price.className = "comp-price";
            price.textContent = "$" + Math.round(row.price);

            const titleEl = document.createElement("a");
            titleEl.className = "comp-title";
            titleEl.href = row.listing_url || "#";
            titleEl.target = "_blank";
            titleEl.rel = "noopener";
            titleEl.textContent = row.title || "(no title)";

            const loc = document.createElement("span");
            loc.className = "comp-loc muted";
            loc.textContent = row.location || "";

            li.appendChild(bar);
            li.appendChild(price);
            li.appendChild(titleEl);
            li.appendChild(loc);
            list.appendChild(li);
        });
        container.appendChild(list);
    }

    async function onAppraiseClick(e) {
        const btn = e.currentTarget;
        const card = btn.closest(".card");
        const pane = card.querySelector(".appraise-pane");
        const spinner = pane.querySelector(".appraise-spinner");
        const result = pane.querySelector(".appraise-result");

        const listingId = card.dataset.listingId;
        const title = card.dataset.listingTitle || "";
        const rawPrice = card.dataset.rawPrice || "0";

        pane.hidden = false;
        spinner.hidden = false;
        result.innerHTML = "";
        btn.disabled = true;
        btn.textContent = "Scoring…";

        try {
            const url =
                "/appraise/" + encodeURIComponent(listingId) +
                "?title=" + encodeURIComponent(title) +
                "&raw_price=" + encodeURIComponent(rawPrice);
            const res = await fetch(url, { method: "POST" });
            const data = await res.json();

            if (data.ok) {
                renderFreshAppraisal(result, data);
                // Wire up the comps-toggle and any other interactive
                // children we just injected.
                result.querySelectorAll(".comps-toggle").forEach(function (el) {
                    el.addEventListener("click", onCompsToggleClick);
                });
                btn.textContent = "Re-appraise";
            } else {
                result.textContent = "Failed: " + (data.error || "unknown error");
                btn.textContent = "Retry";
            }
        } catch (err) {
            result.textContent = "Network error: " + err.message;
            btn.textContent = "Retry";
        } finally {
            spinner.hidden = true;
            btn.disabled = false;
        }
    }

    function renderFreshAppraisal(container, data) {
        const bd = data.breakdown || {};

        // Unscoreable path: refuse to fake a number.
        if (bd.unscoreable) {
            const term = data.search_term || "";
            container.innerHTML =
                '<div class="appraisal-display unscoreable-display">' +
                '<div class="unscoreable-headline">' +
                    '<span class="unscoreable-icon">∅</span>' +
                    '<span class="unscoreable-title">Not enough data to score</span>' +
                '</div>' +
                '<div class="unscoreable-reason">' + escapeHtml(bd.unscoreable_reason || "") + '</div>' +
                (bd.median ?
                    '<div class="unscoreable-stats muted">' +
                        (bd.sample_size || 0) + ' comp(s) found · median $' + Math.round(bd.median) +
                        (bd.iqr ? ' · IQR $' + Math.round(bd.iqr) : '') +
                    '</div>'
                : '') +
                (term && bd.sample_size ?
                    '<button class="comps-toggle muted" type="button" ' +
                        'data-comp-term="' + escapeHtml(term) + '" ' +
                        'data-comp-source="marketplace" ' +
                        'title="See the comps we did find">' +
                        'See ' + bd.sample_size + ' comp(s) ▾</button>' +
                    '<div class="comps-pane" hidden>' +
                        '<div class="comps-spinner" hidden>loading…</div>' +
                        '<div class="comps-content"></div>' +
                    '</div>'
                : '') +
                '</div>';
            return;
        }

        const score = data.deal_score;
        const klass = score >= 70 ? "score-high"
                    : score >= 50 ? "score-mid" : "score-low";
        const ratio = data.ratio || 0;
        const asking = bd.asking_price;
        const fair = data.fair_value;
        const conf = data.confidence || bd.confidence_label;
        const confPm = data.confidence_pm || bd.confidence_pm;

        // Three-dot confidence indicator markup matching the SSR cards.
        let confMarkup = "";
        if (conf) {
            const lit = (level) =>
                (conf === "high" || (conf === "medium" && level !== "high") ||
                 (conf === "low" && level === "low")) ? "lit" : "";
            confMarkup =
                '<div class="confidence-bar conf-' + conf + '" ' +
                'title="confidence interval ±' + confPm + ' on the score; based on n=' + (bd.sample_size || 0) + ' comp(s)">' +
                '<div class="conf-dots">' +
                    '<span class="conf-dot ' + lit("low") + '"></span>' +
                    '<span class="conf-dot ' + lit("medium") + '"></span>' +
                    '<span class="conf-dot ' + lit("high") + '"></span>' +
                '</div>' +
                '<span class="conf-label">' + escapeHtml(conf) + ' confidence</span>' +
                '<span class="conf-detail muted">n=' + (bd.sample_size || 0) +
                (bd.outliers_dropped ? ' (−' + bd.outliers_dropped + ' outlier' +
                    (bd.outliers_dropped > 1 ? 's' : '') + ')' : '') +
                '</span></div>';
        }

        const compsToggle = (data.search_term && data.comp_sample_size) ?
            '<button class="comps-toggle muted" type="button" ' +
                'data-comp-term="' + escapeHtml(data.search_term) + '" ' +
                'data-comp-source="marketplace" ' +
                'title="See the listings this median is based on">' +
                data.comp_sample_size + ' comp(s)' +
                (data.outliers_dropped ? ', ' + data.outliers_dropped + ' outlier(s) dropped' : '') +
                (data.comp_median ? ' · raw median $' + Math.round(data.comp_median) : '') +
                ' ▾</button>' +
            '<div class="comps-pane" hidden>' +
                '<div class="comps-spinner" hidden>loading…</div>' +
                '<div class="comps-content"></div>' +
            '</div>'
            : '';

        const dataWarn = bd.data_quality_poor ?
            '<div class="data-warning" title="IQR exceeds trimmed median; comp distribution is too dispersed for a single number to be reliable. Consider the percentile rank instead.">⚠ comps too varied — score unreliable</div>'
            : '';

        const pctRank = (bd.percentile_rank !== null && bd.percentile_rank !== undefined) ?
            '<div class="pct-rank">Asking sits at the <strong>' +
            Math.round(bd.percentile_rank * 100) + '<sup>th</sup></strong> percentile of comps ' +
            '<span class="pct-detail muted">(cheaper than ' +
            Math.round((1 - bd.percentile_rank) * 100) + '% of similar listings)</span></div>'
            : '';

        const finalKlass = bd.data_quality_poor ? "score-low" : klass;

        container.innerHTML =
            '<div class="appraisal-display ' + finalKlass + '">' +
            '<div class="score-row">' +
                '<span class="score-num">' + score + '</span>' +
                (confPm ? '<span class="score-pm">±' + confPm + '</span>' : '') +
                '<span class="score-label">deal score</span>' +
                (fair ? '<span class="score-fair">fair: $' + Math.round(fair) + '</span>' : '') +
            '</div>' +
            dataWarn +
            pctRank +
            confMarkup +
            (asking && fair ?
                '<div class="score-math">' +
                    '<span class="math-eq">$' + Math.round(asking) + ' ÷ $' + Math.round(fair) + ' = ratio <strong>' + ratio.toFixed(2) + '</strong></span>' +
                    '<span class="math-source muted">' + escapeHtml(data.fair_value_source || "") + '</span>' +
                '</div>'
            : '') +
            (data.note ? '<div class="score-note">' + escapeHtml(data.note) + '</div>' : '') +
            compsToggle +
            '</div>';
    }

    function escapeHtml(s) {
        const d = document.createElement("div");
        d.textContent = s;
        return d.innerHTML;
    }

    async function onFetchClick(e) {
        const btn = e.currentTarget;
        const card = btn.closest(".card");
        const pane = card.querySelector(".detail-pane");
        const spinner = pane.querySelector(".detail-spinner");
        const text = pane.querySelector(".detail-text");
        const source = pane.querySelector(".detail-source");
        const pipeline = pane.querySelector(".detail-pipeline");

        const listingId = card.dataset.listingId;
        const title = card.dataset.listingTitle || "";
        const rawPrice = card.dataset.rawPrice || "0";

        pane.hidden = false;
        spinner.hidden = false;
        text.textContent = "";
        source.textContent = "";
        pipeline.innerHTML = "";
        btn.disabled = true;
        btn.textContent = "Loading…";

        try {
            const url =
                "/detail/" + encodeURIComponent(listingId) +
                "?title=" + encodeURIComponent(title) +
                "&raw_price=" + encodeURIComponent(rawPrice);
            const res = await fetch(url);
            const data = await res.json();

            if (data.description) {
                text.textContent = data.description;
                source.textContent = "source: " + (data.source || "unknown");
                renderPipeline(pipeline, data.pipeline);
                btn.textContent = "Loaded";
            } else {
                text.textContent =
                    "No description returned. " +
                    (data.error ? "(" + data.error + ")" : "");
                btn.disabled = false;
                btn.textContent = "Retry";
            }
        } catch (err) {
            text.textContent = "Network error: " + err.message;
            btn.disabled = false;
            btn.textContent = "Retry";
        } finally {
            spinner.hidden = true;
        }
    }

    function renderPipeline(container, p) {
        if (!p) return;
        const rows = [];
        if (p.price_extracted) {
            rows.push(
                el("div", "pipeline-flag flag-extract",
                   "price extracted: $" + p.resolved_price +
                   " (raw $" + p.raw_price + ")")
            );
        }
        if (p.rejected) {
            rows.push(
                el("div", "pipeline-flag flag-reject",
                   "would reject · " + p.rejection_reason)
            );
        }
        if (rows.length === 0) {
            rows.push(el("div", "muted", "pipeline: pass"));
        }
        rows.forEach(function (r) { container.appendChild(r); });
    }

    function el(tag, className, textContent) {
        const e = document.createElement(tag);
        e.className = className;
        e.textContent = textContent;
        return e;
    }

    // Scroll-triggered fade-up reveal — Intersection Observer over
    // every .card and .reveal block. Adds .in-view when the element
    // crosses ~85% into the viewport. Idempotent — re-runs harmlessly.
    function setupReveal() {
        const els = document.querySelectorAll(".card, .reveal");
        if (!("IntersectionObserver" in window) || els.length === 0) {
            els.forEach((el) => el.classList.add("in-view"));
            return;
        }
        const obs = new IntersectionObserver((entries) => {
            entries.forEach((e, i) => {
                if (e.isIntersecting) {
                    // Tiny stagger for grouped cards, capped so it
                    // doesn't drag forever on dense grids.
                    const delay = Math.min(i * 60, 240);
                    setTimeout(() => e.target.classList.add("in-view"), delay);
                    obs.unobserve(e.target);
                }
            });
        }, { threshold: 0.12, rootMargin: "0px 0px -80px 0px" });
        els.forEach((el) => obs.observe(el));
    }

    // Subscribe form: load active searches into the dropdown, handle submit.
    async function setupSubscribeForm() {
        const form = document.getElementById("subscribe-form");
        if (!form) return;
        const select = form.querySelector("#sub-search");
        const banner = document.getElementById("subscribe-banner");

        try {
            const res = await fetch("/api/searches");
            const data = await res.json();
            const opts = (data.searches || []).map(
                (s) => '<option value="' + s.id + '">' +
                       escapeHtml(s.keyword) + ' (' + s.radius_km + ' km)</option>'
            ).join("");
            select.innerHTML = opts || '<option value="">no saved searches yet</option>';
        } catch (err) {
            select.innerHTML = '<option value="">could not load searches</option>';
        }

        form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            banner.hidden = true;
            banner.classList.remove("is-error");
            const fd = new FormData(form);
            try {
                const res = await fetch("/api/subscribe", {
                    method: "POST",
                    body: fd,
                });
                const data = await res.json();
                if (data.ok) {
                    banner.textContent = data.message ||
                        "Subscribed. We'll email you when a listing scores above your threshold.";
                    banner.hidden = false;
                    form.reset();
                    // Reload search options so the dropdown defaults are fresh.
                    setupSubscribeForm();
                } else {
                    banner.textContent = "Couldn't subscribe: " + (data.error || "unknown error");
                    banner.classList.add("is-error");
                    banner.hidden = false;
                }
            } catch (err) {
                banner.textContent = "Network error: " + err.message;
                banner.classList.add("is-error");
                banner.hidden = false;
            }
        });
    }

    function bootAll() {
        init();
        setupReveal();
        setupSubscribeForm();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootAll);
    } else {
        bootAll();
    }
})();
