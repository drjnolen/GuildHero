"""Unit tests for pure utility functions in bot.py.

bot.py has several external dependencies (psycopg2, openai, nacl, telegram).
We mock them all at the sys.modules level before importing so that only the
pure helper functions are exercised – no real network or database calls are made.
"""

import datetime
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "CityLedger"

# Provide a dummy DATABASE_URL so the db module doesn't raise on import.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/test")

# ── Inject mocks for every module that has external dependencies or side
#    effects (database, network, Telegram API, crypto libraries) before any
#    local module is imported.
for _mod in (
    "psycopg2",
    "psycopg2.extras",
    "openai",
    "nacl",
    "nacl.signing",
    "nacl.secret",
    "pytz",
    "telegram",
    "telegram.constants",
    "telegram.ext",
    "telegram.request",
):
    sys.modules.setdefault(_mod, MagicMock())

# Mock the heavy bot sub-modules so that importing bot.py does not trigger any
# real side effects (DB schema creation, HTTP clients, etc.).
sys.modules["db"] = MagicMock()
sys.modules["ai_services"] = MagicMock()
sys.modules["http_clients"] = MagicMock()
sys.modules["raffle_utils"] = MagicMock()
sys.modules["sui_utils"] = MagicMock()

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import bot  # noqa: E402  (must come after sys.modules setup)

# After bot.py is imported, remove the mocks for modules that have their own
# test files (or that those test files depend on) so that alphabetically-later
# test runners see the real modules instead of MagicMocks.
for _mod_name in ("sui_utils", "raffle_utils", "nacl", "nacl.signing", "nacl.secret"):
    sys.modules.pop(_mod_name, None)

from bot import (  # noqa: E402
    _buybot_checkpoint_batches,
    _initialize_buybot_start_checkpoints,
    format_large_number,
    format_detailed_leaderboard,
    get_stable_proportional_sample,
)


# ---------------------------------------------------------------------------
# format_large_number
# ---------------------------------------------------------------------------

class TestFormatLargeNumber(unittest.TestCase):
    def test_none_returns_na(self):
        self.assertEqual(format_large_number(None), "N/A")

    def test_zero_returns_na(self):
        self.assertEqual(format_large_number(0), "N/A")

    def test_billions(self):
        self.assertEqual(format_large_number(1_500_000_000), "$1.50B")

    def test_millions(self):
        self.assertEqual(format_large_number(2_500_000), "$2.50M")

    def test_thousands(self):
        self.assertEqual(format_large_number(5_000), "$5.00K")

    def test_small_number(self):
        self.assertEqual(format_large_number(42.5), "$42.50")

    def test_exactly_one_billion(self):
        self.assertEqual(format_large_number(1_000_000_000), "$1.00B")

    def test_exactly_one_million(self):
        self.assertEqual(format_large_number(1_000_000), "$1.00M")

    def test_exactly_one_thousand(self):
        self.assertEqual(format_large_number(1_000), "$1.00K")


# ---------------------------------------------------------------------------
# get_stable_proportional_sample
# ---------------------------------------------------------------------------

