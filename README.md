# bullseye

Save a list of what you want on Facebook Marketplace. The system polls
your watches continuously, scores every new listing against real comp
data, and emails you the moment something beats your bar.

Statistical, deterministic, condition-aware. Local-first.

---

## What it does

- **You save watches** in a web UI: keyword, distance from home,
  price range, score threshold for alerts, your email.
- **A scheduler polls Marketplace** in the background, round-robin
  across all your watches, paced under FB's rate limit.
- **Every new listing is scored statistically** — its asking price's
  percentile rank against real comps, capped by a confidence interval
  derived from sample size + comp dispersion + outlier rate.
- **Condition signals** (regex bank ∪ small LLM) detect "needs repair",
  "salvage title", "excellent condition", etc. and adjust the score.
- **An email lands in your inbox** the moment a listing scores above
  your threshold. A daily roundup covers below-threshold scored items.
- **A live dashboard** at `/dashboard` shows the scheduler, FB rate
  limits, pipeline funnel, per-watch performance, score histogram,
  and a 2-second-refresh event tail.

It's deal-finding by way of percentile rank, not vibes.

## Aesthetic

Café-portfolio: warm cream paper (`#f3ead3`), oxblood ink (`#3f1718`),
burnt-orange accents. Heavy slab logo (Alfa Slab One), monospace body
(IBM Plex Mono), slim italic Fraunces for poetic moments. Dashed
borders, dotted dividers, receipt-style stamps.

## Stack

- **Python 3.12** + Flask (webapp), APScheduler (background poller)
- **PostgreSQL 18** for listings, watches, subscribers, observability events
- **Ollama** locally for title normalization, condition extraction,
  embedding-based comp filtering (`llama3.2:3b-instruct-q4_K_M`,
  `nomic-embed-text`). Designed for a 4 GB GPU.
- **Resend** (or Gmail SMTP, or console DRY-RUN) for email delivery
- **Nominatim / OpenStreetMap** for address autocomplete + city geocoding

No cloud services required at runtime. Runs entirely on a laptop.

## Quick start

