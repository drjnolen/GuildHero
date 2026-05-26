"""
PostgreSQL-backed storage for GuildHero data.

Provides a key-value API for existing bot state plus normalized tables for
messages and enrolled chats.
"""

import logging
import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)

_CREATE_KV_TABLE = """
CREATE TABLE IF NOT EXISTS kv_store (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL
);
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    text TEXT NOT NULL,
    sent_at TIMESTAMPTZ NOT NULL,
    is_reply BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (chat_id, message_id)
);
"""

_CREATE_MESSAGES_INDEX = """
CREATE INDEX IF NOT EXISTS idx_messages_chat_sent_at
    ON messages (chat_id, sent_at DESC, message_id DESC);
"""

_CREATE_ENROLLED_CHATS_TABLE = """
CREATE TABLE IF NOT EXISTS enrolled_chats (
    chat_id BIGINT PRIMARY KEY,
    enrolled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class PostgresKV:
    """Drop-in replacement for Replit DB backed by PostgreSQL."""

    def __init__(self, database_url: str):
        self._database_url = database_url
        self._conn = None
        self._ensure_schema()

    def _get_conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._database_url)
            self._conn.autocommit = True
        return self._conn

    def _execute_with_retry(self, fn):
        """Execute *fn(conn)* with one automatic reconnect on OperationalError.

        Handles transient connection drops (e.g., database restarts on Railway)
        without crashing the bot.
        """
        try:
            return fn(self._get_conn())
        except psycopg2.OperationalError as exc:
            logger.warning("Database connection lost (%s); reconnecting...", exc)
            self._conn = None
            return fn(self._get_conn())

    def _ensure_schema(self):
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(_CREATE_KV_TABLE)
                cur.execute(_CREATE_MESSAGES_TABLE)
                cur.execute(_CREATE_MESSAGES_INDEX)
                cur.execute(_CREATE_ENROLLED_CHATS_TABLE)
        self._execute_with_retry(_op)

    def get(self, key: str, default=None):
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM kv_store WHERE key = %s", (key,))
                row = cur.fetchone()
            return default if row is None else row[0]
        return self._execute_with_retry(_op)

    def __getitem__(self, key: str):
        value = self.get(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value):
        json_value = psycopg2.extras.Json(value)
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO kv_store (key, value)
                    VALUES (%s, %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    """,
                    (key, json_value),
                )
        self._execute_with_retry(_op)

    def __delitem__(self, key: str):
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM kv_store WHERE key = %s", (key,))
        self._execute_with_retry(_op)

    def __contains__(self, key: str) -> bool:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM kv_store WHERE key = %s", (key,))
                return cur.fetchone() is not None
        return self._execute_with_retry(_op)

    def prefix(self, prefix: str) -> list:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT key FROM kv_store WHERE key LIKE %s ORDER BY key",
                    (prefix + "%",),
                )
                return [row[0] for row in cur.fetchall()]
        return self._execute_with_retry(_op)

    def keys(self):
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT key FROM kv_store ORDER BY key")
                return [row[0] for row in cur.fetchall()]
        return self._execute_with_retry(_op)

    def enroll_chat(self, chat_id: int):
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO enrolled_chats (chat_id) VALUES (%s) ON CONFLICT (chat_id) DO NOTHING",
                    (chat_id,),
                )
        self._execute_with_retry(_op)

    def get_enrolled_chat_ids(self) -> list[int]:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT chat_id FROM enrolled_chats ORDER BY chat_id")
                return [row[0] for row in cur.fetchall()]
        return self._execute_with_retry(_op)

    def has_messages(self, chat_id: int) -> bool:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM messages WHERE chat_id = %s LIMIT 1", (chat_id,))
                return cur.fetchone() is not None
        return self._execute_with_retry(_op)

    def add_message(self, chat_id: int, message_id: int, user_id: int, username: str | None, text: str, sent_at: datetime, is_reply: bool):
        """Persist one chat message, auto-enrolling the chat and normalizing naive datetimes to UTC.

        Duplicate `(chat_id, message_id)` inserts are ignored by the database.
        """
        self.enroll_chat(chat_id)
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO messages (chat_id, message_id, user_id, username, text, sent_at, is_reply)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_id, message_id) DO NOTHING
                    """,
                    (chat_id, message_id, user_id, username, text, sent_at, is_reply),
                )
        self._execute_with_retry(_op)

    def migrate_legacy_messages(self, chat_id: int, messages: list[dict]):
        if not messages:
            return
        self.enroll_chat(chat_id)
        rows = []
        for index, msg in enumerate(messages, start=1):
            date_value = msg.get("date")
            if not date_value:
                continue
            try:
                sent_at = datetime.fromisoformat(date_value)
            except ValueError:
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            rows.append(
                (
                    int(chat_id),
                    int(msg.get("message_id") or -index),
                    int(msg.get("user_id") or 0),
                    msg.get("username"),
                    msg.get("text") or "",
                    sent_at,
                    bool(msg.get("is_reply", False)),
                )
            )
        if not rows:
            return
        def _op(conn):
            with conn.cursor() as cur:
                execute_values(
                    cur,
                    """
                    INSERT INTO messages (chat_id, message_id, user_id, username, text, sent_at, is_reply)
                    VALUES %s
                    ON CONFLICT (chat_id, message_id) DO NOTHING
                    """,
                    rows,
                )
        self._execute_with_retry(_op)

    def get_messages(self, chat_id: int, start_date: datetime | None = None, end_date: datetime | None = None) -> list[dict]:
        clauses = ["chat_id = %s"]
        params: list[object] = [chat_id]
        if start_date is not None:
            if start_date.tzinfo is None:
                start_date = start_date.replace(tzinfo=timezone.utc)
            clauses.append("sent_at >= %s")
            params.append(start_date)
        if end_date is not None:
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            clauses.append("sent_at <= %s")
            params.append(end_date)
        query = f"""
            SELECT user_id, username, text, sent_at, is_reply, message_id
            FROM messages
            WHERE {' AND '.join(clauses)}
            ORDER BY sent_at ASC, message_id ASC
        """
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            return [
                {
                    "user_id": row[0],
                    "username": row[1],
                    "text": row[2],
                    "date": row[3].astimezone(timezone.utc).isoformat(),
                    "is_reply": row[4],
                    "message_id": row[5],
                }
                for row in rows
            ]
        return self._execute_with_retry(_op)

    def get_recent_messages(self, chat_id: int, limit: int) -> list[dict]:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, username, text, sent_at, is_reply, message_id
                    FROM messages
                    WHERE chat_id = %s
                    ORDER BY sent_at DESC, message_id DESC
                    LIMIT %s
                    """,
                    (chat_id, limit),
                )
                rows = cur.fetchall()
            rows.reverse()
            return [
                {
                    "user_id": row[0],
                    "username": row[1],
                    "text": row[2],
                    "date": row[3].astimezone(timezone.utc).isoformat(),
                    "is_reply": row[4],
                    "message_id": row[5],
                }
                for row in rows
            ]
        return self._execute_with_retry(_op)

    def get_recent_user_messages(self, chat_id: int, user_id: int, limit: int) -> list[dict]:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, username, text, sent_at, is_reply, message_id
                    FROM messages
                    WHERE chat_id = %s AND user_id = %s
                    ORDER BY sent_at DESC, message_id DESC
                    LIMIT %s
                    """,
                    (chat_id, user_id, limit),
                )
                rows = cur.fetchall()
            rows.reverse()
            return [
                {
                    "user_id": row[0],
                    "username": row[1],
                    "text": row[2],
                    "date": row[3].astimezone(timezone.utc).isoformat(),
                    "is_reply": row[4],
                    "message_id": row[5],
                }
                for row in rows
            ]
        return self._execute_with_retry(_op)

    def get_top_message_counts(self, chat_id: int, limit: int) -> list[tuple[int, str | None, int]]:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id, COALESCE(MAX(username), ''), COUNT(*) AS message_count
                    FROM messages
                    WHERE chat_id = %s
                    GROUP BY user_id
                    ORDER BY message_count DESC, user_id ASC
                    LIMIT %s
                    """,
                    (chat_id, limit),
                )
                return cur.fetchall()
        return self._execute_with_retry(_op)

    def get_user_rank(self, chat_id: int, user_id: int) -> tuple[int, int] | None:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH ranked AS (
                        SELECT user_id,
                               COUNT(*) AS message_count,
                               DENSE_RANK() OVER (ORDER BY COUNT(*) DESC) AS rank
                        FROM messages
                        WHERE chat_id = %s
                        GROUP BY user_id
                    )
                    SELECT rank, message_count FROM ranked WHERE user_id = %s
                    """,
                    (chat_id, user_id),
                )
                return cur.fetchone()
        return self._execute_with_retry(_op)

    def get_chat_stats(self, chat_id: int, top_n: int = 5) -> dict:
        def _op(conn):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(sent_at), MAX(sent_at)
                    FROM messages
                    WHERE chat_id = %s
                    """,
                    (chat_id,),
                )
                total_messages, user_count, oldest_date, newest_date = cur.fetchone()
                if not total_messages:
                    return {
                        "total_messages": 0,
                        "user_count": 0,
                        "oldest_date": None,
                        "newest_date": None,
                        "top_users": [],
                    }
                cur.execute(
                    """
                    SELECT COALESCE(MAX(username), ''), COUNT(*) AS message_count
                    FROM messages
                    WHERE chat_id = %s
                    GROUP BY user_id
                    ORDER BY message_count DESC, user_id ASC
                    LIMIT %s
                    """,
                    (chat_id, top_n),
                )
                top_users = cur.fetchall()
            return {
                "total_messages": total_messages,
                "user_count": user_count,
                "oldest_date": oldest_date,
                "newest_date": newest_date,
                "top_users": top_users,
            }
        return self._execute_with_retry(_op)

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()


def _init_db() -> PostgresKV:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Set it to a PostgreSQL connection string, e.g. "
            "postgresql://user:password@host:port/dbname"
        )
    return PostgresKV(url)


db = _init_db()
