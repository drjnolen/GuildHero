"""Unit tests for pure utility functions in bot.py.

bot.py has several external dependencies (psycopg2, openai, nacl, telegram).
We mock them all at the sys.modules level before importing so that only the
pure helper functions are exercised – no real network or database calls are made.
"""

import asyncio
import datetime
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    _buy_event_date,
    _buy_emoji_count,
    _buybot_media_from_message,
    _buybot_checkpoint_batches,
    _calculate_buy_valuation,
    _classify_buy_badges,
    _format_buy_announcement,
    _initialize_buybot_start_checkpoints,
    _send_buy_announcement,
    _updated_buybot_buyer_profile,
    format_large_number,
    format_detailed_leaderboard,
    get_stable_proportional_sample,
    setbuyimage_command,
)


# ---------------------------------------------------------------------------
# Buy announcement valuation and emoji scale
# ---------------------------------------------------------------------------

class TestBuyAnnouncementFormatting(unittest.TestCase):
    def _event(self, *, amount=10_000_000_000, sui_spent=None):
        return SimpleNamespace(
            amount=amount,
            sui_spent=sui_spent,
            exchange="Cetus",
            wallet="0xbuyer",
            sender="0xbuyer",
            digest="Digest123",
        )

    def test_uses_exact_gas_adjusted_sui_spend(self):
        valuation = _calculate_buy_valuation(
            self._event(sui_spent=2_000_000_000),
            {"symbol": "CITY", "decimals": 9},
            token_usd_price="99",
            sui_usd_price="1.50",
        )

        self.assertEqual(valuation["sui"], Decimal("2"))
        self.assertEqual(valuation["usd"], Decimal("3.00"))

        text = _format_buy_announcement(
            self._event(sui_spent=2_000_000_000),
            {"symbol": "CITY", "decimals": 9},
            valuation,
        )
        self.assertTrue(text.startswith("🟢 <b>CITY Buy!</b>\n🔥\n"))
        self.assertIn("<b>Value:</b> 2 SUI / $3.00 USD", text)

    def test_estimates_cross_token_buy_from_market_prices(self):
        valuation = _calculate_buy_valuation(
            self._event(),
            {"symbol": "CITY", "decimals": 9},
            token_usd_price="2",
            sui_usd_price="4",
        )

        self.assertEqual(valuation["usd"], Decimal("20"))
        self.assertEqual(valuation["sui"], Decimal("5"))

        text = _format_buy_announcement(
            self._event(),
            {"symbol": "CITY", "decimals": 9},
            valuation,
        )
        self.assertTrue(text.startswith("🟢 <b>CITY Buy!</b>\n🔥🔥🔥🔥🔥\n"))
        self.assertIn("<b>Value:</b> 5 SUI / $20.00 USD", text)

    def test_adds_one_emoji_for_each_five_dollars(self):
        self.assertEqual(_buy_emoji_count(None), 1)
        self.assertEqual(_buy_emoji_count(Decimal("4.99")), 1)
        self.assertEqual(_buy_emoji_count(Decimal("5")), 2)
        self.assertEqual(_buy_emoji_count(Decimal("10")), 3)

    def test_formats_smart_buyer_badges_below_emojis(self):
        text = _format_buy_announcement(
            self._event(),
            {"symbol": "CITY", "decimals": 9},
            {"sui": Decimal("25"), "usd": Decimal("100")},
            ["whale", "first_time"],
        )

        self.assertTrue(
            text.startswith(
                "🟢 <b>CITY Buy!</b>\n"
                "🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥\n"
                "🐋 <b>Whale Buy</b> · 🆕 <b>First-Time Buyer</b>\n"
            )
        )