class TestGetStableProportionalSample(unittest.TestCase):
    def _make_messages(self, n: int, start: datetime.datetime) -> list[dict]:
        return [
            {
                "date": (start + datetime.timedelta(hours=i)).isoformat(),
                "text": f"msg {i}",
                "username": "user",
            }
            for i in range(n)
        ]

    def test_returns_all_when_under_limit(self):
        msgs = self._make_messages(10, datetime.datetime(2024, 1, 1))
        result, was_sampled = get_stable_proportional_sample(msgs, 50)
        self.assertEqual(result, msgs)
        self.assertFalse(was_sampled)

    def test_returns_all_when_exactly_at_limit(self):
        msgs = self._make_messages(50, datetime.datetime(2024, 1, 1))
        result, was_sampled = get_stable_proportional_sample(msgs, 50)
        self.assertEqual(result, msgs)
        self.assertFalse(was_sampled)

    def test_samples_when_over_limit(self):
        msgs = self._make_messages(300, datetime.datetime(2024, 1, 1))
        result, was_sampled = get_stable_proportional_sample(msgs, 50)
        self.assertTrue(was_sampled)
        # Allow a slight overage because each day gets at least 1 message.
        self.assertLessEqual(len(result), 60)

    def test_result_is_chronologically_ordered(self):
        msgs = self._make_messages(200, datetime.datetime(2024, 3, 1))
        result, _ = get_stable_proportional_sample(msgs, 50)
        dates = [m["date"] for m in result]
        self.assertEqual(dates, sorted(dates))

    def test_empty_input_returns_empty(self):
        result, was_sampled = get_stable_proportional_sample([], 50)
        self.assertEqual(result, [])
        self.assertFalse(was_sampled)


# ---------------------------------------------------------------------------
# Buy bot activation boundaries
# ---------------------------------------------------------------------------

class TestBuyBotActivationCheckpoints(unittest.TestCase):
    def test_initializes_new_chats_without_replaying_history(self):
        original_db = bot.db
        fake_db = {}
        bot.db = fake_db
        try:
            initialized = _initialize_buybot_start_checkpoints(
                {
                    "0xabc::coin::COIN": [
                        (1001, None),
                        (1002, 88),
                    ]
                },
                latest_sequence=100,
            )
        finally:
            bot.db = original_db

        self.assertEqual(
            initialized,
            {"0xabc::coin::COIN": [(1001, 100), (1002, 88)]},
        )
        self.assertEqual(fake_db["buybot_start_checkpoint:1001"], 100)
        self.assertNotIn("buybot_start_checkpoint:1002", fake_db)

    def test_splits_catch_up_ranges_into_bridge_sized_batches(self):
        batches = [
            list(checkpoints)
            for checkpoints in _buybot_checkpoint_batches(101, 221)
        ]

        self.assertEqual(
            batches,
            [
                list(range(101, 151)),
                list(range(151, 201)),
                list(range(201, 222)),
            ],
        )


# ---------------------------------------------------------------------------
# format_detailed_leaderboard
# ---------------------------------------------------------------------------

class TestFormatDetailedLeaderboard(unittest.TestCase):
    def _make_leaderboard(self) -> list:
        return [
            ("alice", {"quality": 15.0, "tone": 12.0, "helpfulness": 10.0, "humor": 8.0, "total": 50.0}, 100, "1"),
            ("bob",   {"quality": 10.0, "tone": 8.0,  "helpfulness": 6.0,  "humor": 5.0, "total": 30.0},  50, "2"),
            ("carol", {"quality": 5.0,  "tone": 4.0,  "helpfulness": 3.0,  "humor": 2.0, "total": 14.0},  20, "3"),
        ]

    def test_output_contains_usernames(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("alice", text)
        self.assertIn("bob", text)
        self.assertIn("carol", text)

    def test_output_contains_leaderboard_header(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("Leaderboard", text)

    def test_first_place_gets_gold_medal(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("🥇", text)

    def test_second_place_gets_silver_medal(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("🥈", text)

    def test_third_place_gets_bronze_medal(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("🥉", text)

    def test_output_wrapped_in_pre_tags(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("<pre>", text)
        self.assertIn("</pre>", text)

    def test_date_range_present_in_output(self):
        lb = self._make_leaderboard()
        text = format_detailed_leaderboard(lb, "01/01/2024", "01/31/2024", 170, 3)
        self.assertIn("01/01/2024", text)
        self.assertIn("01/31/2024", text)

    def test_empty_leaderboard_still_returns_header(self):
        text = format_detailed_leaderboard([], "01/01/2024", "01/31/2024", 0, 0)
        self.assertIn("Leaderboard", text)


if __name__ == "__main__":
    unittest.main()
