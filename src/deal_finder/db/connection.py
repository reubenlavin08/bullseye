"""Postgres connection helper.

Reads DATABASE_URL from environment (.env file in dev), exposes a single
psycopg2 connection-pool-ish helper. We don't use a true pool since the
pipeline is single-process and low-concurrency; one shared connection
with `with get_conn() as conn` is enough for now.

Usage:
    from deal_finder.db.connection import get_conn
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        print(cur.fetchone())
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

DEFAULT_DSN = "postgresql://postgres:1@localhost:5432/dealfinder_dev"


def _load_dotenv_into_os() -> None:
    """Cheap .env loader. We don't pull python-dotenv in just for this —
    the pipeline reads only DATABASE_URL and a few other simple keys."""
    repo_root = Path(__file__).resolve().parents[3]
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv_into_os()


def get_dsn() -> str:
    return os.environ.get("DB_URL") or os.environ.get("DATABASE_URL") or DEFAULT_DSN


@contextmanager
def get_conn() -> Iterator[psycopg2.extensions.connection]:
    """Yield a Postgres connection. Closed on exit; commit/rollback up to
    the caller via `with conn:` if they want transactional semantics."""
    conn = psycopg2.connect(get_dsn())
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_dict_cursor() -> Iterator[psycopg2.extras.DictCursor]:
    """Convenience: yield a dict cursor inside its own connection."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            yield cur
        conn.commit()


def ensure_schema() -> None:
    """Run schema.sql against the DB. Idempotent — uses CREATE IF NOT EXISTS."""
    schema_path = Path(__file__).parent / "schema.sql"
    sql = schema_path.read_text(encoding="utf-8")
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    logger.info("schema ensured at %s", get_dsn())
