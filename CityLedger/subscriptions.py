"""Group entitlements and Telegram Stars recurring subscriptions."""

import asyncio
import logging
import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice

from subscription_store import SubscriptionStore
from telegram_utils import user_is_admin

logger = logging.getLogger(__name__)
SUBSCRIPTION_PERIOD = 30 * 24 * 60 * 60
ACCESS_CACHE_SECONDS = 60
ACCESS_CACHE_SIZE = 4096
TERMS_VERSION = '1'


@dataclass(frozen=True)
class SubscriptionConfig:
    whitelist: frozenset[int]
    stars: int = 250
    support_url: str = ''

    @classmethod
    def from_env(cls):
        raw = os.environ.get('PREMIUM_WHITELIST_CHAT_IDS', '').strip()
        values = re.split(r'[\s,]+', raw) if raw else []
        if any(not re.fullmatch(r'-[1-9][0-9]*', value) for value in values):
            raise ValueError('PREMIUM_WHITELIST_CHAT_IDS must contain only negative group IDs separated by commas or whitespace')
        stars = int(os.environ.get('SUBSCRIPTION_PRICE_STARS', '250'))
        if not 1 <= stars <= 10000:
            raise ValueError('SUBSCRIPTION_PRICE_STARS must be between 1 and 10000')
        support = os.environ.get('PAYMENT_SUPPORT_URL', '').strip()
        if support and (urlparse(support).scheme != 'https' or not urlparse(support).netloc):
            raise ValueError('PAYMENT_SUPPORT_URL must be an HTTPS contact URL')
        return cls(frozenset(map(int, values)), stars, support)


class GroupAccess:
    def __init__(self, store, config, clock=time.time):
        self.store, self.config, self.clock = store, config, clock
        self._cache = OrderedDict()
        self._lock = asyncio.Lock()

    def invalidate(self, chat_id):
        self._cache.pop(chat_id, None)

    async def allowed(self, chat_id):
        if chat_id >= 0:
            return False  # Group subscriptions never authorize private AI use.
        if chat_id in self.config.whitelist:
            return True
        async with self._lock:
            now = self.clock()
            cached = self._cache.get(chat_id)
            if cached and now < cached[0]:
                self._cache.move_to_end(chat_id)
                # Never cache permission beyond the actual paid expiration.
                return now < cached[1]
            try:
                record = await asyncio.to_thread(self.store.get_subscription, chat_id)
                expires_at = int((record or {}).get('expires_at', 0))
            except Exception:
                logger.exception('Subscription lookup failed; chat tracking and AI denied')
                expires_at = 0
            self._cache[chat_id] = (now + ACCESS_CACHE_SECONDS, expires_at)
            self._cache.move_to_end(chat_id)
            while len(self._cache) > ACCESS_CACHE_SIZE:
                self._cache.popitem(last=False)
            return self.clock() < expires_at


def get_access(context):
    access = context.bot_data.get('group_access')
    if access is None:
        access = GroupAccess(SubscriptionStore(os.environ['DATABASE_URL']), SubscriptionConfig.from_env())
        context.bot_data['group_access'] = access
    return access


async def initialize_subscriptions(application):
    access = get_access(application)
    await asyncio.to_thread(access.store.ensure_schema)
    if not access.config.support_url:
        logger.warning('PAYMENT_SUPPORT_URL is unset; new subscription checkout is disabled')


async def has_group_access(context, chat_id):
    return await get_access(context).allowed(chat_id)


def subscribe_keyboard(chat_id, stars):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f'⭐ Subscribe · {stars} Stars / 30 days', callback_data=f'subscribe:{chat_id}')
    ]])


async def require_group_access(update, context, chat_id=None):
    chat_id = update.effective_chat.id if chat_id is None else chat_id
    if await has_group_access(context, chat_id):
        return True
    if chat_id >= 0:
        await update.effective_message.reply_text('Chat analytics are available inside a subscribed or whitelisted group. Use /subscribe in your group.')
    else:
        await update.effective_message.reply_text(
            '🔒 Chat tracking, AI analysis, statistics, and leaderboard features require a group subscription. '
            'An admin can subscribe for the whole group. New chat history is collected only while access is active.',
            reply_markup=subscribe_keyboard(chat_id, get_access(context).config.stars),
        )
    return False


def premium_group_feature(handler):
    @wraps(handler)
    async def guarded(update, context):
        if await require_group_access(update, context):
            return await handler(update, context)
    return guarded


