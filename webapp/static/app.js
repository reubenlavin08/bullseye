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
                const score = data.deal_score;
                const klass = score >= 70 ? "score-high"
                            : score >= 50 ? "score-mid" : "score-low";
                const ratio = data.ratio || 0;
                const asking = data.breakdown && data.breakdown.asking_price;
                const fair = data.fair_value;
                result.innerHTML =
                    '<div class="appraisal-display ' + klass + '">' +
                    '<div class="score-row">' +
                        '<span class="score-num">' + score + '</span>' +
                        (data.confidence_pm ? '<span class="score-pm">±' + data.confidence_pm + '</span>' : '') +
                        '<span class="score-label">deal score</span>' +
                        (fair ? '<span class="score-fair">fair: $' + Math.round(fair) + '</span>' : '') +
                    '</div>' +
                    (asking && fair ?
                        '<div class="score-math">' +
                            '<span class="math-eq">$' + Math.round(asking) + ' ÷ $' + Math.round(fair) + ' = ratio <strong>' + ratio.toFixed(2) + '</strong></span>' +
                            '<span class="math-source muted">' + escapeHtml(data.fair_value_source || "") + '</span>' +
                        '</div>'
                    : '') +
                    (data.note ? '<div class="score-note">' + escapeHtml(data.note) + '</div>' : '') +
                    '<div class="score-comps muted">' +
                        (data.comp_sample_size || 0) + ' comp(s) for "' +
                        escapeHtml(data.search_term || "") + '"' +
                        (data.outliers_dropped ? ", " + data.outliers_dropped + " outlier(s) dropped" : "") +
                        (data.comp_median ? ' · raw median $' + Math.round(data.comp_median) : '') +
                        (data.elapsed_s ? ' · LLM ' + data.elapsed_s.toFixed(1) + 's' : ' · formula-only') +
                    '</div></div>';
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

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
