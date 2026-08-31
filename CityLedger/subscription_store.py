"""Durable Stars billing, isolated from the bot's shared autocommit connection.

Billing mutations lock one group row in a transaction. This prevents concurrent
checkouts and makes payment delivery safe to retry after an uncertain commit.
"""

from contextlib import contextmanager

import psycopg2
from psycopg2.extras import RealDictCursor


SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_orders (
    payload TEXT PRIMARY KEY,
    chat_id BIGINT NOT NULL CHECK (chat_id < 0),
    user_id BIGINT NOT NULL,
    stars INTEGER NOT NULL CHECK (stars BETWEEN 1 AND 10000),
    created_at BIGINT NOT NULL,
    first_charge_id TEXT,
    canceled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS group_subscriptions (
    chat_id BIGINT PRIMARY KEY,
    payload TEXT REFERENCES subscription_orders(payload),
    expires_at BIGINT NOT NULL DEFAULT 0,
    checkout_id TEXT,
    checkout_until BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS subscription_payments (
    charge_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL REFERENCES subscription_orders(payload),
    expires_at BIGINT NOT NULL,
    refunded BOOLEAN NOT NULL DEFAULT FALSE,
    needs_refund BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_subscription_payments_payload
    ON subscription_payments(payload);
"""


class SubscriptionStore:
    def __init__(self, database_url):
        self.database_url = database_url

    @contextmanager
    def _transaction(self):
        # Separate connections keep billing transactions isolated from unrelated
        # message writes in asyncio.to_thread. Billing is infrequent; access reads
        # are cached by GroupAccess. Bound waits for Telegram's checkout deadline.
        existing_options = psycopg2.extensions.parse_dsn(self.database_url).get('options', '')
        conn = psycopg2.connect(
            self.database_url, connect_timeout=3,
            options=existing_options + " -c statement_timeout=3000 -c lock_timeout=2000",
        )
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur
        finally:
            conn.close()

    def ensure_schema(self):
        with self._transaction() as cur:
            cur.execute(SCHEMA)

    def get_subscription(self, chat_id):
        with self._transaction() as cur:
            cur.execute(
                """SELECT g.*, o.user_id, o.stars, o.first_charge_id, o.canceled
                   FROM group_subscriptions g
                   LEFT JOIN subscription_orders o ON o.payload = g.payload
                   WHERE g.chat_id = %s""", (chat_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def create_order(self, payload, chat_id, user_id, stars, now):
        with self._transaction() as cur:
            cur.execute(
                """INSERT INTO subscription_orders
                   (payload, chat_id, user_id, stars, created_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (payload, chat_id, user_id, stars, now),
            )

    def get_order(self, payload):
        with self._transaction() as cur:
            return self._order(cur, payload)

    @staticmethod
    def _order(cur, payload):
        cur.execute("SELECT * FROM subscription_orders WHERE payload = %s", (payload,))
        row = cur.fetchone()
        return dict(row) if row else None

    @staticmethod
    def _lock_group(cur, chat_id):
        cur.execute(
            "INSERT INTO group_subscriptions (chat_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (chat_id,),
        )
        cur.execute("SELECT * FROM group_subscriptions WHERE chat_id = %s FOR UPDATE", (chat_id,))
        return dict(cur.fetchone())

    def reserve_checkout(self, payload, user_id, stars, checkout_id, now):
        with self._transaction() as cur:
            order = self._order(cur, payload)
            if not order or order['user_id'] != user_id or order['stars'] != stars:
                return False
            group = self._lock_group(cur, order['chat_id'])
            # Orders are single-purchaser, expire after one hour, and cannot be
            # reused to start a second subscription. Renewals use payment events.
            order = self._order(cur, payload)
            if order['first_charge_id'] or now >= order['created_at'] + 3600:
                return False
            if group['expires_at'] > now:
                return False
            if group['checkout_until'] > now and group['checkout_id'] != checkout_id:
                return False
            cur.execute(
                "UPDATE group_subscriptions SET checkout_id = %s, checkout_until = %s WHERE chat_id = %s",
                (checkout_id, now + 120, order['chat_id']),
            )
            return True

    def apply_payment(self, payload, user_id, stars, charge_id, expires_at, now, is_first=False):
        with self._transaction() as cur:
            order = self._order(cur, payload)
            if not order or order['user_id'] != user_id or order['stars'] != stars:
                raise ValueError('Payment does not match a saved order')
            group = self._lock_group(cur, order['chat_id'])
            cur.execute(
                """INSERT INTO subscription_payments (charge_id, payload, expires_at)
                   VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING charge_id""",
                (charge_id, payload, expires_at),
            )
            inserted = cur.fetchone() is not None
            cur.execute("SELECT * FROM subscription_payments WHERE charge_id = %s", (charge_id,))
            payment = cur.fetchone()
            if payment['payload'] != payload:
                raise ValueError('Payment charge belongs to a different order')
            if payment['refunded']:
                return {'chat_id': order['chat_id'], 'refunded': True, 'duplicate': not inserted}
            if not inserted:
                return {
                    'chat_id': order['chat_id'], 'duplicate': True,
                    'conflict': payment['needs_refund'], 'refunded': False,
                }
            cur.execute(
                """UPDATE subscription_orders
                   SET first_charge_id = CASE WHEN %s THEN %s ELSE first_charge_id END
                   WHERE payload = %s""", (is_first, charge_id, payload),
            )
            conflict = group['expires_at'] > now and group['payload'] != payload
            if conflict:
                cur.execute('UPDATE subscription_payments SET needs_refund = TRUE WHERE charge_id = %s', (charge_id,))
            else:
                cur.execute(
                    """UPDATE group_subscriptions SET payload = %s,
                       expires_at = GREATEST(expires_at, %s),
                       checkout_id = NULL, checkout_until = 0 WHERE chat_id = %s""",
                    (payload, expires_at, order['chat_id']),
                )
            return {
                'chat_id': order['chat_id'], 'duplicate': not inserted,
                'conflict': conflict, 'refunded': False,
            }

    def mark_refunded(self, payload, charge_id):
        with self._transaction() as cur:
            order = self._order(cur, payload)
            if not order:
                raise ValueError('Unknown refunded order')
            self._lock_group(cur, order['chat_id'])
            # Keep a tombstone even if a refund arrives before payment delivery.
            cur.execute(
                """INSERT INTO subscription_payments (charge_id, payload, expires_at, refunded)
                   VALUES (%s, %s, 0, TRUE) ON CONFLICT (charge_id)
                   DO UPDATE SET refunded = TRUE
                   WHERE subscription_payments.payload = EXCLUDED.payload""",
                (charge_id, payload),
            )
            cur.execute(
                """UPDATE group_subscriptions SET expires_at = COALESCE(
                       (SELECT MAX(expires_at) FROM subscription_payments
                        WHERE payload = %s AND NOT refunded AND NOT needs_refund), 0)
                   WHERE chat_id = %s AND payload = %s""",
                (payload, order['chat_id'], payload),
            )
            return order['chat_id']

    def set_canceled(self, payload):
        with self._transaction() as cur:
            cur.execute("UPDATE subscription_orders SET canceled = TRUE WHERE payload = %s", (payload,))
