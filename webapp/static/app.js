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
            // Each row is a full <a> wrapping all cells so clicking
            // anywhere on the row opens the comp listing. (Title-only
            // links were too small a click target.) Falls back to
            // <li> when listing_url is missing.
            const useLink = !!row.listing_url;
            const li = document.createElement(useLink ? "a" : "li");
            li.className = "comp-row" + (useLink ? " comp-row-link" : "");
            if (useLink) {
                li.href = row.listing_url;
                li.target = "_blank";
                li.rel = "noopener";
            }
            const isNearMedian = Math.abs(row.price - median) / median < 0.15;
            if (isNearMedian) li.classList.add("near-median");

            const bar = document.createElement("div");
            bar.className = "comp-bar";
            bar.style.width = ((row.price / max) * 100).toFixed(1) + "%";

            const price = document.createElement("span");
            price.className = "comp-price";
            price.textContent = "$" + Math.round(row.price);

            const titleEl = document.createElement("span");
            titleEl.className = "comp-title";
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
                        'data-comp-source="' + escapeHtml(data.comp_source || 'marketplace') + '" ' +
                        'title="See the comps we did find">' +
                        'See ' + bd.sample_size + ' ' + (data.comp_source || 'mkt') + ' comp(s) ▾</button>' +
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
                'data-comp-source="' + escapeHtml(data.comp_source || 'marketplace') + '" ' +
                'title="See the listings this median is based on">' +
                data.comp_sample_size + ' ' + (data.comp_source || 'mkt') + ' comp(s)' +
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

    function renderSuccessBanner(banner, data) {
        const created = data.created || [];
        const dup = data.duplicate || [];
        const conf = data.confirmation;
        const total = created.length + dup.length;

        let confLine = "";
        if (conf && conf.ok) {
            confLine = `<div class="banner-line">✉ confirmation email sent (via <code>${conf.backend}</code>)</div>`;
        } else if (conf && !conf.ok) {
            confLine = `<div class="banner-line muted">no confirmation email — backend <code>${conf.backend || "?"}</code>: ${escapeHtml(conf.message || "")}</div>`;
        }

        const newLine = created.length
            ? `<div class="banner-line">+ NEW: ${created.map(c => escapeHtml(c.keyword)).join(", ")}</div>`
            : "";
        const dupLine = dup.length
            ? `<div class="banner-line muted">↻ re-activated: ${dup.map(c => escapeHtml(c.keyword)).join(", ")}</div>`
            : "";

        banner.innerHTML =
            `<div class="banner-stamp">order ticket · confirmed</div>` +
            `<h3 class="banner-headline">Thanks — you're on watch.</h3>` +
            `<div class="banner-summary">${total} watch${total === 1 ? "" : "es"} now active.</div>` +
            newLine + dupLine + confLine;
    }

    async function setupBulkForm() {
        const form = document.getElementById("bulk-form");
        if (!form) return;
        const banner = document.getElementById("bulk-banner");

        form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            banner.hidden = true;
            banner.classList.remove("is-error");
            const fd = new FormData(form);
            try {
                const res = await fetch("/api/searches/bulk", {
                    method: "POST",
                    body: fd,
                });
                const data = await res.json();
                if (data.ok) {
                    renderSuccessBanner(banner, data);
                    banner.hidden = false;
                    form.reset();
                    setupSubscribeForm();
                } else {
                    banner.innerHTML = "<strong>Couldn't save:</strong> " + escapeHtml(data.error || "unknown error");
                    banner.classList.add("is-error");
                    banner.hidden = false;
                }
            } catch (err) {
                banner.innerHTML = "<strong>Network error:</strong> " + escapeHtml(err.message);
                banner.classList.add("is-error");
                banner.hidden = false;
            }
        });
    }

    // --- Side panels (left = test, right = saved searches) -------------

    function setupSidePanels() {
        const overlay = document.getElementById("panel-overlay");
        const panels = {
            left: document.getElementById("panel-left"),
            right: document.getElementById("panel-right"),
        };

        function open(side) {
            const panel = panels[side];
            if (!panel) return;
            // close the OTHER side first (only one open at a time on mobile)
            const other = side === "left" ? "right" : "left";
            close(other, false);
            panel.hidden = false;
            // double rAF so the transition fires from the off-screen state
            requestAnimationFrame(() =>
                requestAnimationFrame(() => panel.classList.add("is-open"))
            );
            overlay.hidden = false;
            requestAnimationFrame(() => overlay.classList.add("is-visible"));
            document.body.style.overflow = "hidden";
        }

        function close(side, hideOverlay = true) {
            const panel = panels[side];
            if (!panel) return;
            panel.classList.remove("is-open");
            // hide after the transition so it doesn't disappear instantly
            setTimeout(() => {
                if (!panel.classList.contains("is-open")) panel.hidden = true;
            }, 380);
            if (hideOverlay) {
                overlay.classList.remove("is-visible");
                setTimeout(() => {
                    if (!overlay.classList.contains("is-visible")) {
                        overlay.hidden = true;
                        document.body.style.overflow = "";
                    }
                }, 280);
            }
        }

        document.querySelectorAll("[data-toggle-panel]").forEach((btn) => {
            btn.addEventListener("click", (e) => {
                const side = e.currentTarget.dataset.togglePanel;
                const panel = panels[side];
                if (panel.classList.contains("is-open")) {
                    close(side);
                } else {
                    open(side);
                }
            });
        });

        overlay.addEventListener("click", () => {
            close("left");
            close("right");
        });

        // Esc closes either
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") {
                close("left");
                close("right");
            }
        });
    }

    // --- Tab bar inside the saved-searches panel ----------------------

    function setupTabs() {
        const tabBar = document.querySelector(".tab-bar");
        if (!tabBar) return;
        tabBar.querySelectorAll(".tab-btn").forEach((btn) => {
            btn.addEventListener("click", () => {
                const tab = btn.dataset.tab;
                tabBar.querySelectorAll(".tab-btn").forEach((b) =>
                    b.classList.toggle("is-active", b === btn)
                );
                document.querySelectorAll(".tab-pane").forEach((p) => {
                    p.classList.toggle("is-active", p.dataset.pane === tab);
                });
            });
        });
    }

    // --- Single-search form (in right panel) --------------------------

    function setupSingleForm() {
        const form = document.getElementById("single-form");
        if (!form) return;
        const banner = document.getElementById("single-banner");

        form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            banner.hidden = true;
            banner.classList.remove("is-error");
            // Reuse the bulk endpoint with a single keyword — saves wiring
            // up a separate route. Same dedup logic applies.
            const fd = new FormData(form);
            try {
                const res = await fetch("/api/searches/bulk", {
                    method: "POST",
                    body: fd,
                });
                const data = await res.json();
                if (data.ok) {
                    renderSuccessBanner(banner, data);
                    banner.hidden = false;
                    form.reset();
                    setupSubscribeForm();
                } else {
                    banner.innerHTML = "<strong>Couldn't save:</strong> " + escapeHtml(data.error || "unknown");
                    banner.classList.add("is-error");
                    banner.hidden = false;
                }
            } catch (err) {
                banner.innerHTML = "<strong>Network error:</strong> " + escapeHtml(err.message);
                banner.classList.add("is-error");
                banner.hidden = false;
            }
        });
    }

    // --- Home location strip with address autocomplete ---------------
    //
    // UX: user types into a single field, we debounce 250ms, hit our
    // /api/geocode endpoint (which proxies Nominatim), render a
    // suggestion dropdown, and on click we populate the hidden lat/lng
    // fields. Save button is disabled until a suggestion is chosen so
    // we can't submit free-text without a coordinate.

    async function setupHomeLocation() {
        const wrap = document.getElementById("home-location");
        if (!wrap) return;
        const display = wrap.querySelector(".home-loc-display");
        const form = wrap.querySelector("#home-form");
        const labelEl = wrap.querySelector(".home-loc-label");
        const editBtn = wrap.querySelector(".home-loc-edit");
        const cancelBtn = wrap.querySelector(".home-loc-cancel");
        const fLabel = form.querySelector("#home-label");
        const fLat = form.querySelector("#home-lat");
        const fLng = form.querySelector("#home-lng");
        const search = form.querySelector("#home-search");
        const suggBox = form.querySelector("#geo-suggestions");
        const selectedRow = form.querySelector(".home-loc-selected");
        const selectedLbl = form.querySelector(".home-loc-selected-label");
        const submitBtn = form.querySelector('button[type="submit"]');

        let debounceTimer = null;
        let lastQuery = "";
        let activeReq = 0;

        function clearSelection() {
            fLabel.value = "";
            fLat.value = "";
            fLng.value = "";
            selectedRow.hidden = true;
            selectedLbl.textContent = "";
            submitBtn.disabled = true;
        }

        function setSelection(item) {
            fLabel.value = item.label;
            fLat.value = item.lat;
            fLng.value = item.lng;
            selectedLbl.textContent = item.label;
            selectedRow.hidden = false;
            submitBtn.disabled = false;
            suggBox.hidden = true;
            suggBox.innerHTML = "";
        }

        function renderSuggestions(items) {
            suggBox.innerHTML = "";
            if (!items || items.length === 0) {
                suggBox.hidden = true;
                return;
            }
            items.forEach((item) => {
                const row = document.createElement("button");
                row.type = "button";
                row.className = "geo-sugg";
                row.textContent = item.label;
                row.addEventListener("click", () => {
                    search.value = item.label;
                    setSelection(item);
                });
                suggBox.appendChild(row);
            });
            suggBox.hidden = false;
        }

        async function load() {
            try {
                const res = await fetch("/api/settings");
                const data = await res.json();
                if (data.home_label || data.home_latitude !== null) {
                    const txt = data.home_label
                        ? data.home_label
                        : `${data.home_latitude}, ${data.home_longitude}`;
                    labelEl.textContent = txt;
                    fLabel.value = data.home_label || "";
                    fLat.value = data.home_latitude ?? "";
                    fLng.value = data.home_longitude ?? "";
                    if (data.home_label) {
                        search.value = data.home_label;
                        selectedLbl.textContent = data.home_label;
                        selectedRow.hidden = false;
                        submitBtn.disabled = false;
                    }
                }
            } catch (e) { /* leave default */ }
        }

        async function fetchSuggestions(q) {
            const reqId = ++activeReq;
            try {
                const res = await fetch("/api/geocode?q=" + encodeURIComponent(q));
                const data = await res.json();
                // Drop stale responses (user kept typing)
                if (reqId !== activeReq) return;
                renderSuggestions(data.results || []);
            } catch (err) {
                if (reqId !== activeReq) return;
                suggBox.hidden = true;
            }
        }

        search.addEventListener("input", () => {
            const q = search.value.trim();
            // Typing invalidates any prior selection
            clearSelection();
            if (debounceTimer) clearTimeout(debounceTimer);
            if (q.length < 3) {
                suggBox.hidden = true;
                return;
            }
            if (q === lastQuery) return;
            lastQuery = q;
            debounceTimer = setTimeout(() => fetchSuggestions(q), 250);
        });

        // Hide dropdown when clicking outside
        document.addEventListener("click", (ev) => {
            if (!form.contains(ev.target)) suggBox.hidden = true;
        });

        // Esc closes the dropdown without closing the form
        search.addEventListener("keydown", (ev) => {
            if (ev.key === "Escape") {
                ev.stopPropagation();
                suggBox.hidden = true;
            }
        });

        editBtn.addEventListener("click", () => {
            form.hidden = false;
            display.hidden = true;
            search.focus();
        });

        cancelBtn.addEventListener("click", () => {
            form.hidden = true;
            display.hidden = false;
            suggBox.hidden = true;
        });

        form.addEventListener("submit", async (ev) => {
            ev.preventDefault();
            // Build a clean payload from hidden fields (the visible
            // search input is just a UX helper — we don't send it).
            const payload = new FormData();
            payload.append("home_label", fLabel.value);
            payload.append("home_latitude", fLat.value);
            payload.append("home_longitude", fLng.value);
            try {
                const res = await fetch("/api/settings", {
                    method: "POST",
                    body: payload,
                });
                const data = await res.json();
                if (data.ok) {
                    form.hidden = true;
                    display.hidden = false;
                    suggBox.hidden = true;
                    load();
                } else {
                    alert("Couldn't save: " + (data.error || "unknown"));
                }
            } catch (err) {
                alert("Network error: " + err.message);
            }
        });

        await load();
    }

    // After /search renders results, jump the user past the hero
    // straight to the listings grid. The form is a full POST so the
    // page reloads with #listings present in the DOM whenever there
    // are results — that's the trigger.
    function setupSearchScroll() {
        const listings = document.getElementById("listings");
        if (!listings) return;
        // Smooth scroll once layout settles. requestAnimationFrame
        // double-tap so the in-view animation observer attaches first.
        requestAnimationFrame(() =>
            requestAnimationFrame(() =>
                listings.scrollIntoView({ behavior: "smooth", block: "start" })
            )
        );
    }

    // --- Per-watch dashboard (Manage tab) -----------------------------
    //
    // Lists every watch with its threshold + activity stats. Inline
    // controls for: pause/resume, edit threshold, delete. Optimistic
    // updates: we patch the row UI immediately and revert on failure
    // rather than waiting for the round trip.

    function setupWatchesDashboard() {
        const list = document.getElementById("watches-list");
        const refreshBtn = document.getElementById("watches-refresh");
        if (!list) return;

        // Refresh whenever the Manage tab becomes active OR the user
        // clicks the refresh button OR a watch was just created.
        async function load() {
            try {
                const res = await fetch("/api/watches");
                const data = await res.json();
                render(data.watches || []);
            } catch (err) {
                list.innerHTML =
                    '<div class="watches-empty muted is-error">' +
                    'failed to load: ' + escapeHtml(err.message) + '</div>';
            }
        }

        function render(watches) {
            if (watches.length === 0) {
                list.innerHTML =
                    '<div class="watches-empty muted">' +
                    'no watches yet — save one in the <em>Single</em> or <em>List</em> tab.' +
                    '</div>';
                return;
            }
            list.innerHTML = watches.map(renderRow).join("");
            list.querySelectorAll(".watch-row").forEach(wireRow);
        }

        function renderRow(w) {
            const active = w.active ? "is-active" : "is-paused";
            const status = w.active ? "live" : "paused";
            const thresh = w.score_threshold ?? "—";
            const lastScrape = w.last_scrape ? timeAgo(w.last_scrape) : "never polled";
            const radius = w.radius_km ?? "—";
            const priceHint =
                w.price_min && w.price_max ? `$${w.price_min}–$${w.price_max}` :
                w.price_max ? `≤ $${w.price_max}` :
                w.price_min ? `≥ $${w.price_min}` : "any price";

            return (
                `<div class="watch-row ${active}" data-watch-id="${w.id}">` +
                  `<div class="watch-line-1">` +
                    `<span class="watch-status">${status}</span>` +
                    `<strong class="watch-keyword">${escapeHtml(w.keyword)}</strong>` +
                    `<span class="watch-meta muted">${radius}km · ${escapeHtml(priceHint)}</span>` +
                  `</div>` +
                  `<div class="watch-line-2">` +
                    `<span class="watch-stat">${w.hit_count}<small>hits</small></span>` +
                    `<span class="watch-stat">${w.total_seen}<small>seen</small></span>` +
                    `<span class="watch-stat watch-thresh">` +
                      `<span class="thresh-label">alert ≥</span>` +
                      `<input type="number" class="thresh-input" value="${thresh}" min="0" max="100" />` +
                    `</span>` +
                    `<span class="watch-last muted">${escapeHtml(lastScrape)}</span>` +
                  `</div>` +
                  `<div class="watch-actions">` +
                    `<button class="btn-tiny btn-pause" type="button">` +
                      (w.active ? "Pause" : "Resume") + `</button>` +
                    `<button class="btn-tiny btn-delete" type="button">Delete</button>` +
                  `</div>` +
                `</div>`
            );
        }

        function wireRow(row) {
            const id = row.dataset.watchId;
            const pauseBtn = row.querySelector(".btn-pause");
            const deleteBtn = row.querySelector(".btn-delete");
            const threshInput = row.querySelector(".thresh-input");

            pauseBtn.addEventListener("click", async () => {
                const wasActive = row.classList.contains("is-active");
                pauseBtn.disabled = true;
                const ok = await patchWatch(id, { active: (!wasActive).toString() });
                pauseBtn.disabled = false;
                if (ok) load();
            });

            deleteBtn.addEventListener("click", async () => {
                const kw = row.querySelector(".watch-keyword").textContent;
                if (!confirm(`Delete watch "${kw}"? This cannot be undone.`)) return;
                deleteBtn.disabled = true;
                try {
                    const res = await fetch("/api/watches/" + id, { method: "DELETE" });
                    const data = await res.json();
                    if (data.ok) {
                        row.style.opacity = "0";
                        setTimeout(load, 220);
                    } else {
                        alert("Couldn't delete: " + (data.error || "unknown"));
                        deleteBtn.disabled = false;
                    }
                } catch (err) {
                    alert("Network error: " + err.message);
                    deleteBtn.disabled = false;
                }
            });

            // Threshold edit: commit on blur or Enter
            const commitThresh = async () => {
                const v = parseInt(threshInput.value, 10);
                if (isNaN(v) || v < 0 || v > 100) {
                    threshInput.classList.add("is-error");
                    return;
                }
                threshInput.classList.remove("is-error");
                threshInput.disabled = true;
                const ok = await patchWatch(id, { score_threshold: v.toString() });
                threshInput.disabled = false;
                if (ok) {
                    threshInput.classList.add("just-saved");
                    setTimeout(() => threshInput.classList.remove("just-saved"), 800);
                }
            };
            threshInput.addEventListener("blur", commitThresh);
            threshInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); threshInput.blur(); }
            });
        }

        async function patchWatch(id, body) {
            try {
                const fd = new FormData();
                Object.entries(body).forEach(([k, v]) => fd.append(k, v));
                const res = await fetch("/api/watches/" + id, {
                    method: "PATCH",
                    body: fd,
                });
                const data = await res.json();
                if (!data.ok) {
                    alert("Couldn't save: " + (data.error || "unknown"));
                    return false;
                }
                return true;
            } catch (err) {
                alert("Network error: " + err.message);
                return false;
            }
        }

        // Refresh hooks
        refreshBtn?.addEventListener("click", load);
        // Auto-load when the Manage tab is opened
        document.querySelectorAll('.tab-btn[data-tab="manage"]').forEach((btn) => {
            btn.addEventListener("click", load);
        });
        // Initial load lazy: only when the panel actually opens
        const panelRight = document.getElementById("panel-right");
        if (panelRight) {
            const obs = new MutationObserver(() => {
                if (panelRight.classList.contains("is-open")) load();
            });
            obs.observe(panelRight, { attributes: true, attributeFilter: ["class"] });
        }
    }

    // Pretty "X minutes ago" formatting for last-scrape timestamps.
    function timeAgo(iso) {
        const t = new Date(iso).getTime();
        const diff = Math.max(0, Date.now() - t);
        const m = Math.floor(diff / 60000);
        if (m < 1) return "just now";
        if (m < 60) return m + " min ago";
        const h = Math.floor(m / 60);
        if (h < 24) return h + "h ago";
        const d = Math.floor(h / 24);
        return d + "d ago";
    }

    // ---------- BULK EDIT ALL WATCHES -------------------------------------
    //
    // Each row in the bulk-edit form is a (checkbox, input) pair. Only
    // checked rows participate in the update payload — so the user can
    // change just the threshold without accidentally clearing prices.
    function setupBulkEdit() {
        const btn = document.getElementById("bulk-apply-btn");
        const status = document.getElementById("bulk-apply-status");
        if (!btn) return;

        // Map: payload field -> {check, value-getter}
        const fields = {
            score_threshold:        () => ({on: document.getElementById("bulk-thresh-on").checked,
                                           v: document.getElementById("bulk-thresh").value}),
            radius_km:              () => ({on: document.getElementById("bulk-radius-on").checked,
                                           v: document.getElementById("bulk-radius").value}),
            price_min:              () => ({on: document.getElementById("bulk-pmin-on").checked,
                                           v: document.getElementById("bulk-pmin").value}),
            price_max:              () => ({on: document.getElementById("bulk-pmax-on").checked,
                                           v: document.getElementById("bulk-pmax").value}),
            daily_summary_enabled:  () => ({on: document.getElementById("bulk-summary-on").checked,
                                           v: document.getElementById("bulk-summary").value}),
            active:                 () => ({on: document.getElementById("bulk-active-on").checked,
                                           v: document.getElementById("bulk-active").value}),
        };

        // Auto-tick the checkbox when the user types in its input — saves a
        // confused "why didn't anything happen" moment.
        const pairs = [
            ["bulk-thresh", "bulk-thresh-on"],
            ["bulk-radius", "bulk-radius-on"],
            ["bulk-pmin",    "bulk-pmin-on"],
            ["bulk-pmax",    "bulk-pmax-on"],
            ["bulk-summary", "bulk-summary-on"],
            ["bulk-active",  "bulk-active-on"],
        ];
        pairs.forEach(([inp, chk]) => {
            const i = document.getElementById(inp);
            const c = document.getElementById(chk);
            if (!i || !c) return;
            i.addEventListener("input",  () => { c.checked = true; });
            i.addEventListener("change", () => { c.checked = true; });
        });

        btn.addEventListener("click", async () => {
            const payload = {};
            for (const [key, getFn] of Object.entries(fields)) {
                const {on, v} = getFn();
                if (on) payload[key] = v;
            }
            if (Object.keys(payload).length === 0) {
                status.textContent = "tick at least one box first";
                status.classList.add("is-error");
                return;
            }

            const summary = Object.entries(payload)
                .map(([k, v]) => k + "=" + v).join(", ");
            if (!confirm(
                `Apply to ALL watches:\n\n  ${summary}\n\n` +
                `Continue?`
            )) return;

            btn.disabled = true;
            status.classList.remove("is-error");
            status.textContent = "applying…";
            try {
                const res = await fetch("/api/watches/bulk-update", {
                    method: "POST",
                    headers: {"Content-Type": "application/json"},
                    body: JSON.stringify(payload),
                });
                const data = await res.json();
                if (data.ok) {
                    status.textContent =
                        `✓ updated ${data.watches_updated} watch row(s), ` +
                        `${data.subscribers_updated} subscription row(s)`;
                    // Refresh the watches list so user sees the change
                    document.getElementById("watches-refresh").click();
                } else {
                    status.classList.add("is-error");
                    status.textContent = "failed: " + (data.error || "unknown");
                }
            } catch (err) {
                status.classList.add("is-error");
                status.textContent = "network error: " + err.message;
            } finally {
                btn.disabled = false;
            }
        });
    }

    function bootAll() {
        init();
        setupReveal();
        setupSidePanels();
        setupTabs();
        setupHomeLocation();
        setupSingleForm();
        setupBulkForm();
        setupSubscribeForm();
        setupWatchesDashboard();
        setupBulkEdit();
        setupSearchScroll();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", bootAll);
    } else {
        bootAll();
    }
})();