def terms_text(config):
    return (
        f'<b>Group subscription terms (v{TERMS_VERSION})</b>\n\n'
        f'{config.stars} Telegram Stars per group every 30 days, automatically renewed until canceled. '
        'The dollar cost of Stars varies by platform, country, and tax; $4.99 is a pricing target, not a guaranteed exchange rate.\n\n'
        'The paying admin authorizes chat-message storage and AI analysis for this group and must inform its members. '
        'Chat text used by AI features is sent to OpenAI. Existing command permissions and processing limits still apply. '
        'Messages skipped while access is inactive cannot be recovered by subscribing.\n\n'
        'Cancel in Telegram subscription settings or use /subscription in this group. Cancellation stops renewal; '
        'access continues to the paid expiration. On expiry, new chat tracking and analytics stop. '
        'Previously stored history is retained; contact support about deletion or payment/refund issues. '
        'Telegram support cannot resolve purchases from this bot.\n\n'
        'Use /paysupport for the operator’s contact details.'
    )


async def terms_command(update, context):
    await update.effective_message.reply_text(terms_text(get_access(context).config), parse_mode='HTML')


async def paysupport_command(update, context):
    config = get_access(context).config
    text = 'For payment, refund, or deletion requests, contact the bot operator. Do not post payment details or personal data in a group.'
    keyboard = None
    if config.support_url:
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Contact payment support', url=config.support_url)]])
    else:
        text += '\nPayment support has not been configured; new purchases are disabled.'
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def subscribe_command(update, context):
    chat_id = update.effective_chat.id
    if chat_id >= 0:
        await update.effective_message.reply_text('Use /subscribe in the group you want to upgrade. One subscription covers that group.')
        return
    if await has_group_access(context, chat_id):
        await subscription_command(update, context)
        return
    await require_group_access(update, context)


async def subscription_command(update, context, chat_id=None):
    chat_id = update.effective_chat.id if chat_id is None else chat_id
    access = get_access(context)
    if chat_id >= 0:
        await update.effective_message.reply_text('Use /subscription in your group. You can also manage renewals in Telegram subscription settings.')
        return
    record = await asyncio.to_thread(access.store.get_subscription, chat_id)
    text = f'Group ID: {chat_id}\n'
    if chat_id in access.config.whitelist:
        text += '✅ Whitelisted: full features without a subscription.\n'
    keyboard = None
    if record and record.get('payload'):
        expires = datetime.fromtimestamp(record['expires_at'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        text += f"Paid access ends: {expires}.\n"
        text += 'Renewal canceled.' if record['canceled'] else 'Manage renewal in Telegram, or the payer can cancel below.'
        if not record['canceled'] and record.get('first_charge_id'):
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton('Cancel automatic renewal', callback_data=f'unsubscribe:{chat_id}')]])
    elif chat_id not in access.config.whitelist:
        text += 'No active subscription.'
        keyboard = subscribe_keyboard(chat_id, access.config.stars)
    if record and record.get('payload') and record['expires_at'] <= time.time() and chat_id not in access.config.whitelist:
        text += '\nAccess has expired. Use /subscribe to start a new subscription.'
        # Expose both actions: the old payer may still need to stop a pending renewal.
        rows = list(keyboard.inline_keyboard) if keyboard else []
        rows.extend(subscribe_keyboard(chat_id, access.config.stars).inline_keyboard)
        keyboard = InlineKeyboardMarkup(rows)
    await update.effective_message.reply_text(text, reply_markup=keyboard)


async def subscription_callback(update, context):
    query = update.callback_query
    parts = query.data.split(':')
    action, chat_id = parts[0], int(parts[1])
    if chat_id >= 0 or (update.effective_chat.id < 0 and update.effective_chat.id != chat_id):
        await query.answer('Use the subscription button in its original group.', show_alert=True)
        return
    access = get_access(context)
    if action == 'unsubscribe':
        record = await asyncio.to_thread(access.store.get_subscription, chat_id)
        if not record or record.get('user_id') != query.from_user.id or not record.get('first_charge_id'):
            await query.answer('Only the paying user can cancel this renewal.', show_alert=True)
            return
        await query.answer()
        await context.bot.edit_user_star_subscription(
            user_id=query.from_user.id, telegram_payment_charge_id=record['first_charge_id'], is_canceled=True,
        )
        await asyncio.to_thread(access.store.set_canceled, record['payload'])
        await query.message.reply_text('Automatic renewal canceled. Paid access remains until its expiration.')
        return
    if not await user_is_admin(context, chat_id, query.from_user.id):
        await query.answer('Only a group admin can start its subscription.', show_alert=True)
        return
    await query.answer()
    if await has_group_access(context, chat_id):
        await subscription_command(update, context, chat_id)
        return
    if not access.config.support_url:
        await paysupport_command(update, context)
        return
    if action == 'subscribe' or parts[2:] != [str(access.config.stars), TERMS_VERSION]:
        await query.message.reply_text(
            terms_text(access.config), parse_mode='HTML',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton('I agree · Continue to payment', callback_data=f'subagree:{chat_id}:{access.config.stars}:{TERMS_VERSION}')
            ]]),
        )
        return
    if action != 'subagree':
        return
    payload = f'group-v{TERMS_VERSION}-{secrets.token_urlsafe(24)}'
    await asyncio.to_thread(access.store.create_order, payload, chat_id, query.from_user.id, access.config.stars, int(time.time()))
    link = await context.bot.create_invoice_link(
        title='CityLedger Group Subscription',
        description=f'AI analytics and chat tracking for group {chat_id}. {access.config.stars} Stars every 30 days until canceled. Terms accepted; /paysupport for help.',
        payload=payload, provider_token='', currency='XTR',
        prices=[LabeledPrice('Group subscription · 30 days', access.config.stars)],
        subscription_period=SUBSCRIPTION_PERIOD,
    )
    await query.message.reply_text(
        f'Only the admin who created this checkout can pay. This upgrades group {chat_id}, not the payer’s other groups. Checkout expires in one hour.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f'Pay {access.config.stars} Stars · auto-renews every 30 days', url=link)]]),
    )


