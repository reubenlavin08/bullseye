"""Listings table data-access layer.

Persists the pipeline's `ProcessedListing` rows into Postgres, supports
fast dedup checks ("have we seen this ID?"), and provides the queries
the appraisal worker + alert layer will use.

All functions take an explicit psycopg2 connection so callers can run
multiple ops in one transaction. Convenience wrappers that open their
own connection live at the bottom of the module.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable

import psycopg2.extras

from ..scraper.pipeline import ProcessedListing
from .connection import get_conn

logger = logging.getLogger(__name__)


# --- Existence checks -----------------------------------------------------

def existing_ids(conn, candidate_ids: Iterable[str]) -> set[str]:
    """Return the subset of `candidate_ids` already present in `listings`.

    Used by the pipeline before doing any expensive per-listing work
    (description fetch, comp lookup, LLM scoring). One query, indexed
    primary-key lookup, fast even at 100k+ rows.
    """
    ids = list({str(i) for i in candidate_ids if i})
    if not ids:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM listings WHERE id = ANY(%s)", (ids,),
        )
        return {row[0] for row in cur.fetchall()}


# --- Inserts / upserts ----------------------------------------------------

# Columns we write on insert. Anything appraisal/comp/notification-related
# is filled in later by other workers, so we let those default to NULL.
_INSERT_COLS = (
    "id", "search_id", "title", "price", "raw_price",
    "price_extracted_from_description", "previous_price", "is_pending",
    "photo_url", "seller_name", "seller_location", "seller_type",
    "description", "listing_url", "listed_at", "scraped_at",
    "detail_source", "rejected", "rejection_reason",
)


def upsert_processed(
    conn,
    pl: ProcessedListing,
    *,
    search_id: int | None = None,
) -> bool:
    """Insert a ProcessedListing row, or update it if the ID already
    exists. Returns True if this was a new insert, False if it was an
    update (caller can use the bool to decide whether to enqueue
    appraisal).

    Uses `INSERT ... ON CONFLICT (id) DO UPDATE` so we never lose history
    on a rescrape.
    """
    listed_at = (
        datetime.fromtimestamp(pl.listed_at_unix, tz=timezone.utc)
        if pl.listed_at_unix else None
    )
    values = {
        "id": pl.id,
        "search_id": search_id,
        "title": pl.title,
        "price": pl.resolved_price,
        "raw_price": pl.raw_price,
        "price_extracted_from_description": pl.price_extracted_from_description,
        "previous_price": pl.previous_price,
        "is_pending": pl.is_pending,
        "photo_url": pl.photo_url,
        "seller_name": pl.seller_name,
        "seller_location": pl.seller_location,
        "seller_type": pl.seller_type,
        "description": pl.description,
        "listing_url": pl.listing_url,
        "listed_at": listed_at,
        "scraped_at": datetime.now(timezone.utc),
        "detail_source": pl.detail_source,
        "rejected": pl.rejected,
        "rejection_reason": pl.rejection_reason,
    }

    cols = ", ".join(_INSERT_COLS)
    placeholders = ", ".join(f"%({c})s" for c in _INSERT_COLS)
    # Only update fields that can change between scrapes. Don't touch
    # appraisal/comp/notification fields — those are owned by other
    # workers.
    update_cols = [
        c for c in _INSERT_COLS
        if c not in ("id", "search_id", "scraped_at")
    ]
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    sql = (
        f"INSERT INTO listings ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (id) DO UPDATE SET {update_set}, "
        f"scraped_at = EXCLUDED.scraped_at "
        f"RETURNING (xmax = 0) AS inserted"
    )

    with conn.cursor() as cur:
        cur.execute(sql, values)
        row = cur.fetchone()
        was_insert = bool(row[0]) if row else False
    return was_insert


def upsert_many(
    conn,
    listings: Iterable[ProcessedListing],
    *,
    search_id: int | None = None,
) -> tuple[int, int]:
    """Upsert a batch. Returns (n_inserted, n_updated)."""
    inserted = 0
    updated = 0
    for pl in listings:
        if upsert_processed(conn, pl, search_id=search_id):
            inserted += 1
        else:
            updated += 1
    return inserted, updated


# --- Read queries (used by appraisal + alert workers) --------------------

def fetch_unappraised(conn, limit: int = 50) -> list[psycopg2.extras.DictRow]:
    """Pull the next batch of listings the LLM should score.

    Excludes rejected rows (those skip appraisal entirely) and rows that
    have already been appraised. Oldest-first so backlog drains in order.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """SELECT * FROM listings
               WHERE appraised = FALSE AND rejected = FALSE
               ORDER BY scraped_at ASC
               LIMIT %s""",
            (limit,),
        )
        return list(cur.fetchall())


def fetch_alertable(
    conn, *, score_threshold: int = 70, limit: int = 25,
) -> list[psycopg2.extras.DictRow]:
    """High-score listings the alert layer hasn't notified on yet."""
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            """SELECT * FROM listings
               WHERE appraised = TRUE
                 AND rejected = FALSE
                 AND notified = FALSE
                 AND deal_score >= %s
               ORDER BY deal_score DESC
               LIMIT %s""",
            (score_threshold, limit),
        )
        return list(cur.fetchall())


# --- Write-back from later phases ----------------------------------------

def update_appraisal(
    conn,
    listing_id: str,
    *,
    deal_score: int,
    fair_value: float | None,
    appraisal_note: str | None,
    appraisal_model: str | None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE listings SET
                  deal_score = %s,
                  fair_value = %s,
                  appraisal_note = %s,
                  appraisal_model = %s,
                  appraised = TRUE,
                  appraised_at = NOW()
               WHERE id = %s""",
            (deal_score, fair_value, appraisal_note, appraisal_model, listing_id),
        )


def update_comps_resolution(
    conn,
    listing_id: str,
    *,
    search_term: str,
    source: str,
    median: float | None,
    mean: float | None,
    minimum: float | None,
    maximum: float | None,
    sample_size: int,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE listings SET
                  comp_search_term = %s,
                  comp_source = %s,
                  comp_median = %s,
                  comp_mean = %s,
                  comp_min = %s,
                  comp_max = %s,
                  comp_sample_size = %s,
                  comps_resolved_at = NOW()
               WHERE id = %s""",
            (search_term, source, median, mean, minimum, maximum,
             sample_size, listing_id),
        )


def mark_notified(conn, listing_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE listings SET
                  notified = TRUE,
                  notified_at = NOW()
               WHERE id = %s""",
            (listing_id,),
        )


# --- Convenience (own-connection) wrappers --------------------------------

def filter_new_ids(candidate_ids: Iterable[str]) -> list[str]:
    """Return only IDs not yet in the listings table. Opens its own conn."""
    candidates = [str(i) for i in candidate_ids if i]
    if not candidates:
        return []
    with get_conn() as conn:
        seen = existing_ids(conn, candidates)
    return [i for i in candidates if i not in seen]
