import asyncio
import copy
import unittest
from unittest.mock import AsyncMock
from CityLedger.airdrop_utils import parse_airdrop_tiers, plan_batches, U64_MAX
from CityLedger.airdrop_runner import execute_batches, reconcile_batches


class TierTests(unittest.TestCase):
    def test_legacy_and_tiered_amounts(self):
        self.assertEqual(parse_airdrop_tiers(['10', '10000'], 9), [(1, 10, 10000 * 10**9)])
        self.assertEqual(parse_airdrop_tiers(['1-5:30k', '6-10:15k'], 9),
                         [(1, 5, 30000 * 10**9), (6, 10, 15000 * 10**9)])
        self.assertEqual(parse_airdrop_tiers(['1:1.25m', '2-3:0.001k'], 0),
                         [(1, 1, 1250000), (2, 3, 1)])

    def test_invalid_tiers_and_amounts(self):
        for args in [[], ['0', '1'], ['1', '0'], ['1', '-1'], ['1', 'NaN'],
                     ['2-5:10'], ['1-5:10', '5-10:5'], ['1:1', '3:1'],
                     ['1-1001:1'], ['1', '1', 'ignored'], ['1:0.0001'],
                     ['1:' + str(U64_MAX + 1)], ['1:1e10'], ['1:1,000']]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_airdrop_tiers(args, 3)

    def test_batches_are_bounded_and_keep_amounts(self):
        recipients = [{'rank': n, 'amount': str(n)} for n in range(1, 62)]
        batches = plan_batches(recipients)
        self.assertEqual([len(batch) for batch in batches], [25, 25, 11])
        self.assertEqual([item for batch in batches for item in batch], recipients)
        with self.assertRaises(ValueError):
            plan_batches([{'amount': str(U64_MAX)}, {'amount': '1'}])


class Store(dict):
    def __setitem__(self, key, value):
        super().__setitem__(key, copy.deepcopy(value))


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def run_data(self):
        return {'coin_type': 'coin', 'batches': [
            {'status': 'not_sent', 'recipients': [{'amount': '30', 'wallet': 'a'}]},
            {'status': 'not_sent', 'recipients': [{'amount': '15', 'wallet': 'b'}]},
        ]}

    async def test_persists_digest_before_submit_and_stops_on_unknown(self):
        store, run = Store(), self.run_data()
        async def transfer(recipients, coin, key, gas, before_submit):
            await before_submit('digest1')
            self.assertEqual(store['run']['batches'][0]['status'], 'submitted')
            raise RuntimeError('network interrupted')
        service = AsyncMock()
        service.transfer_batch.side_effect = transfer
        await execute_batches(service, store, 'run', run, 'secret', 50)
        self.assertEqual([b['status'] for b in run['batches']], ['unknown', 'not_sent'])
        self.assertEqual(service.transfer_batch.await_count, 1)
        self.assertNotIn('secret', str(store))
        service.transaction_status.return_value = {'success': True}
        await reconcile_batches(service, store, 'run', run)
        self.assertEqual(run['batches'][0]['status'], 'sent')
        self.assertEqual(service.transfer_batch.await_count, 1)

    async def test_definite_failure_does_not_mark_any_batch_recipient_paid(self):
        service, store, run = AsyncMock(), Store(), self.run_data()
        async def transfer(recipients, coin, key, gas, before_submit):
            await before_submit('failed')
            return {'success': False, 'digest': 'failed', 'error': 'reverted'}
        service.transfer_batch.side_effect = transfer
        await execute_batches(service, store, 'run', run, 'secret', 50)
        self.assertEqual([b['status'] for b in run['batches']], ['failed', 'not_sent'])
        self.assertEqual(service.transfer_batch.await_count, 1)

    async def test_unknown_lookup_stays_unknown(self):
        service, store, run = AsyncMock(), Store(), self.run_data()
        run['batches'][0].update(status='unknown', digest='d')
        service.transaction_status.side_effect = RuntimeError('not found')
        await reconcile_batches(service, store, 'run', run)
        self.assertEqual(run['batches'][0]['status'], 'unknown')
        service.transfer_batch.assert_not_called()

    async def test_preparation_failure_leaves_all_unsent(self):
        service, store, run = AsyncMock(), Store(), self.run_data()
        service.transfer_batch.side_effect = RuntimeError('simulation failed')
        await execute_batches(service, store, 'run', run, 'secret', 50)
        self.assertEqual([b['status'] for b in run['batches']], ['not_sent', 'not_sent'])

    async def test_successful_batches_have_separate_receipts(self):
        service, store, run = AsyncMock(), Store(), self.run_data()
        async def transfer(recipients, coin, key, gas, before_submit):
            digest = recipients[0]['wallet']
            await before_submit(digest)
            return {'success': True, 'digest': digest}
        service.transfer_batch.side_effect = transfer
        await execute_batches(service, store, 'run', run, 'secret', 50)
        self.assertEqual([b['status'] for b in store['run']['batches']], ['sent', 'sent'])
        self.assertEqual([b['digest'] for b in store['run']['batches']], ['a', 'b'])
