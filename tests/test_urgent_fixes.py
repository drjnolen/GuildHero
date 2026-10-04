"""Regression cases from the October reliability review; all external I/O is mocked."""
import copy
import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from test_bot_utils import bot
from CityLedger.sui_utils import parse_token_amount


class MetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_lookup_is_retried_and_never_used_for_custom_payout(self):
        service = AsyncMock()
        service.get_coin_metadata.side_effect = [RuntimeError('outage'), {'decimals': 6, 'symbol': 'T'}]
        with patch.object(bot, 'get_sui_service', return_value=service), \
             patch.object(bot, '_coin_metadata_cache', {}):
            with self.assertRaisesRegex(ValueError, 'No payout'):
                await bot.get_coin_amount_config('custom', require_verified=True)
            config = await bot.get_coin_amount_config('custom', require_verified=True)
            self.assertEqual(config['decimals'], 6)
            self.assertEqual(parse_token_amount('1', config['decimals']), 10**6)
            await bot.get_coin_amount_config('custom', require_verified=True)
        self.assertEqual(service.get_coin_metadata.await_count, 2)

    async def test_invalid_decimals_fail_closed_but_native_sui_keeps_known_precision(self):
        for decimals in (None, True, -1, 256, '6', 6.5):
            with self.subTest(decimals=decimals), \
                 patch.object(bot, 'sui_get_coin_metadata', AsyncMock(return_value={'decimals': decimals})), \
                 patch.object(bot, 'DEFAULT_SUI_COIN_DECIMALS', 9):
                with self.assertRaises(ValueError):
                    await bot.get_coin_amount_config('custom', require_verified=True)
                config = await bot.get_coin_amount_config(bot.DEFAULT_SUI_COIN_TYPE, require_verified=True)
                self.assertEqual(config['decimals'], 9)

    async def test_buy_display_keeps_existing_fallback(self):
        with patch.object(bot, 'sui_get_coin_metadata', AsyncMock(return_value=None)), \
             patch.object(bot, 'DEFAULT_SUI_COIN_DECIMALS', 9):
            self.assertEqual((await bot.get_coin_amount_config('custom'))['decimals'], 9)


class WalletAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.update = SimpleNamespace(
            effective_chat=SimpleNamespace(type='private'),
            effective_user=SimpleNamespace(id=7),
            message=SimpleNamespace(text='remove', reply_text=AsyncMock(), delete=AsyncMock()))
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'airdrop_wallet_flows': {7: -100}}),
                                       bot=SimpleNamespace(get_chat=AsyncMock(return_value=SimpleNamespace(title='Test'))))

    async def test_revoked_admin_cannot_delete_or_replace_wallet(self):
        for value in ('remove', 'a' * 64):
            self.update.message.text = value
            self.context.application.bot_data['airdrop_wallet_flows'][7] = -100
            with patch.object(bot, 'user_is_admin', AsyncMock(return_value=False)) as check, \
                 patch.object(bot, 'delete_airdrop_wallet') as delete, \
                 patch.object(bot, 'store_airdrop_wallet') as store:
                await bot.receive_airdrop_private_key(self.update, self.context)
                check.assert_awaited_once_with(self.context, -100, 7)
                delete.assert_not_called()
                store.assert_not_called()
                self.assertNotIn(7, bot._get_airdrop_wallet_flows(self.context))

    async def test_permission_lookup_failure_never_mutates_wallet(self):
        with patch.object(bot, 'user_is_admin', AsyncMock(side_effect=RuntimeError('offline'))), \
             patch.object(bot, 'delete_airdrop_wallet') as delete:
            await bot.receive_airdrop_private_key(self.update, self.context)
            delete.assert_not_called()

    async def test_group_submission_is_rejected_and_message_deleted(self):
        self.update.effective_chat.type = 'supergroup'
        with patch.object(bot, 'user_is_admin', AsyncMock()) as check, \
             patch.object(bot, 'delete_airdrop_wallet') as delete:
            await bot.receive_airdrop_private_key(self.update, self.context)
            check.assert_not_awaited()
            delete.assert_not_called()
            self.update.message.delete.assert_awaited_once()

    async def test_current_admin_can_still_remove_wallet(self):
        with patch.object(bot, 'user_is_admin', AsyncMock(return_value=True)), \
             patch.object(bot, 'delete_airdrop_wallet') as delete:
            await bot.receive_airdrop_private_key(self.update, self.context)
            delete.assert_called_once_with(-100)

    async def test_setup_link_never_prompts_for_a_private_key_in_group(self):
        self.update.effective_chat.type = 'supergroup'
        self.context.args = ['airdropwallet_-100']
        with patch.object(bot, 'user_is_admin', AsyncMock()) as check:
            await bot.start_command(self.update, self.context)
            check.assert_not_awaited()
            self.assertIn('Never send a private key', self.update.message.reply_text.await_args.args[0])


