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
