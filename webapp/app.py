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

    return jsonify({
        "ok": True,
        "created": created,
        "duplicate": duplicate,
        "subscribed_to": subscribed_to,
        "summary": " · ".join(summary_bits),
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