class CoverageTests(unittest.IsolatedAsyncioTestCase):
    def messages(self, count=500, length=100):
        start = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        return [{'username': 'alice', 'text': f'item {i} ' + 'x' * length,
                 'date': (start + datetime.timedelta(minutes=i)).isoformat()} for i in range(count)]

    def test_character_budget_reduction_is_reported(self):
        messages = self.messages()
        transcript, coverage, days = bot.prepare_ai_transcript(messages, lambda msg: msg['text'])
        included = len(transcript.splitlines())
        self.assertLess(included, 500)
        self.assertIn(f'{included} of 500', coverage)
        self.assertIn('Partial coverage', coverage)
        self.assertNotIn('item 0 ', transcript)
        self.assertLessEqual(len(transcript), 12000)

    def test_count_sampling_and_single_clipped_message_are_disclosed(self):
        for messages in (self.messages(1500, 10), self.messages(1, 13000)):
            transcript, coverage, _ = bot.prepare_ai_transcript(messages, lambda msg: msg['text'])
            self.assertIn('Partial coverage', coverage)
            self.assertLessEqual(len(transcript), 12000)
        self.assertIn('shortened', coverage)

    async def test_all_three_ai_commands_include_coverage_in_final_output(self):
        messages = self.messages()
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=-100),
                                 effective_user=SimpleNamespace(id=7),
                                 message=SimpleNamespace(reply_text=AsyncMock()))
        context = SimpleNamespace(args=['500'])
        with patch.object(bot, '_check_ai_rate_limit', return_value=0), \
             patch.object(bot, '_record_ai_rate_limit'), patch.object(bot, '_track_chat'), \
             patch.object(bot, '_get_recent_messages', return_value=messages), \
             patch.object(bot, 'summarize_chat_history', return_value='summary'), \
             patch.object(bot, 'get_best_of_messages', return_value='digest'), \
             patch.object(bot, 'get_vibe_check', return_value={'sentiment': 'Neutral', 'summary': 'ok'}), \
             patch.object(bot, '_reply_with_footer', AsyncMock()) as reply:
            for handler in (bot.summarize_command, bot.bestof_command, bot.vibecheck_command):
                await handler.__wrapped__(update, context)
                self.assertIn('of 500 matching stored messages', reply.await_args.args[1])
                self.assertIn('Partial coverage', reply.await_args.args[1])


class Journal(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, copy.deepcopy(value))


class RaffleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Journal()
        self.service = AsyncMock()
        self.message = SimpleNamespace(message_id=101, reply_to_message=SimpleNamespace(message_id=99),
                                       reply_text=AsyncMock())
        self.update = SimpleNamespace(effective_chat=SimpleNamespace(id=-100), message=self.message)
        self.context = SimpleNamespace(args=['1'], application=SimpleNamespace(bot_data={}))
        patches = {
            'db': self.store, 'require_admin': AsyncMock(return_value=True),
            'require_group_access': AsyncMock(return_value=True),
            'get_sui_service': MagicMock(return_value=self.service),
            'get_coin_amount_config': AsyncMock(return_value={'decimals': 6, 'symbol': 'T'}),
            '_get_leaderboard_messages': MagicMock(return_value={(-100, 99): [('alice', {}, 1, '7')]}),
            'get_wallet': MagicMock(return_value={'wallet_address': '0x' + '7' * 64}),
            'resolve_airdrop_sender': MagicMock(return_value={'wallet_address': 'sender', 'private_key_hex': 'secret'}),
            'preflight_airdrop': AsyncMock(),
            'parse_token_amount': parse_token_amount, 'RAFFLE_MAX_RANK': 20,
            'select_weighted_raffle_winner': MagicMock(side_effect=lambda choices: choices[0]),
            '_report_airdrop': AsyncMock(),
        }
        for name, value in patches.items():
            patcher = patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        async def transfer(recipients, coin, key, gas, before_submit):
            await before_submit('digest')
            saved = self.store['raffle_run:-100:101']
            self.assertEqual(saved['batches'][0]['status'], 'submitted')
            self.assertEqual(saved['batches'][0]['recipients'][0]['user_id'], '7')
            self.assertEqual(self.store['raffle_latest:-100'], '101')
            return {'success': True, 'digest': 'digest'}
        self.service.transfer_batch.side_effect = transfer

    async def test_saves_winner_and_digest_and_redelivery_never_redraws(self):
        await bot.raffle_command(self.update, self.context)
        await bot.raffle_command(self.update, self.context)
        self.service.transfer_batch.assert_awaited_once()
        bot.select_weighted_raffle_winner.assert_called_once()
        bot.get_coin_amount_config.assert_awaited_once_with(bot.DEFAULT_SUI_COIN_TYPE, require_verified=True)
        saved = self.store['raffle_run:-100:101']
        self.assertEqual(saved['batches'][0]['status'], 'sent')
        self.assertEqual(saved['batches'][0]['recipients'][0]['amount'], '1000000')
        self.assertNotIn('secret', str(saved))

    async def test_ambiguous_submission_blocks_next_draw_and_status_only_reconciles(self):
        async def interrupted(recipients, coin, key, gas, before_submit):
            await before_submit('uncertain')
            raise RuntimeError('connection lost')
        self.service.transfer_batch.side_effect = interrupted
        await bot.raffle_command(self.update, self.context)
        self.assertEqual(self.store['raffle_run:-100:101']['batches'][0]['status'], 'unknown')
        self.service.transaction_status.side_effect = RuntimeError('not found')
        self.message.message_id = 102
        await bot.raffle_command(self.update, self.context)
        self.assertIn('unconfirmed payout', self.message.reply_text.await_args.args[0])
        self.service.transfer_batch.assert_awaited_once()
        bot.select_weighted_raffle_winner.assert_called_once()
        # Simulate restart: in-memory leaderboard/lock state is gone.
        self.context.application.bot_data.clear()
        bot._get_leaderboard_messages.return_value = {}
        self.context.args = ['status', '101']
        bot.require_group_access.side_effect = AssertionError('status must work after expiry')
        self.service.transaction_status.side_effect = None
        self.service.transaction_status.return_value = {'success': True}
        await bot.raffle_command(self.update, self.context)
        self.assertEqual(self.store['raffle_run:-100:101']['batches'][0]['status'], 'sent')
        self.service.transfer_batch.assert_awaited_once()

    async def test_metadata_failure_prevents_draw_and_transfer(self):
        bot.get_coin_amount_config.side_effect = ValueError('Token decimals unavailable')
        await bot.raffle_command(self.update, self.context)
        self.service.transfer_batch.assert_not_awaited()
        bot.select_weighted_raffle_winner.assert_not_called()
        self.assertFalse(self.store)

    async def test_journal_failure_prevents_submission(self):
        class BrokenStore(Journal):
            def __setitem__(self, key, value):
                raise RuntimeError('disk unavailable')
        with patch.object(bot, 'db', BrokenStore()), self.assertRaises(RuntimeError):
            await bot.raffle_command(self.update, self.context)
        self.service.transfer_batch.assert_not_awaited()

    async def test_unknown_airdrop_also_blocks_raffle(self):
        self.store['airdrop_latest:-100'] = '90'
        self.store['airdrop_run:-100:90'] = {'batches': [{'status': 'unknown', 'digest': 'other'}]}
        self.service.transaction_status.side_effect = RuntimeError('offline')
        await bot.raffle_command(self.update, self.context)
        self.service.transfer_batch.assert_not_awaited()
        self.assertIn('/airdrop status 90', self.message.reply_text.await_args.args[0])

    async def test_raffle_status_still_requires_admin(self):
        self.context.args = ['status']
        bot.require_admin.return_value = False
        await bot.raffle_command(self.update, self.context)
        self.service.transaction_status.assert_not_awaited()
        bot._report_airdrop.assert_not_awaited()

    async def test_airdrop_metadata_failure_never_transfers(self):
        self.context.args = ['1', '1']
        bot.get_coin_amount_config.side_effect = ValueError('Token decimals unavailable')
        await bot.airdrop_command(self.update, self.context)
        self.service.transfer_batch.assert_not_awaited()
        self.assertIn('Token decimals unavailable', self.message.reply_text.await_args.args[0])