class TestSmartBuyerBadges(unittest.TestCase):
    def test_first_observed_buy_can_also_be_a_whale(self):
        badges = _classify_buy_badges(
            {},
            Decimal("100"),
            datetime.date(2026, 7, 27),
        )

        self.assertEqual(badges, ["whale", "first_time"])

    def test_subsequent_buy_is_a_returning_holder(self):
        badges = _classify_buy_badges(
            {"buy_count": 1, "buy_dates": ["2026-07-26"]},
            Decimal("20"),
            datetime.date(2026, 7, 27),
        )

        self.assertEqual(badges, ["returning"])

    def test_third_consecutive_utc_day_earns_streak(self):
        profile = {
            "buy_count": 2,
            "buy_dates": ["2026-07-25", "2026-07-26"],
        }

        badges = _classify_buy_badges(
            profile,
            Decimal("20"),
            datetime.date(2026, 7, 27),
        )
        updated = _updated_buybot_buyer_profile(
            profile,
            datetime.date(2026, 7, 27),
        )

        self.assertEqual(badges, ["returning", "three_day_streak"])
        self.assertEqual(updated["buy_count"], 3)
        self.assertEqual(
            updated["buy_dates"],
            ["2026-07-25", "2026-07-26", "2026-07-27"],
        )

    def test_multiple_buys_on_one_day_do_not_advance_streak(self):
        profile = {
            "buy_count": 2,
            "buy_dates": ["2026-07-26"],
        }

        badges = _classify_buy_badges(
            profile,
            Decimal("20"),
            datetime.date(2026, 7, 26),
        )
        updated = _updated_buybot_buyer_profile(
            profile,
            datetime.date(2026, 7, 26),
        )

        self.assertEqual(badges, ["returning"])
        self.assertEqual(updated["buy_dates"], ["2026-07-26"])

    def test_uses_finalized_transaction_timestamp_in_utc(self):
        self.assertEqual(
            _buy_event_date(
                {"seconds": "1785126600", "nanos": 0},
                fallback=datetime.date(2000, 1, 1),
            ),
            datetime.date(2026, 7, 27),
        )


# ---------------------------------------------------------------------------
# Custom buy announcement media and command menu
# ---------------------------------------------------------------------------

class _FakeDB(dict):
    def enroll_chat(self, _chat_id):
        return None


class TestSmartBuyerBadgePersistence(unittest.TestCase):
    def test_records_profile_only_after_successful_announcement(self):
        async def exercise(send_error=None):
            originals = {
                "db": bot.db,
                "detect_buy": bot.detect_buy,
                "get_coin_amount_config": bot.get_coin_amount_config,
                "_get_buy_valuation": bot._get_buy_valuation,
                "_send_buy_announcement": bot._send_buy_announcement,
            }
            event = SimpleNamespace(
                amount=10_000_000_000,
                sui_spent=1_000_000_000,
                exchange="Cetus",
                wallet="0xbuyer",
                sender="0xbuyer",
                digest="Digest123",
                timestamp={"seconds": "1785126600", "nanos": 0},
            )
            fake_db = _FakeDB()
            send_announcement = AsyncMock(side_effect=send_error)
            bot.db = fake_db
            bot.detect_buy = MagicMock(return_value=event)
            bot.get_coin_amount_config = AsyncMock(
                return_value={"symbol": "CITY", "decimals": 9}
            )
            bot._get_buy_valuation = AsyncMock(
                return_value={"sui": Decimal("100"), "usd": Decimal("150")}
            )
            bot._send_buy_announcement = send_announcement
            try:
                all_sent = await bot._announce_checkpoint_buys(
                    SimpleNamespace(),
                    SimpleNamespace(sequence_number=11, transactions=[object()]),
                    {"0xabc::city::CITY": [(42, 10)]},
                )
            finally:
                for name, value in originals.items():
                    setattr(bot, name, value)
            return all_sent, fake_db, send_announcement

        all_sent, fake_db, send_announcement = asyncio.run(exercise())

        self.assertTrue(all_sent)
        announcement = send_announcement.await_args.args[2]
        self.assertIn("🐋 <b>Whale Buy</b>", announcement)
        self.assertIn("🆕 <b>First-Time Buyer</b>", announcement)
        buyer_keys = [
            key for key in fake_db if key.startswith("buybot_buyer:42:")
        ]
        self.assertEqual(len(buyer_keys), 1)
        self.assertEqual(fake_db[buyer_keys[0]]["buy_count"], 1)
        self.assertEqual(fake_db["buybot_seen:42"], ["Digest123"])

        all_sent, failed_db, _ = asyncio.run(exercise(RuntimeError("temporary")))

        self.assertFalse(all_sent)
        self.assertFalse(
            any(key.startswith("buybot_buyer:42:") for key in failed_db)
        )
        self.assertNotIn("buybot_seen:42", failed_db)


