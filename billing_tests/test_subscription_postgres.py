"""Real transaction tests. Uses a unique schema in TEST_DATABASE_URL only."""

import os
import sys
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg2
from psycopg2 import sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'CityLedger'))
from subscription_store import SubscriptionStore


@unittest.skipUnless(os.environ.get('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL for PostgreSQL integration tests')
class PostgresBillingTests(unittest.TestCase):
    def setUp(self):
        self.url = os.environ['TEST_DATABASE_URL']
        self.schema = 'billing_test_' + uuid.uuid4().hex
        self.conn = psycopg2.connect(self.url)
        self.conn.autocommit = True
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        # Search path is part of this test connection URL, never production data.
        params = psycopg2.extensions.parse_dsn(self.url)
        params['options'] = '-c search_path=' + self.schema
        self.store = SubscriptionStore(psycopg2.extensions.make_dsn(**params))
        self.store.ensure_schema()
        self.now = int(time.time())
        self.store.create_order('a', -100, 7, 250, self.now)
        self.store.create_order('b', -100, 8, 250, self.now)

    def tearDown(self):
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        self.conn.close()

    def pay(self, payload='a', user=7, charge='first', expiration=None, first=True):
        return self.store.apply_payment(payload, user, 250, charge, expiration or self.now + 2592000, self.now, first)

    def test_checkout_race_only_reserves_one_payer(self):
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda p: self.store.reserve_checkout(p[0], p[1], 250, p[0], self.now), [('a', 7), ('b', 8)]))
        self.assertEqual(sum(outcomes), 1)
        self.assertFalse(self.store.get_subscription(-100)['expires_at'])

    def test_duplicate_payment_is_idempotent_even_after_reconnect(self):
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: self.pay(), range(2)))
        self.assertEqual(sum(not result['duplicate'] for result in outcomes), 1)
        # A replay cannot change expiration, even if its supplied value differs.
        self.pay(expiration=self.now + 9999999)
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], self.now + 2592000)

    def test_renewals_and_out_of_order_delivery_never_shorten_access(self):
        self.pay(charge='renewal', expiration=self.now + 5184000, first=False)
        self.pay()
        record = self.store.get_subscription(-100)
        self.assertEqual(record['expires_at'], self.now + 5184000)
        self.assertEqual(record['first_charge_id'], 'first')
        self.store.set_canceled('a')
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], record['expires_at'])

    def test_amount_and_payer_are_checked_again_at_delivery(self):
        with self.assertRaises(ValueError):
            self.pay(user=8)
        with self.assertRaises(ValueError):
            self.store.apply_payment('a', 7, 1, 'forged', self.now + 100, self.now)
        self.assertIsNone(self.store.get_subscription(-100))

    def test_active_group_rejects_second_checkout(self):
        self.pay()
        self.assertFalse(self.store.reserve_checkout('b', 8, 250, 'another', self.now))

    def test_expired_invoice_cannot_be_purchased(self):
        self.assertFalse(self.store.reserve_checkout('a', 7, 250, 'old', self.now + 3600))

    def test_double_payment_is_marked_for_refund_without_overwriting_payer(self):
        self.pay()
        result = self.pay(payload='b', user=8, charge='duplicate-subscription')
        self.assertTrue(result['conflict'])
        self.assertEqual(self.store.get_subscription(-100)['user_id'], 7)
        self.store.mark_refunded('b', 'duplicate-subscription')
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], self.now + 2592000)

    def test_refunding_latest_period_reverts_to_previous_paid_period(self):
        self.pay()
        self.pay(charge='renewal', expiration=self.now + 5184000, first=False)
        self.store.mark_refunded('a', 'renewal')
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], self.now + 2592000)
        self.store.mark_refunded('a', 'first')
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], 0)
        self.pay()
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], 0)

    def test_refund_before_success_blocks_reactivation(self):
        self.store.mark_refunded('a', 'first')
        self.assertTrue(self.pay()['refunded'])
        self.assertEqual(self.store.get_subscription(-100)['expires_at'], 0)

    def test_replay_from_previous_subscription_cannot_affect_replacement(self):
        self.pay(expiration=self.now - 10)
        self.pay(payload='b', user=8, charge='replacement')
        result = self.pay()
        self.assertTrue(result['duplicate'])
        self.assertFalse(result['conflict'])
        self.assertEqual(self.store.get_subscription(-100)['user_id'], 8)


if __name__ == '__main__':
    unittest.main()