Prerequisites:
- Python 3.12+, PostgreSQL 18 (use the EDB installer on Windows)
- [Ollama](https://ollama.com/) installed locally with:
  ```
  ollama pull llama3.2:3b-instruct-q4_K_M
  ollama pull nomic-embed-text
  ```
- A Resend account (free tier 100 emails/day) — or just use the
  console backend for development.

Install:
```bash
python -m venv .venv
.venv\Scripts\activate                  # Windows; use 'source .venv/bin/activate' on macOS/Linux
pip install -e .[dev]
copy .env.example .env                  # then edit it (see below)
```

Configure `.env`:
```
DB_URL=postgresql://postgres:1@localhost:5432/dealfinder_dev
ALERT_BACKEND=resend                    # or 'smtp' or 'console'
RESEND_API_KEY=re_xxx                   # get one at resend.com/api-keys
ALERT_FROM_EMAIL=onboarding@resend.dev
```

Initialize the database:
```bash
.venv\Scripts\python.exe -c "from deal_finder.db.connection import get_conn; \
  sql = open('src/deal_finder/db/schema.sql').read(); \
  conn = next(iter([get_conn().__enter__()])); \
  cur = conn.cursor(); cur.execute(sql); conn.commit(); print('OK')"
```

Run two processes in separate terminals:

```bash
# Terminal 1 — webapp
.venv\Scripts\python.exe -m webapp.app
# now visit http://127.0.0.1:5000
```

```bash
# Terminal 2 — scheduler
.venv\Scripts\python.exe -m deal_finder.scheduler.main
# polls your watches and emails you on score >= threshold
```

## Using the site

- **Front page (`/`)** — hero, save a watch (single or bulk list),
  test the appraiser on any keyword.
- **Save a watch** — type an address (autocomplete from
  OpenStreetMap), pick distance + price range + score threshold +
  email. Get a confirmation email immediately.
- **Manage tab** (right side panel) — list every watch, edit
  thresholds inline, pause / resume, delete. Live activity stats.
- **Dashboard (`/dashboard`)** — observability page. Scheduler
  alive/offline, polls per hour, FB rate-limit count, today's pipeline
  funnel (scraped → rejected → appraised → emailed), live event tail
  (2s refresh), per-watch performance table, score histogram.

## Architecture

```
[ webapp  /api routes  ]   [ APScheduler                      ]
        |                          |
        |     coordinator_tick()   v
        |          one watch picked per 15s, stalest first
        |                          |
        |             [ FB Marketplace GraphQL search ]
        |                          |
        |             [ keyword filter (must-include / must-exclude) ]
        |                          |
        |             [ distance filter (city geocache + haversine) ]
        |                          |
        |             [ FB PDP detail fetch per new listing ]
        |                          |
        |             [ rejection filter (regex + keyword config) ]
        |                          |
        |             [ comp lookup (cached) + condition signals ]
        |                          |
        |             [ statistical scoring formula ]
        |                          |
        v                          v
[ Postgres: listings + scheduler_events + comps + subscribers + ... ]
        |
        v
[ digest worker every 15s -> email backend (Resend/SMTP/console) ]
```

Key points:
- **Round-robin coordinator** — one job picks the stalest watch every
  15s, polls it, exits. Scales gracefully with N watches and never
  bursts.
- **Adaptive backoff** — when FB rate-limits, the coordinator skips
  ticks until the count subsides.
- **Statistical scoring** — score = `(1 − percentile_rank) × 100`,
  capped by a confidence interval. Tukey-fence outlier trimming,
  category-aware confidence floors (vehicles get min ±20),
  data-quality flag when IQR exceeds median, bimodal cluster split
  for heterogeneous comps, plus an outlier-rate penalty for
  distributions with too-fat tails.
- **Observability** — every poll, rate-limit, email send, error gets
  recorded as a row in `scheduler_events` with JSONB detail. Dashboard
  reads aggregates from there + tails `logs/scheduler.log` for raw
  output.

## Status

Working today:
- ✅ Scrape Marketplace (search + PDP detail with description recovery)
- ✅ Reject services / scams / rentals / "make-me-an-offer" / trade-ins
- ✅ Recover real prices from $0/$1 placeholder listings
- ✅ Statistical scoring with confidence intervals
- ✅ Hybrid regex + LLM condition signals (10 flags, point adjustments
  clamped to [−35, +10])
- ✅ Marketplace comp lookup with 12h TTL cache
- ✅ Bimodal cluster split for heterogeneous comp distributions
- ✅ Round-robin scheduler with adaptive rate-limit backoff
- ✅ Email alerts (instant) + daily summary (rolling 24h roundup)
- ✅ Three email backends: Resend, SMTP (Gmail), console
- ✅ Per-watch dashboard: list, edit, pause, resume, delete
- ✅ Live observability dashboard at `/dashboard`
- ✅ Address autocomplete via OpenStreetMap
- ✅ Client-side distance filtering (FB ignores `filter_radius_km`)
- ✅ Per-watch keyword must-include / must-exclude (backend; UI in progress)

In progress:
- 🔄 Multi-keyword grouping (one FB poll covers many related watches)
- 🔄 UI controls for keyword must-include/must-exclude

Roadmap:
- LLM-suggested offer price ("you should offer $X")
- Swap Marketplace comps → eBay sold comps via the official Finding API
  (5,000 req/day proper rate limits — ground truth instead of asking-prices)
- Per-watch dashboard drill-down (recent matches per watch)
- Price-drop notifications on already-seen listings

## Tests

143 tests covering scoring formula, condition signals, rejection
patterns, bimodal cluster splitter, score breakdown contract.

```bash
.venv\Scripts\python.exe -m pytest tests/ -v
```

## License

Personal project. Use however you want.

---

Built by Reuben Lavin · [github.com/reubenlavin08/bullseye](https://github.com/reubenlavin08/bullseye)
