"""Billing tests use real Telegram types; only external I/O is mocked.

Run separately from legacy tests, which replace telegram in sys.modules.
"""

import asyncio
import datetime
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'CityLedger'))

from telegram import (Chat, Message, PreCheckoutQuery, RefundedPayment,
                      SuccessfulPayment, Update, User)
from telegram.ext import filters
import subscriptions as sub
from subscription_updates import BillingUpdateProcessor


def update_with_payment(**overrides):
    values = dict(currency='XTR', total_amount=250, invoice_payload='saved-order',
                  telegram_payment_charge_id='charge-1', provider_payment_charge_id='',
                  subscription_expiration_date=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
                  is_recurring=True, is_first_recurring=True)
    values.update(overrides)
    return Update(1, message=Message(
        1, datetime.datetime.now(datetime.timezone.utc), Chat(7, 'private'),
        from_user=User(7, 'Admin', False), successful_payment=SuccessfulPayment(**values),
    ))


class ConfigTests(unittest.TestCase):
    def test_empty_whitelist_is_deny_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            config = sub.SubscriptionConfig.from_env()
        self.assertEqual(config.whitelist, frozenset())
        self.assertEqual(config.stars, 250)
        self.assertFalse(config.support_url)

    def test_parse_secret_without_accepting_private_ids_or_wildcards(self):
        with patch.dict(os.environ, {'PREMIUM_WHITELIST_CHAT_IDS': '-1001, -2002\n-1001'}, clear=True):
            self.assertEqual(sub.SubscriptionConfig.from_env().whitelist, {-1001, -2002})
        for invalid in ('*', '123', '-1001,all', '0', '-0', '-1.5'):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {'PREMIUM_WHITELIST_CHAT_IDS': invalid}, clear=True):
                with self.assertRaises(ValueError):
                    sub.SubscriptionConfig.from_env()

    def test_price_and_support_validation(self):
        for value in ('0', '10001', '2.5', 'invalid'):
            with patch.dict(os.environ, {'SUBSCRIPTION_PRICE_STARS': value}, clear=True):
                with self.assertRaises(ValueError):
                    sub.SubscriptionConfig.from_env()
        with patch.dict(os.environ, {'PAYMENT_SUPPORT_URL': 'javascript:alert(1)'}, clear=True):
            with self.assertRaises(ValueError):
                sub.SubscriptionConfig.from_env()


class AccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 1000
        self.store = MagicMock()
        self.store.get_subscription.return_value = None
        self.access = sub.GroupAccess(self.store, sub.SubscriptionConfig(frozenset({-1})), clock=lambda: self.now)

    async def test_whitelist_and_private_chats_do_not_read_database(self):
        self.assertTrue(await self.access.allowed(-1))
        self.assertFalse(await self.access.allowed(1))
        self.store.get_subscription.assert_not_called()

    async def test_free_group_messages_share_one_cached_lookup(self):
        self.assertFalse(any(await asyncio.gather(*(self.access.allowed(-2) for _ in range(100)))))
        self.store.get_subscription.assert_called_once_with(-2)
        self.now += 61
        await self.access.allowed(-2)
        self.assertEqual(self.store.get_subscription.call_count, 2)

    async def test_expiry_is_enforced_inside_cache_lifetime(self):
        self.store.get_subscription.return_value = {'expires_at': 1001}
        self.assertTrue(await self.access.allowed(-2))
        self.now = 1001
        self.assertFalse(await self.access.allowed(-2))
        self.store.get_subscription.assert_called_once()

    async def test_payment_invalidation_enables_access_immediately(self):
        self.assertFalse(await self.access.allowed(-2))
        self.store.get_subscription.return_value = {'expires_at': 2000}
        self.access.invalidate(-2)
        self.assertTrue(await self.access.allowed(-2))
        self.store.get_subscription.return_value = None
        self.assertFalse(await self.access.allowed(-3))

    async def test_storage_outage_fails_closed(self):
        self.store.get_subscription.side_effect = RuntimeError('offline')
        with self.assertLogs(sub.logger, level='ERROR'):
            self.assertFalse(await self.access.allowed(-2))
        self.assertTrue(await self.access.allowed(-1))

    async def test_cache_is_bounded(self):
        with patch.object(sub, 'ACCESS_CACHE_SIZE', 3):
            for chat in range(-10, -20, -1):
                await self.access.allowed(chat)
        self.assertEqual(len(self.access._cache), 3)


class PaymentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = MagicMock()
        self.store.get_subscription.return_value = None
        self.store.get_order.return_value = dict(chat_id=-100, user_id=7, stars=250)
        self.store.reserve_checkout.return_value = True
        self.store.apply_payment.return_value = dict(chat_id=-100, duplicate=False, conflict=False, refunded=False)
        self.config = sub.SubscriptionConfig(frozenset(), support_url='https://t.me/operator')
        self.access = sub.GroupAccess(self.store, self.config)
        self.bot = SimpleNamespace(
            create_invoice_link=AsyncMock(return_value='https://t.me/$invoice'),
            get_chat_member=AsyncMock(return_value=SimpleNamespace(status='administrator')),
            edit_user_star_subscription=AsyncMock(), refund_star_payment=AsyncMock(),
        )
        self.context = SimpleNamespace(bot_data={'group_access': self.access}, bot=self.bot)
        self.reply_patch = patch.object(Message, 'reply_text', AsyncMock())
        self.reply = self.reply_patch.start()
        self.addCleanup(self.reply_patch.stop)

    def checkout(self, **overrides):
        values = dict(id='checkout-1', from_user=User(7, 'Admin', False), currency='XTR', total_amount=250, invoice_payload='saved-order')
        values.update(overrides)
        return Update(1, pre_checkout_query=PreCheckoutQuery(**values))

    async def test_checkout_validates_but_never_grants_access(self):
        with patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer:
            await sub.precheckout_callback(self.checkout(), self.context)
        answer.assert_awaited_once_with(ok=True)
        self.store.apply_payment.assert_not_called()

    async def test_checkout_rejects_wrong_payer_currency_price_and_unknown_payload(self):
        for overrides in ({'currency': 'USD'}, {'total_amount': 1}, {'from_user': User(8, 'Other', False)}):
            with self.subTest(overrides=overrides), patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer:
                await sub.precheckout_callback(self.checkout(**overrides), self.context)
                self.assertFalse(answer.call_args.kwargs['ok'])
        self.store.get_order.return_value = None
        with patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer:
            await sub.precheckout_callback(self.checkout(), self.context)
            self.assertFalse(answer.call_args.kwargs['ok'])
        self.store.reserve_checkout.assert_not_called()

    async def test_checkout_rechecks_admin_and_whitelist(self):
        self.bot.get_chat_member.return_value.status = 'member'
        with patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer:
            await sub.precheckout_callback(self.checkout(), self.context)
            self.assertFalse(answer.call_args.kwargs['ok'])
        self.access.config = sub.SubscriptionConfig(frozenset({-100}), support_url=self.config.support_url)
        self.bot.get_chat_member.return_value.status = 'administrator'
        with patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer:
            await sub.precheckout_callback(self.checkout(), self.context)
            self.assertFalse(answer.call_args.kwargs['ok'])

    async def test_checkout_storage_failure_is_answered_not_approved(self):
        self.store.get_order.side_effect = RuntimeError('offline')
        with patch.object(PreCheckoutQuery, 'answer', AsyncMock()) as answer, self.assertLogs(sub.logger, level='ERROR'):
            await sub.precheckout_callback(self.checkout(), self.context)
        self.assertFalse(answer.call_args.kwargs['ok'])

    async def test_success_uses_telegram_expiration_and_original_payer(self):
        update = update_with_payment()
        await sub.successful_payment_callback(update, self.context)
        args = self.store.apply_payment.call_args.args
        self.assertEqual(args[:4], ('saved-order', 7, 250, 'charge-1'))
        self.assertEqual(args[4], int(update.message.successful_payment.subscription_expiration_date.timestamp()))
        self.assertTrue(args[-1])
        self.reply.assert_awaited_once()

    async def test_renewal_does_not_depend_on_current_admin_or_configured_price(self):
        self.access.config = sub.SubscriptionConfig(frozenset(), stars=500)
        await sub.successful_payment_callback(update_with_payment(is_first_recurring=False), self.context)
        self.store.apply_payment.assert_called_once()
        self.bot.get_chat_member.assert_not_awaited()

    async def test_duplicate_or_previously_refunded_payment_does_not_announce_activation(self):
        for result in (dict(chat_id=-100, duplicate=True, conflict=False, refunded=False), dict(chat_id=-100, refunded=True)):
            self.store.apply_payment.return_value = result
            await sub.successful_payment_callback(update_with_payment(), self.context)
        self.reply.assert_not_awaited()

    async def test_non_subscription_payment_never_enables_access(self):
        with self.assertLogs(sub.logger, level='ERROR'):
            await sub.successful_payment_callback(update_with_payment(is_recurring=False), self.context)
        self.store.apply_payment.assert_not_called()

    async def test_conflicting_payment_is_refunded_and_renewal_canceled(self):
        self.store.apply_payment.return_value = dict(chat_id=-100, duplicate=False, conflict=True, refunded=False)
        await sub.successful_payment_callback(update_with_payment(), self.context)
        self.bot.refund_star_payment.assert_awaited_once_with(user_id=7, telegram_payment_charge_id='charge-1')
        self.bot.edit_user_star_subscription.assert_awaited_once()
        self.store.mark_refunded.assert_called_once()

    async def test_refund_service_message_revokes_cached_access(self):
        payment = RefundedPayment('XTR', 250, 'saved-order', 'charge-1')
        update = Update(1, message=Message(1, datetime.datetime.now(datetime.timezone.utc), Chat(7, 'private'), refunded_payment=payment))
        self.assertTrue(filters.StatusUpdate.REFUNDED_PAYMENT.check_update(update))
        self.store.mark_refunded.return_value = -100
        self.access._cache[-100] = (9999999999, 9999999999)
        await sub.refunded_payment_callback(update, self.context)
        self.assertNotIn(-100, self.access._cache)

    def callback_update(self, action='subscribe', user=7, chat=-100):
        # Query UI methods are intentionally mocked; invoice fields use the real SDK.
        query = SimpleNamespace(data=f'{action}:-100', from_user=User(user, 'Admin', False), answer=AsyncMock(), message=SimpleNamespace(reply_text=AsyncMock()))
        if action == 'subagree':
            query.data += ':250:1'
        return SimpleNamespace(callback_query=query, effective_chat=Chat(chat, 'group'), effective_message=query.message)

    async def test_inline_flow_requires_terms_before_recurring_invoice(self):
        update = self.callback_update()
        await sub.subscription_callback(update, self.context)
        self.bot.create_invoice_link.assert_not_awaited()
        terms = update.callback_query.message.reply_text.call_args
        self.assertIn('automatically renewed', terms.args[0])
        self.assertEqual(terms.kwargs['reply_markup'].inline_keyboard[0][0].callback_data, 'subagree:-100:250:1')
        update = self.callback_update('subagree')
        await sub.subscription_callback(update, self.context)
        invoice = self.bot.create_invoice_link.call_args.kwargs
        self.assertEqual(invoice['currency'], 'XTR')
        self.assertEqual(invoice['prices'][0].amount, 250)
        self.assertEqual(invoice['subscription_period'], 2592000)
        self.assertLessEqual(len(invoice['payload'].encode()), 128)
        self.assertEqual(self.store.create_order.call_args.args[1:4], (-100, 7, 250))

    async def test_no_checkout_without_support_contact_or_from_another_chat(self):
        self.access.config = sub.SubscriptionConfig(frozenset())
        await sub.subscription_callback(self.callback_update('subagree'), self.context)
        await sub.subscription_callback(self.callback_update('subagree', chat=-200), self.context)
        self.bot.create_invoice_link.assert_not_awaited()

    async def test_terms_must_be_accepted_again_after_price_change(self):
        self.access.config = sub.SubscriptionConfig(frozenset(), stars=500, support_url=self.config.support_url)
        update = self.callback_update('subagree')
        await sub.subscription_callback(update, self.context)
        self.bot.create_invoice_link.assert_not_awaited()
        self.assertIn('500 Telegram Stars', update.effective_message.reply_text.call_args.args[0])

    async def test_private_export_upgrade_keeps_the_original_group(self):
        await sub.subscription_callback(self.callback_update('subagree', chat=7), self.context)
        self.assertEqual(self.store.create_order.call_args.args[1:3], (-100, 7))

    async def test_only_payer_can_cancel_and_expiry_is_not_removed(self):
        self.store.get_subscription.return_value = dict(user_id=7, first_charge_id='initial', payload='saved-order')
        await sub.subscription_callback(self.callback_update('unsubscribe', user=8), self.context)
        self.bot.edit_user_star_subscription.assert_not_awaited()
        await sub.subscription_callback(self.callback_update('unsubscribe'), self.context)
        self.bot.edit_user_star_subscription.assert_awaited_once_with(user_id=7, telegram_payment_charge_id='initial', is_canceled=True)
        self.store.set_canceled.assert_called_once_with('saved-order')
        self.store.mark_refunded.assert_not_called()


class UpdateProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_billing_bypasses_slow_analytics_but_other_updates_stay_serial(self):
        processor = BillingUpdateProcessor()
        entered, release, paid = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        async def slow():
            order.append('first')
            entered.set()
            await release.wait()

        async def ordinary():
            order.append('second')

        async def checkout():
            paid.set()

        first = asyncio.create_task(processor.process_update(Update(1), slow()))
        await entered.wait()
        second = asyncio.create_task(processor.process_update(Update(2), ordinary()))
        payment = asyncio.create_task(processor.process_update(update_with_payment(), checkout()))
        await asyncio.wait_for(paid.wait(), timeout=1)
        self.assertEqual(order, ['first'])
        release.set()
        await asyncio.gather(first, second, payment)
        self.assertEqual(order, ['first', 'second'])


if __name__ == '__main__':
    unittest.main()
