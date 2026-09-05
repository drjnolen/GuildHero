"""Build the real PTB application offline to catch wiring and SDK mismatches."""

import importlib
import os
import sys
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update
from telegram.ext import Application, MessageHandler, PreCheckoutQueryHandler, filters

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'CityLedger'))


class RegistrationTests(unittest.TestCase):
    def test_real_application_has_billing_before_conversations(self):
        fake_db = ModuleType('db')
        fake_db.db = MagicMock()
        with patch.dict(sys.modules, {'db': fake_db}):
            bot = importlib.import_module('bot')
        captured = []
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '123456:offline-test-token'}), \
                patch.object(Application, 'run_polling', lambda app, **kwargs: captured.append(app)):
            bot.main()
        app = captured[0]
        self.assertIsInstance(app.handlers[-1][0], PreCheckoutQueryHandler)
        self.assertEqual(app.handlers[-1][0].callback, bot.precheckout_callback)
        self.assertEqual(app.handlers[-1][1].callback, bot.successful_payment_callback)
        self.assertEqual(app.handlers[-1][2].callback, bot.refunded_payment_callback)
        self.assertTrue(any(isinstance(h, MessageHandler) and h.filters is filters.SUCCESSFUL_PAYMENT for h in app.handlers[-1]))
        self.assertEqual(type(app.update_processor).__name__, 'BillingUpdateProcessor')
        self.assertEqual(app.post_init, bot.initialize_services)
        fake_db.db.record_message.assert_not_called()


if __name__ == '__main__':
    unittest.main()
