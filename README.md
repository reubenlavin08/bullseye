# deal_finder

Pipeline that scrapes Facebook Marketplace, deduplicates listings into Postgres,
enriches each with eBay sold-price comps, scores deals with a local Ollama LLM,
and pushes high-score listings to Discord.

## Status

Phase 1 (FB scraper spike) — in progress. Nothing downstream is wired yet.

## Prerequisites

- Python 3.11+
- PostgreSQL 16 (install directly, no Docker — EDB installer on Windows)
- Ollama with `qwen2.5:7b-instruct-q4_K_M` and `llama3.2:3b-instruct-q4_K_M`
- eBay Developer App ID
- Discord webhook URL

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate    # Windows
pip install -e .[dev]
copy .env.example .env    # then fill in values
```

## Build order

See `BUILD.md` (architecture doc). Short version:

1. Scraper spike (GO/NO-GO)
2. DB + dedup
3. Price extraction + rejection
4. eBay client + cache (GO/NO-GO)
5. Title normalizer + appraisal worker
6. Discord alerts
7. Scheduler
