-- deal_finder Postgres schema.
--
-- Source-agnostic comps design: the `comps` table holds price observations
-- from any source (currently only 'marketplace'; 'ebay' will be added once
-- the developer account is approved). Each row is one observed asking
-- (or sold, for eBay) price for a normalized search term, cached for the
-- TTL window before being refetched.
--
-- Run via: psql -U postgres -d dealfinder_dev -f schema.sql
-- Or via Python: db.bootstrap.ensure_schema()

-- ---------------------------------------------------------------------
-- user_searches — saved keyword + location pairs the scheduler iterates
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_searches (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL DEFAULT 1,
    keyword         TEXT NOT NULL,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    radius_km       INTEGER NOT NULL DEFAULT 40,
    price_min       INTEGER,
    price_max       INTEGER,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------
-- listings — every Marketplace listing we've seen, with all pipeline state
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS listings (
    id                                  TEXT PRIMARY KEY,                 -- FB listing ID
    search_id                           INTEGER REFERENCES user_searches(id) ON DELETE SET NULL,
    title                               TEXT NOT NULL,
    price                               REAL,                             -- final price used for scoring
    raw_price                           REAL,                             -- original scraped value (audit)
    price_extracted_from_description    BOOLEAN NOT NULL DEFAULT FALSE,
    previous_price                      TEXT,                             -- "CA$290" — formatted, nullable
    is_pending                          BOOLEAN NOT NULL DEFAULT FALSE,
    photo_url                           TEXT,
    seller_name                         TEXT,
    seller_location                     TEXT,
    seller_type                         TEXT,
    description                         TEXT,
    listing_url                         TEXT,
    category_id                         TEXT,                             -- FB marketplace_listing_category_id
    listed_at                           TIMESTAMPTZ,                      -- when FB posted it
    scraped_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail_source                       TEXT,                             -- 'pdp' | 'html' | NULL

    -- Rejection filter outputs
    rejected                            BOOLEAN NOT NULL DEFAULT FALSE,
    rejection_reason                    TEXT,

    -- Appraisal outputs (filled by the worker)
    appraised                           BOOLEAN NOT NULL DEFAULT FALSE,
    deal_score                          INTEGER,
    fair_value                          REAL,
    appraisal_note                      TEXT,
    appraisal_model                     TEXT,
    appraised_at                        TIMESTAMPTZ,
    -- Full reproducible breakdown of how the score was derived. JSONB so
    -- it can hold formula version, comps_used, ratio, confidence etc.
    -- See appraisal/formula.py::ScoreBreakdown.
    appraisal_breakdown                 JSONB,

    -- Comp lookup outputs (cached on listing for fast read)
    comp_search_term                    TEXT,
    comp_source                         TEXT,
    comp_median                         REAL,
    comp_mean                           REAL,
    comp_min                            REAL,
    comp_max                            REAL,
    comp_sample_size                    INTEGER,
    comps_resolved_at                   TIMESTAMPTZ,

    -- Notification state
    notified                            BOOLEAN NOT NULL DEFAULT FALSE,
    notified_at                         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_listings_appraised  ON listings(appraised);
CREATE INDEX IF NOT EXISTS idx_listings_rejected   ON listings(rejected);
CREATE INDEX IF NOT EXISTS idx_listings_score      ON listings(deal_score DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_listings_scraped_at ON listings(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_notified   ON listings(notified);
CREATE INDEX IF NOT EXISTS idx_listings_search_id  ON listings(search_id);

-- ---------------------------------------------------------------------
-- comps — price observations for fair-value lookup
--   source = 'marketplace' (asking prices, current bridge)
--   source = 'ebay'        (sold prices, future)
-- One row per observed listing per fetch. Aggregated on read.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comps (
    id              BIGSERIAL PRIMARY KEY,
    search_term     TEXT NOT NULL,
    source          TEXT NOT NULL,
    price           REAL NOT NULL,
    title           TEXT,
    listing_url     TEXT,
    location        TEXT,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_comps_lookup
    ON comps(search_term, source, fetched_at DESC);

-- ---------------------------------------------------------------------
-- comps_meta — TTL bookkeeping. (search_term, source) -> last_fetched
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS comps_meta (
    search_term     TEXT NOT NULL,
    source          TEXT NOT NULL,
    last_fetched    TIMESTAMPTZ NOT NULL,
    sample_size     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (search_term, source)
);

-- ---------------------------------------------------------------------
-- subscribers — people who want to be notified about high-score deals
--   for a given saved search. Email is required; phone is optional.
--   Each row = (email, search_id). The same email can subscribe to many
--   searches; the digest worker groups all matches per email into one.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS subscribers (
    id                       SERIAL PRIMARY KEY,
    name                     TEXT,
    email                    TEXT NOT NULL,
    phone                    TEXT,
    search_id                INTEGER REFERENCES user_searches(id) ON DELETE CASCADE,
    score_threshold          INTEGER NOT NULL DEFAULT 70,
    daily_summary_enabled    BOOLEAN NOT NULL DEFAULT TRUE,   -- send 24h-rolling summary of below-threshold scored listings
    last_summary_sent_at     TIMESTAMPTZ,                     -- when we last sent the summary
    confirmed                BOOLEAN NOT NULL DEFAULT FALSE,  -- email-verify hook for later
    active                   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (email, search_id)
);
CREATE INDEX IF NOT EXISTS idx_subscribers_search ON subscribers(search_id);

-- ---------------------------------------------------------------------
-- user_settings — per-user prefs. For a single-user system this is
-- effectively one row, but the user_id column lets us add multi-user
-- later without schema change. Currently holds the home location used
-- as the default centre for new saved searches.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_settings (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL DEFAULT 1,
    home_label      TEXT,                     -- e.g. "Vancouver, BC"
    home_latitude   REAL,
    home_longitude  REAL,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id)
);

-- ---------------------------------------------------------------------
-- listings.summarized_at — set by the daily-summary job when a listing
-- is included in a 24h roundup so we don't re-include it tomorrow.
-- ---------------------------------------------------------------------
ALTER TABLE listings ADD COLUMN IF NOT EXISTS summarized_at TIMESTAMPTZ;

-- ---------------------------------------------------------------------
-- subscribers backfill — these were added to the CREATE TABLE above
-- after some users already had subscribers tables. ALTER ... IF NOT
-- EXISTS makes the migration idempotent.
-- ---------------------------------------------------------------------
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS daily_summary_enabled BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS last_summary_sent_at  TIMESTAMPTZ;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS last_digest_sent_at   TIMESTAMPTZ;
ALTER TABLE subscribers ADD COLUMN IF NOT EXISTS confirmation_sent_at  TIMESTAMPTZ;

-- ---------------------------------------------------------------------
-- scheduler_events — observability log for the dashboard.
-- One row per significant scheduler event so we can render aggregates
-- (polls/hr, rate-limit count today, score histogram) and an event tail
-- without parsing stdout. See db/events.py for the writer helper.
--
-- event_type values currently emitted:
--   poll              - one poll_search() cycle (raw_count, new_count, etc)
--   fb_rate_limit     - Marketplace returned a 429-ish error
--   email_sent        - successful send via console/smtp/resend
--   email_failed      - send failed (auth, network, rate limit)
--   safety_drain      - safety-net appraisal pass result
--   reload            - reload_searches() added/removed jobs
--   scheduler_boot    - run_forever() started
--   pipeline_error    - any uncaught exception in _process_new_listing
--
-- detail is opaque JSONB for flexibility; the dashboard knows which
-- fields to expect per event_type.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduler_events (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    search_id   INTEGER,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    duration_ms INTEGER,
    detail      JSONB
);
CREATE INDEX IF NOT EXISTS idx_events_created_at  ON scheduler_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type_time   ON scheduler_events (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_search_time ON scheduler_events (search_id, created_at DESC);
