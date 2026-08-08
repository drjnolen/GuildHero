import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_ROOT / "CityLedger"
if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

from telegram_utils import HELP_TEXT, normalize_wallet_address, sanitize_html_for_telegram


class TelegramUtilsTests(unittest.TestCase):
    def test_help_lists_buy_configuration_commands(self):
        self.assertIn("/setbuybot", HELP_TEXT)
        self.assertIn("/setbuyimage", HELP_TEXT)
        self.assertIn("/setminbuy", HELP_TEXT)
        self.assertIn("/nameguard", HELP_TEXT)

    def test_normalize_wallet_address_accepts_valid_sui_hex(self):
        raw = "A" * 64
        self.assertEqual(normalize_wallet_address(raw), "0x" + ("a" * 64))
        self.assertEqual(normalize_wallet_address("0x" + raw), "0x" + ("a" * 64))

    def test_normalize_wallet_address_rejects_invalid_values(self):
        self.assertIsNone(normalize_wallet_address(""))
        self.assertIsNone(normalize_wallet_address("0x1234"))
        self.assertIsNone(normalize_wallet_address("z" * 64))

    def test_sanitize_html_for_telegram_strips_unsupported_tags_and_unsafe_links(self):
        text = '<div>Hello</div><script>alert(1)</script><a href="javascript:alert(1)">bad</a><a href="https://example.com">ok</a>'
        result = sanitize_html_for_telegram(text)
        self.assertEqual(result, 'Helloalert(1)bad<a href="https://example.com">ok</a>')
        self.assertNotIn('<script>', result)
        self.assertNotIn('javascript:', result)


if __name__ == "__main__":
    unittest.main()
