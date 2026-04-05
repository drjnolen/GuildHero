"""
PostgreSQL-backed key-value store that provides the same interface as Replit DB.

Uses a single JSONB table to store key-value pairs, matching the Replit DB API:
  - db.get(key, default=None)
  - db[key] = value
  - del db[key]
  - key in db
  - db.prefix(prefix) -> list of matching keys
"""

import json
import logging
import os

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
"""


class PostgresKV:
    """Drop-in replacement for Replit DB backed by PostgreSQL."""

    def __init__(self, database_url: str):
        self._database_url = database_url
        self._conn = None
        self._ensure_table()

    # -- connection management --------------------------------------------------

    def _get_conn(self):
        """Return an open connection, reconnecting if necessary."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._database_url)
            self._conn.autocommit = True
        return self._conn

    def _ensure_table(self):
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(_CREATE_TABLE)

    # -- public API (mirrors Replit DB) -----------------------------------------

    def get(self, key: str, default=None):
        """Retrieve the value for *key*, or *default* if not found."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
            row = cur.fetchone()
        if row is None:
            return default
        return row[0]

    def __getitem__(self, key: str):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value):
        conn = self._get_conn()
        json_value = psycopg2.extras.Json(value)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kv_store (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                """,
                (key, json_value),
            )

    def __delitem__(self, key: str):
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))

    def __contains__(self, key: str) -> bool:
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM kv_store WHERE key = %s", (key,))
            return cur.fetchone() is not None

    def prefix(self, prefix: str) -> list:
        """Return all keys that start with *prefix*."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT key FROM kv_store WHERE key LIKE %s ORDER BY key",
                (prefix + "%",),
            )
            return [row[0] for row in cur.fetchall()]

    def keys(self):
        """Return all keys in the store."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM kv_store ORDER BY key")
            return [row[0] for row in cur.fetchall()]

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()


def _init_db() -> PostgresKV:
    """Initialise the database from the DATABASE_URL environment variable."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Set it to a PostgreSQL connection string, e.g. "
            "postgresql://user:password@host:port/dbname"
        )
    return PostgresKV(url)


# Module-level singleton so that ``from db import db`` works identically to
# ``from replit import db``.
db = _init_db()
