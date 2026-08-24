"""Unit tests for the PostgreSQL message hot path without a live database."""

import datetime
import importlib.util
import os
import sys
import threading
import unittest
from pathlib import Path
from types import ModuleType

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "CityLedger" / "db.py"
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")


class _FakeCursor:
    def __init__(self, fetchone_result=None):
        self.executions = []
        self.fetchone_result = fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.executions.append((query, params))

    def fetchone(self):
        return self.fetchone_result


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return self._cursor


_saved_psycopg_modules = {
    name: sys.modules.get(name)
    for name in ("psycopg2", "psycopg2.extras")
}
_schema_cursor = _FakeCursor()
_fake_psycopg = ModuleType("psycopg2")
_fake_extras = ModuleType("psycopg2.extras")
_fake_extras.Json = lambda value: value
_fake_extras.execute_values = lambda *_args, **_kwargs: None
_fake_psycopg.extras = _fake_extras
_fake_psycopg.OperationalError = RuntimeError
_fake_psycopg.connect = lambda _url: _FakeConnection(_schema_cursor)
sys.modules["psycopg2"] = _fake_psycopg
sys.modules["psycopg2.extras"] = _fake_extras

_spec = importlib.util.spec_from_file_location("_guildhero_db_target", DB_PATH)
db_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(db_module)

for _name, _module in _saved_psycopg_modules.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


def _storage_with_cursor(cursor):
    storage = db_module.PostgresKV.__new__(db_module.PostgresKV)
    storage._database_url = "postgresql://localhost/test"
    storage._conn = None
    storage._enrolled_chat_ids = set()
    storage._enrolled_chat_lock = threading.Lock()
    connection = _FakeConnection(cursor)
    storage._execute_with_retry = lambda operation: operation(connection)
    return storage


class TestPostgresMessageEfficiency(unittest.TestCase):
    def test_record_message_uses_one_atomic_statement(self):
        cursor = _FakeCursor(
            fetchone_result=(
                True,
                {
                    "message_count": 2,
                    "first_seen": "2026-08-01T00:00:00+00:00",
                },
                True,
                ["contributor_100"],
            )
        )
        storage = _storage_with_cursor(cursor)

        result = storage.record_message(
            42,
            99,
            7,
            "alice",
            "Alice",
            None,
            "hello",
            datetime.datetime(2026, 8, 24, tzinfo=datetime.timezone.utc),
            False,
            user_key="user:42:7",
            stats_key="user_stats:42:7",
            achievements_key="achievements_enabled:42",
            badges_key="badges:42:7",
        )

        self.assertEqual(len(cursor.executions), 1)
        query, _params = cursor.executions[0]
        self.assertIn("inserted_message AS", query)
        self.assertIn("updated_stats AS", query)
        self.assertTrue(result["inserted"])
        self.assertEqual(result["user_stats"]["message_count"], 2)
        self.assertTrue(result["achievements_enabled"])
        self.assertEqual(result["badges"], ["contributor_100"])

    def test_enrolled_chat_cache_avoids_duplicate_insert(self):
        cursor = _FakeCursor()
        storage = _storage_with_cursor(cursor)

        storage.enroll_chat(42)
        storage.enroll_chat(42)

        self.assertEqual(len(cursor.executions), 1)


if __name__ == "__main__":
    unittest.main()