async def precheckout_callback(update, context):
    query = update.pre_checkout_query
    access = get_access(context)
    ok = False
    try:
        async with asyncio.timeout(8):
            order = await asyncio.to_thread(access.store.get_order, query.invoice_payload)
            if (order and query.currency == 'XTR' and query.total_amount == order['stars']
                    and query.from_user.id == order['user_id'] and access.config.support_url
                    and order['chat_id'] not in access.config.whitelist
                    and await user_is_admin(context, order['chat_id'], query.from_user.id)):
                ok = await asyncio.to_thread(
                    access.store.reserve_checkout, query.invoice_payload, query.from_user.id,
                    query.total_amount, query.id, int(time.time()),
                )
    except Exception:
        logger.exception('Subscription checkout validation failed')
    await query.answer(ok=ok, **({} if ok else {'error_message': 'Checkout unavailable, expired, already active, or not yours. Ask a group admin to run /subscribe again.'}))


async def successful_payment_callback(update, context):
    payment = update.effective_message.successful_payment
    access = get_access(context)
    expiration = payment.subscription_expiration_date
    if isinstance(expiration, datetime):
        expiration = int(expiration.timestamp())
    if payment.currency != 'XTR' or not payment.is_recurring or not expiration or not payment.telegram_payment_charge_id:
        logger.error('Unexpected non-subscription payment received; operator reconciliation required')
        await update.effective_message.reply_text('Payment could not be matched to a subscription. Please use /paysupport.')
        return
    # SuccessfulPayment is authoritative, including renewals after an admin
    # change or a price-config change. Never activate at pre-checkout time.
    result = None
    for attempt in range(3):
        try:
            result = await asyncio.to_thread(
                access.store.apply_payment, payment.invoice_payload, update.effective_user.id,
                payment.total_amount, payment.telegram_payment_charge_id, int(expiration), int(time.time()),
                bool(payment.is_first_recurring),
            )
            break
        except Exception:
            logger.exception('Could not persist subscription payment (attempt %s)', attempt + 1)
            if attempt < 2:
                await asyncio.sleep(attempt + 1)
    if result is None:
        await update.effective_message.reply_text('Your payment needs operator reconciliation. Please use /paysupport; do not pay again.')
        return
    access.invalidate(result['chat_id'])
    if result['refunded']:
        return
    if result['conflict']:
        # A delayed checkout can finish after another invoice has already paid.
        # Refund it instead of charging twice for the same active group.
        await context.bot.refund_star_payment(user_id=update.effective_user.id, telegram_payment_charge_id=payment.telegram_payment_charge_id)
        await asyncio.to_thread(access.store.mark_refunded, payment.invoice_payload, payment.telegram_payment_charge_id)
        await context.bot.edit_user_star_subscription(user_id=update.effective_user.id, telegram_payment_charge_id=payment.telegram_payment_charge_id, is_canceled=True)
        await asyncio.to_thread(access.store.set_canceled, payment.invoice_payload)
        access.invalidate(result['chat_id'])
        await update.effective_message.reply_text('This group already has an active subscription. The duplicate payment was refunded and its renewal canceled.')
    elif not result['duplicate']:
        expires = datetime.fromtimestamp(expiration, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        await update.effective_message.reply_text(f"✅ Group {result['chat_id']} has paid access through {expires}. Use /subscription in the group to manage renewal.")


async def refunded_payment_callback(update, context):
    payment = update.effective_message.refunded_payment
    access = get_access(context)
    order = await asyncio.to_thread(access.store.get_order, payment.invoice_payload)
    if not order or payment.currency != 'XTR' or payment.total_amount != order['stars']:
        logger.error('Unmatched Stars refund; operator reconciliation required')
        return
    chat_id = await asyncio.to_thread(access.store.mark_refunded, payment.invoice_payload, payment.telegram_payment_charge_id)
    access.invalidate(chat_id)