class TestBuyAnnouncementMedia(unittest.TestCase):
    def test_extracts_largest_photo_or_animation_file_id(self):
        photo_message = SimpleNamespace(
            animation=None,
            photo=[
                SimpleNamespace(file_id="small-photo"),
                SimpleNamespace(file_id="large-photo"),
            ],
        )
        animation_message = SimpleNamespace(
            animation=SimpleNamespace(file_id="animation-id"),
            photo=[],
        )

        self.assertEqual(
            _buybot_media_from_message(photo_message),
            {"type": "photo", "file_id": "large-photo"},
        )
        self.assertEqual(
            _buybot_media_from_message(animation_message),
            {"type": "animation", "file_id": "animation-id"},
        )

    def test_sends_photo_with_announcement_as_caption(self):
        async def exercise():
            original_db = bot.db
            bot.db = _FakeDB(
                {"buybot_media:42": {"type": "photo", "file_id": "photo-id"}}
            )
            telegram_bot = SimpleNamespace(
                send_photo=AsyncMock(),
                send_animation=AsyncMock(),
                send_message=AsyncMock(),
            )
            try:
                await _send_buy_announcement(
                    SimpleNamespace(bot=telegram_bot),
                    42,
                    "<b>CITY Buy!</b>",
                )
            finally:
                bot.db = original_db

            telegram_bot.send_photo.assert_awaited_once_with(
                chat_id=42,
                photo="photo-id",
                caption="<b>CITY Buy!</b>",
                parse_mode=bot.ParseMode.HTML,
            )
            telegram_bot.send_message.assert_not_awaited()

        asyncio.run(exercise())

    def test_sends_animation_with_announcement_as_caption(self):
        async def exercise():
            original_db = bot.db
            bot.db = _FakeDB(
                {
                    "buybot_media:42": {
                        "type": "animation",
                        "file_id": "animation-id",
                    }
                }
            )
            telegram_bot = SimpleNamespace(
                send_photo=AsyncMock(),
                send_animation=AsyncMock(),
                send_message=AsyncMock(),
            )
            try:
                await _send_buy_announcement(
                    SimpleNamespace(bot=telegram_bot),
                    42,
                    "<b>CITY Buy!</b>",
                )
            finally:
                bot.db = original_db

            telegram_bot.send_animation.assert_awaited_once_with(
                chat_id=42,
                animation="animation-id",
                caption="<b>CITY Buy!</b>",
                parse_mode=bot.ParseMode.HTML,
            )
            telegram_bot.send_message.assert_not_awaited()

        asyncio.run(exercise())

    def test_setbuyimage_saves_and_removes_group_media(self):
        async def exercise():
            original_db = bot.db
            original_require_admin = bot.require_admin
            fake_db = _FakeDB()
            bot.db = fake_db
            bot.require_admin = AsyncMock(return_value=True)
            reply_text = AsyncMock()
            message = SimpleNamespace(
                reply_to_message=SimpleNamespace(
                    animation=SimpleNamespace(file_id="animation-id"),
                    photo=[],
                ),
                reply_text=reply_text,
            )
            update = SimpleNamespace(
                effective_chat=SimpleNamespace(id=42, type="group"),
                message=message,
            )
            try:
                await setbuyimage_command(
                    update,
                    SimpleNamespace(args=[]),
                )
                self.assertEqual(
                    fake_db["buybot_media:42"],
                    {"type": "animation", "file_id": "animation-id"},
                )

                message.reply_to_message = None
                await setbuyimage_command(
                    update,
                    SimpleNamespace(args=["off"]),
                )
                self.assertNotIn("buybot_media:42", fake_db)
            finally:
                bot.db = original_db
                bot.require_admin = original_require_admin

        asyncio.run(exercise())

    def test_command_menu_includes_new_buy_commands(self):
        async def exercise():
            original_bot_command = bot.BotCommand
            bot.BotCommand = lambda command, description: SimpleNamespace(
                command=command,
                description=description,
            )
            set_my_commands = AsyncMock()
            try:
                await bot.setup_bot_commands(
                    SimpleNamespace(
                        bot=SimpleNamespace(set_my_commands=set_my_commands)
                    )
                )
            finally:
                bot.BotCommand = original_bot_command

            group_commands = set_my_commands.call_args_list[1].args[0]
            private_commands = set_my_commands.call_args_list[2].args[0]
            group_names = {command.command for command in group_commands}
            private_names = {command.command for command in private_commands}

            self.assertIn("setbuybot", group_names)
            self.assertIn("setbuyimage", group_names)
            self.assertNotIn("setbuybot", private_names)
            self.assertNotIn("setbuyimage", private_names)

        asyncio.run(exercise())


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
