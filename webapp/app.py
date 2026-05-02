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

import sys
import time
from dataclasses import asdict
from pathlib import Path

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

    # Pull any existing appraisal for this listing from Postgres.
    base["deal_score"] = None
    base["fair_value"] = None
    base["appraisal_note"] = None
    base["comp_median"] = None
    base["comp_sample_size"] = None
    base["comp_search_term"] = None
    base["comp_source"] = None
    base["appraisal_breakdown"] = None
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT deal_score, fair_value, appraisal_note,
                              comp_median, comp_sample_size,
                              comp_search_term, comp_source,
                              appraisal_breakdown
                       FROM listings WHERE id = %s AND appraised = TRUE""",
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
                    base["appraisal_breakdown"] = row[7]  # already a dict from JSONB
    except Exception:  # noqa: BLE001
        # DB might be down; the rest of the UI should still render.
        pass

    return base


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
        error=None,
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
        comp = get_comps(
            search_term=search_term,
            lat=49.2827, lng=-123.1207, radius_km=1500,
            exclude_listing_id=listing_id,
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

    # 6) Compute deterministic score
    try:
        breakdown = compute_score(
            asking_price=pl.resolved_price,
            comp=comp,
            fair_value_from_llm=(
                llm_estimate.fair_value if llm_estimate else None
            ),
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
