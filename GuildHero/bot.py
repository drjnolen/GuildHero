import os
import re
import math
import random
import datetime
import calendar
import asyncio
import logging
import json
import io
import csv
import signal
import sys
import html
import time
from collections import defaultdict
import pytz
import httpx
from db import db
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from telegram.request import HTTPXRequest
from openai import OpenAI

# Configure logging for debugging.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Initialize OpenAI client once
openai_client = None
def get_openai_client():
    """Returns a singleton OpenAI client instance."""
    global openai_client
    if openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        openai_client = OpenAI(api_key=api_key)
    return openai_client

# LRU cache for user analysis to avoid re-analyzing same content with bounded size
from functools import lru_cache
@lru_cache(maxsize=128)
def _analysis_cache_key(username: str, message_count: int, messages_text: str) -> str:
    """Generate cache key for analysis (using LRU cache with max 128 entries)."""
    import hashlib
    return hashlib.md5(f"{username}:{message_count}:{messages_text}".encode()).hexdigest()

# Conversation states
SELECTING_ACTION, AWAITING_EVENT_TEXT, AWAITING_WALLET = range(3)


# --- Constants ---
FOOTER_HTML = "\n\n<i>Product of Alpha City (<a href=\"https://app.nexa.xyz/trade/0x308fa16c7aead43e3a49a4ff2e76205ba2a12697234f4fe80a2da66515284060::city::CITY\">$CITY</a>)</i>"
MAX_MESSAGES_FOR_SUMMARY = 1000
# Set a safe upper limit for messages to load into memory at once to prevent crashes.
MAX_MESSAGES_TO_PROCESS = 1500
INVALID_FORMAT_MESSAGE = "Invalid format. Usage: /command #, or /command # topic. For example /command 100 or /command 500 Bitcoin"


# --- Badge Definitions ---
BADGES = {
    'contributor_100': {'name': 'Contributor', 'emoji': '✍️', 'description': 'Sent over 100 messages.'},
    'hero_500': {'name': 'Hero', 'emoji': '🦸', 'description': 'Sent over 500 messages.'},
    'godlike_1000': {'name': 'God-like', 'emoji': '⚡️', 'description': 'Sent over 1,000 messages.'},
    'weekly_champ': {'name': 'Weekly Champion', 'emoji': '👑', 'description': 'Finished #1 in a weekly leaderboard.'},
    'high_quality': {'name': 'High Quality', 'emoji': '✨', 'description': 'Achieved an average quality score of 18+.'},
    'helping_hand': {'name': 'Helping Hand', 'emoji': '🙏', 'description': 'Achieved a helpfulness score of 18+.'},
    'diamond_hands': {'name': 'Diamond Hands', 'emoji': '💎', 'description': 'Active in the group for 30+ days.'},
}

# --- CoinGecko API ---
COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
COINGECKO_CANDIDATE_FALLBACK_RANK = 10_000
COINGECKO_SCORE_PREFERRED_ALIAS = 1000
COINGECKO_SCORE_EXACT_SYMBOL_MATCH = 500
COINGECKO_SCORE_EXACT_NAME_MATCH = 450
COINGECKO_SCORE_EXACT_ID_MATCH = 400
COINGECKO_SCORE_SUI_PLATFORM_MATCH = 300
COINGECKO_SCORE_PARTIAL_SYMBOL_MATCH = 100
COINGECKO_SCORE_PARTIAL_NAME_MATCH = 75
COINGECKO_SCORE_PARTIAL_ID_MATCH = 50
COINGECKO_SCORE_SEARCH_ORDER_BONUS = 20
COINGECKO_SCORE_MARKET_CAP_BONUS = 125
PRICE_CACHE_TTL = 60  # seconds – avoid repeated API hits for the same symbol
_price_cache: dict[str, tuple[dict, float]] = {}

SUI_PRICE_ALIASES = {
    "afsui": "aftermath-staked-sui",
    "blub": "blub",
    "buck": "bucket-protocol",
    "cetus": "cetus-protocol",
    "sui": "sui",
    "deep": "deepbook-protocol",
    "deepbook": "deepbook-protocol",
    "fud": "fud-the-pug",
    "hasui": "haedal-staked-sui",
    "hippo": "sudeng",
    "navx": "navi-protocol",
    "wal": "walrus-2",
    "walrus": "walrus-2",
    "ns": "ns-protocol",
    "sca": "scallop-2",
    "sol": "solana",
    "sudeng": "sudeng",
    "suins": "ns-protocol",
    "turbos": "turbos-finance",
    "usdc": "usd-coin",
    "usdt": "tether",
    "vsui": "volo-staked-sui",
    "wbtc": "wrapped-bitcoin",
    "weth": "ethereum",
    "wusdc": "usd-coin",
    "wusdt": "tether",
}

# --- SUI Blockchain ---
SUI_RPC_URL = os.environ.get("SUI_RPC_URL", "https://fullnode.mainnet.sui.io:443")
DEFAULT_SUI_COIN_TYPE = "0x2::sui::SUI"
SUI_GAS_BUDGET = "50000000"  # 0.05 SUI


# --- Helper Functions ---
def escape_markdown(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2 parse mode."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

def sanitize_html_for_telegram(text: str) -> str:
    """Sanitizes HTML content to only include tags supported by Telegram."""
    # Remove unsupported HTML tags like <p>, <div>, etc. but keep their content
    # List of supported Telegram HTML tags: b, strong, i, em, u, ins, s, strike, del, code, pre, a
    unsupported_tags = ['p', 'div', 'span', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']

    for tag in unsupported_tags:
        # Remove opening and closing tags but keep content
        text = re.sub(f'<{tag}[^>]*>', '', text, flags=re.IGNORECASE)
        text = re.sub(f'</{tag}>', '', text, flags=re.IGNORECASE)

    # Replace <br> tags with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)

    # Clean up multiple consecutive newlines
    text = re.sub(r'\n\s*\n\s*\n+', '\n\n', text)

    return text.strip()

def get_stable_proportional_sample(messages, max_messages):
    """
    If the message count exceeds max_messages, it returns a proportionally sampled list
    from the entire period to ensure stability while maintaining representation.
    """
    if len(messages) <= max_messages:
        return messages, False  # Not sampled

    daily_messages = defaultdict(list)
    for msg in messages:
        daily_messages[datetime.datetime.fromisoformat(msg['date']).date()].append(msg)

    sampled_messages = []
    total_messages = len(messages)

    sorted_days = sorted(daily_messages.keys())

    for day in sorted_days:
        day_msgs = daily_messages[day]
        proportion = len(day_msgs) / total_messages
        sample_size = int(proportion * max_messages)

        # Ensure we sample at least one message if the day has any, to guarantee representation
        if len(day_msgs) > 0 and sample_size == 0:
            sample_size = 1

        sampled_messages.extend(random.sample(day_msgs, min(len(day_msgs), sample_size)))

    return sorted(sampled_messages, key=lambda x: x['date']), True  # Sampled

def get_proportionally_sampled_messages(messages):
    """
    If the message count exceeds the max for an AI prompt, it returns a proportionally sampled list.
    Otherwise, it returns the original list.
    """
    if len(messages) <= MAX_MESSAGES_FOR_SUMMARY:
        return messages

    daily_messages = defaultdict(list)
    for msg in messages:
        daily_messages[datetime.datetime.fromisoformat(msg['date']).date()].append(msg)

    sampled_messages = []
    total_messages = len(messages)
    for day, day_msgs in daily_messages.items():
        sample_size = int(len(day_msgs) / total_messages * MAX_MESSAGES_FOR_SUMMARY)
        sampled_messages.extend(random.sample(day_msgs, min(len(day_msgs), sample_size)))

    return sorted(sampled_messages, key=lambda x: x['date'])

def build_safe_transcript(messages, line_formatter, max_chars=12000):
    """Builds a transcript from messages, ensuring it doesn't exceed a character limit."""
    transcript_lines = []
    char_count = 0

    # Iterate in reverse to prioritize recent messages
    for msg in reversed(messages):
        line = line_formatter(msg)
        if char_count + len(line) + 1 > max_chars: # +1 for newline
            break
        transcript_lines.append(line)
        char_count += len(line) + 1

    # Return in chronological order
    return "\n".join(reversed(transcript_lines))

# --- Database Key Helper Functions ---
def _get_user_key(chat_id, user_id):
    return f"user:{chat_id}:{user_id}"

def _get_user_stats_key(chat_id, user_id):
    return f"user_stats:{chat_id}:{user_id}"

def _get_messages_key(chat_id):
    return f"messages:{chat_id}"

def _get_wallet_key(chat_id, user_id):
    return f"wallet:{chat_id}:{user_id}"

def _get_global_wallet_key(user_id):
    return f"wallet:global:{user_id}"

def _get_event_key(chat_id, date_obj):
    return f"event:{chat_id}:{date_obj.strftime('%Y-%m-%d')}"

def _get_timezone_key(chat_id):
    return f"timezone:{chat_id}"

def _get_announced_key(chat_id, date_obj):
    return f"announced:{chat_id}:{date_obj.strftime('%Y-%m-%d')}"

def _get_badges_key(chat_id, user_id):
    """Returns the database key for a user's badges."""
    return f"badges:{chat_id}:{user_id}"

def _get_welcome_key(chat_id):
    """Returns the database key for the welcome message toggle."""
    return f"welcome:{chat_id}"

def _get_airdrop_token_key(chat_id):
    """Returns the database key for the group's airdrop token type."""
    return f"airdrop_token:{chat_id}"

def get_events_for_month(chat_id, year, month):
    event_dates = set()
    prefix = f"event:{chat_id}:{year}-{str(month).zfill(2)}"
    event_keys = db.prefix(prefix)
    for key in event_keys:
        try:
            date_str = key.split(":")[-1]
            event_dates.add(datetime.datetime.strptime(date_str, "%Y-%m-%d").day)
        except (ValueError, IndexError):
            continue
    return event_dates

# --- Badge Awarding Logic ---
async def award_badge(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, badge_id: str):
    """Awards a badge to a user if they don't already have it."""
    badges_key = _get_badges_key(chat_id, user_id)
    user_badges = db.get(badges_key, [])

    if badge_id not in user_badges:
        user_badges.append(badge_id)
        db[badges_key] = user_badges

        badge_info = BADGES[badge_id]
        user = await context.bot.get_chat_member(chat_id, user_id)
        username = user.user.username or user.user.first_name

        text = (
            f"🎉 <b>Achievement Unlocked!</b> 🎉\n\n"
            f"@{username} has earned the <b>{badge_info['name']}</b> badge! {badge_info['emoji']}\n"
            f"<i>{badge_info['description']}</i>"
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML
        )

# --- Database Interaction Functions ---
async def store_message_db(context: ContextTypes.DEFAULT_TYPE, chat_id, user_id, username, first_name, last_name, text, date, is_reply, message_id):
    try:
        chat_id_str, user_id_str = str(chat_id), str(user_id)
        user_key = _get_user_key(chat_id_str, user_id_str)
        existing_user_data = db.get(user_key)
        new_user_data = {"username": username, "first_name": first_name, "last_name": last_name}
        if not existing_user_data or any(existing_user_data.get(k) != v for k, v in new_user_data.items()):
            db[user_key] = {**new_user_data, "updated_at": datetime.datetime.now().isoformat()}

        # Store the message itself
        message_key = _get_messages_key(chat_id_str)
        messages = db.get(message_key, [])
        messages.append({"user_id": user_id, "username": username, "text": text, "date": date.isoformat(), "is_reply": is_reply, "message_id": message_id})
        db[message_key] = messages[-10000:]

        # --- OPTIMIZATION: Increment message count in user_stats ---
        stats_key = _get_user_stats_key(chat_id_str, user_id_str)
        user_stats = db.get(stats_key, {"message_count": 0})
        user_stats["message_count"] += 1
        db[stats_key] = user_stats

        # Check for message count badges using the efficient count
        message_count = user_stats["message_count"]
        if message_count == 100:
            await award_badge(context, chat_id, user_id, 'contributor_100')
        elif message_count == 500:
            await award_badge(context, chat_id, user_id, 'hero_500')
        elif message_count == 1000:
            await award_badge(context, chat_id, user_id, 'godlike_1000')

        # Check for Diamond Hands badge (active for 30+ distinct days)
        if "first_seen" not in user_stats:
            user_stats["first_seen"] = date.isoformat()
            db[stats_key] = user_stats
        else:
            first_seen = datetime.datetime.fromisoformat(user_stats["first_seen"])
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=datetime.timezone.utc)
            now = date if date.tzinfo else date.replace(tzinfo=datetime.timezone.utc)
            if (now - first_seen).days >= 30:
                await award_badge(context, chat_id, user_id, 'diamond_hands')

    except Exception as e:
        logging.error(f"Error storing message from {username} in chat {chat_id}: {e}")

def store_wallet(chat_id, user_id, username, wallet_address):
    wallet_key = _get_wallet_key(chat_id, user_id)
    safe_username = username if username else f"user_{user_id}"
    wallet_data = {"username": safe_username, "wallet_address": wallet_address, "submitted_at": datetime.datetime.now().isoformat(), "user_id": str(user_id), "chat_id": str(chat_id)}
    db[wallet_key] = wallet_data
    if not db.get(wallet_key): raise Exception("Failed to verify wallet storage")
    return True

def store_wallet_private(user_id, username, wallet_address):
    wallet_key = _get_global_wallet_key(user_id)
    safe_username = username if username else f"user_{user_id}"
    wallet_data = {"username": safe_username, "wallet_address": wallet_address, "submitted_at": datetime.datetime.now().isoformat(), "user_id": str(user_id)}
    db[wallet_key] = wallet_data
    if not db.get(wallet_key): raise Exception("Failed to verify global wallet storage")
    return True

def get_messages_by_date_range(chat_id, start_date, end_date):
    """Get messages from database, checking for both new and old key formats."""
    chat_id_str = str(chat_id)
    new_message_key = _get_messages_key(chat_id_str)
    messages = db.get(new_message_key)
    if messages is None:
        old_message_key = f"{chat_id_str}:messages"
        messages = db.get(old_message_key, [])
    if start_date.tzinfo is None: start_date = start_date.replace(tzinfo=datetime.timezone.utc)
    if end_date.tzinfo is None: end_date = end_date.replace(tzinfo=datetime.timezone.utc)
    filtered = [msg for msg in messages if start_date <= datetime.datetime.fromisoformat(msg["date"]).replace(tzinfo=datetime.timezone.utc) <= end_date]
    return filtered

def get_chat_stats(chat_id):
    messages = get_messages_by_date_range(chat_id, datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), datetime.datetime.max.replace(tzinfo=datetime.timezone.utc))
    if not messages: return {'total_messages': 0, 'user_count': 0, 'oldest_date': None, 'newest_date': None, 'top_users': []}
    user_ids = {msg["user_id"] for msg in messages}
    dates = [datetime.datetime.fromisoformat(msg["date"]).replace(tzinfo=datetime.timezone.utc) for msg in messages if "date" in msg]
    user_message_counts = defaultdict(int)
    for msg in messages: user_message_counts[msg["username"]] += 1
    top_users = sorted(user_message_counts.items(), key=lambda item: item[1], reverse=True)[:5]
    return {'total_messages': len(messages), 'user_count': len(user_ids), 'oldest_date': min(dates), 'newest_date': max(dates), 'top_users': top_users}

def get_group_specific_wallet(chat_id, user_id):
    """Get wallet address for a specific group only, without global fallback."""
    chat_id_str, user_id_str = str(chat_id), str(user_id)

    # Check new format
    wallet_data = db.get(_get_wallet_key(chat_id_str, user_id_str))
    if wallet_data: return wallet_data

    # Check old format
    old_wallet_key = f"{chat_id_str}:wallet:{user_id_str}"
    wallet_data = db.get(old_wallet_key)
    return wallet_data

def get_wallet(chat_id, user_id):
    """Get wallet address, checking new, old, and global formats for backward compatibility."""
    chat_id_str, user_id_str = str(chat_id), str(user_id)

    wallet_data = db.get(_get_wallet_key(chat_id_str, user_id_str))
    if wallet_data: return wallet_data

    old_wallet_key = f"{chat_id_str}:wallet:{user_id_str}"
    wallet_data = db.get(old_wallet_key)
    if wallet_data: return wallet_data

    wallet_data = db.get(_get_global_wallet_key(user_id_str))
    return wallet_data


# --- SUI Airdrop Functions ---
def get_top_users_by_messages(chat_id, count):
    """Returns the top N users by message count, with user_id included."""
    chat_id_str = str(chat_id)
    all_stats_keys = db.prefix(f"user_stats:{chat_id_str}:")
    user_counts = []
    for key in all_stats_keys:
        try:
            key_user_id = key.split(":")[-1]
            stats = db.get(key)
            if stats and "message_count" in stats:
                user_counts.append((key_user_id, stats["message_count"]))
        except (ValueError, IndexError):
            continue
    user_counts.sort(key=lambda item: item[1], reverse=True)
    return user_counts[:count]


async def sui_rpc_call(method: str, params: list) -> dict:
    """Makes a JSON-RPC call to the SUI network."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            SUI_RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise Exception(f"SUI RPC error: {result['error']}")
        return result.get("result")


async def sui_get_coins(owner: str, coin_type: str = None) -> dict:
    """Gets coin objects owned by an address."""
    params = [owner]
    if coin_type:
        params.append(coin_type)
    return await sui_rpc_call("suix_getCoins", params)


async def sui_transfer_token(recipient: str, amount: int, coin_type: str) -> dict | None:
    """Builds, signs, and executes a SUI token transfer.

    Uses the SUI `unsafe_paySui` (for native SUI) or `unsafe_pay` (for other coins)
    RPC methods to build a transaction, then signs and executes it.

    Requires SUI_PRIVATE_KEY environment variable (hex-encoded Ed25519 private key).
    """
    import hashlib
    from nacl.signing import SigningKey
    import base64

    private_key_hex = os.environ.get("SUI_PRIVATE_KEY")
    if not private_key_hex:
        raise ValueError("SUI_PRIVATE_KEY environment variable not set")

    # Derive the sender address from the private key
    try:
        signing_key = SigningKey(bytes.fromhex(private_key_hex))
    except (ValueError, Exception) as e:
        raise ValueError("Invalid SUI_PRIVATE_KEY format. Expected 64 hex characters (32-byte Ed25519 key).") from e
    public_key = signing_key.verify_key.encode()

    # SUI address = blake2b(flag_byte + pubkey)[0:32], hex-encoded with 0x prefix
    addr_hash = hashlib.blake2b(b'\x00' + public_key, digest_size=32).digest()
    sender_address = "0x" + addr_hash.hex()

    is_sui = coin_type == "0x2::sui::SUI"

    if is_sui:
        # For native SUI, use unsafe_paySui
        coins_result = await sui_get_coins(sender_address, "0x2::sui::SUI")
        coin_ids = [c["coinObjectId"] for c in coins_result.get("data", [])]
        if not coin_ids:
            raise ValueError("Bot wallet has no SUI coins for transfer")

        tx_result = await sui_rpc_call("unsafe_paySui", [
            sender_address,
            coin_ids,
            [recipient],
            [str(amount)],
            SUI_GAS_BUDGET
        ])
    else:
        # For other coin types, use unsafe_pay
        coins_result = await sui_get_coins(sender_address, coin_type)
        coin_ids = [c["coinObjectId"] for c in coins_result.get("data", [])]
        if not coin_ids:
            raise ValueError(f"Bot wallet has no coins of type {coin_type}")

        # Need separate gas coins (SUI)
        gas_result = await sui_get_coins(sender_address, "0x2::sui::SUI")
        gas_data = gas_result.get("data", [])
        if not gas_data:
            raise ValueError("Bot wallet has no SUI for gas")
        gas_coin = gas_data[0].get("coinObjectId")

        tx_result = await sui_rpc_call("unsafe_pay", [
            sender_address,
            coin_ids,
            [recipient],
            [str(amount)],
            gas_coin,
            SUI_GAS_BUDGET
        ])

    # Sign the transaction
    tx_bytes_b64 = tx_result["txBytes"]
    tx_bytes = base64.b64decode(tx_bytes_b64)

    # Intent message: intent_scope(0) + version(0) + app_id(0) + tx_bytes
    intent_message = bytes([0, 0, 0]) + tx_bytes
    digest = hashlib.blake2b(intent_message, digest_size=32).digest()

    # Ed25519 signature
    signed = signing_key.sign(digest)
    signature = signed.signature  # 64 bytes

    # Serialized signature: flag(1) + sig(64) + pubkey(32) = 97 bytes
    serialized_sig = base64.b64encode(bytes([0x00]) + signature + public_key).decode()

    # Execute the transaction
    exec_result = await sui_rpc_call("sui_executeTransactionBlock", [
        tx_bytes_b64,
        [serialized_sig],
        {"showEffects": True},
        "WaitForLocalExecution"
    ])

    return exec_result


# --- Price Lookup ---
async def fetch_crypto_price(symbol: str) -> dict | None:
    """Fetches cryptocurrency price data from CoinGecko free API.

    Optimised to use at most 2 API calls per unique symbol (1 for known SUI
    aliases) and a 60-second in-memory cache to avoid 429 rate-limit errors
    on the free CoinGecko tier.
    """
    cache_key = symbol.lower().strip()
    cached = _price_cache.get(cache_key)
    if cached:
        result, ts = cached
        if time.monotonic() - ts < PRICE_CACHE_TTL:
            return result

    result = None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            normalized_query = re.sub(r"[^a-z0-9]+", "", symbol.lower())
            preferred_coin_id = SUI_PRICE_ALIASES.get(normalized_query)

            search_coin_map: dict[str, tuple[dict, int]] = {}

            if preferred_coin_id:
                # Known alias – skip the search entirely
                coin_ids = [preferred_coin_id]
            else:
                # Search for the coin by symbol/name
                search_resp = await client.get(
                    f"{COINGECKO_API_URL}/search",
                    params={"query": symbol}
                )
                search_resp.raise_for_status()
                coins = search_resp.json().get("coins", [])

                if not coins:
                    return None

                coin_ids = [coin["id"] for coin in coins[:10]]
                search_coin_map = {coin["id"]: (coin, i) for i, coin in enumerate(coins[:10])}

            # Single batch call replaces N×/coins/{id} + /simple/price
            markets_resp = await client.get(
                f"{COINGECKO_API_URL}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ",".join(coin_ids),
                    "price_change_percentage": "24h",
                    "order": "market_cap_desc",
                    "per_page": str(len(coin_ids)),
                    "page": "1",
                }
            )
            markets_resp.raise_for_status()
            markets: list[dict] = markets_resp.json()

            if not markets:
                return None

            def _normalize(v: str | None) -> str:
                return re.sub(r"[^a-z0-9]+", "", (v or "").lower())

            # Known SUI ecosystem coin IDs (for scoring, replaces platform API calls)
            _sui_coin_ids = set(SUI_PRICE_ALIASES.values())

            def _score(market: dict) -> tuple:
                coin_id = market["id"]
                coin_symbol = market.get("symbol") or ""
                coin_name = market.get("name") or ""
                market_cap_rank = market.get("market_cap_rank")
                search_coin, search_index = search_coin_map.get(coin_id, ({}, None))

                score = 0
                if preferred_coin_id and coin_id == preferred_coin_id:
                    score += COINGECKO_SCORE_PREFERRED_ALIAS
                if _normalize(coin_symbol) == normalized_query:
                    score += COINGECKO_SCORE_EXACT_SYMBOL_MATCH
                if _normalize(coin_name) == normalized_query:
                    score += COINGECKO_SCORE_EXACT_NAME_MATCH
                if _normalize(coin_id) == normalized_query:
                    score += COINGECKO_SCORE_EXACT_ID_MATCH
                if coin_id in _sui_coin_ids:
                    score += COINGECKO_SCORE_SUI_PLATFORM_MATCH
                if normalized_query and normalized_query in _normalize(coin_symbol):
                    score += COINGECKO_SCORE_PARTIAL_SYMBOL_MATCH
                if normalized_query and normalized_query in _normalize(coin_name):
                    score += COINGECKO_SCORE_PARTIAL_NAME_MATCH
                if normalized_query and normalized_query in _normalize(coin_id):
                    score += COINGECKO_SCORE_PARTIAL_ID_MATCH
                if search_index is not None:
                    score += max(0, COINGECKO_SCORE_SEARCH_ORDER_BONUS - search_index)
                if isinstance(market_cap_rank, int) and market_cap_rank > 0:
                    score += max(0, COINGECKO_SCORE_MARKET_CAP_BONUS - min(market_cap_rank, COINGECKO_SCORE_MARKET_CAP_BONUS))

                return (
                    score,
                    isinstance(market_cap_rank, int),
                    -(market_cap_rank or COINGECKO_CANDIDATE_FALLBACK_RANK),
                    -(COINGECKO_SCORE_SEARCH_ORDER_BONUS - search_index if search_index is not None else 0),
                )

            best = max(markets, key=_score, default=None)
            if not best:
                return None

            result = {
                "name": best.get("name") or symbol,
                "symbol": (best.get("symbol") or symbol).upper(),
                "price": best.get("current_price") or 0,
                "change_24h": best.get("price_change_percentage_24h") or 0,
                "market_cap": best.get("market_cap") or 0,
                "volume_24h": best.get("total_volume") or 0,
            }
    except Exception as e:
        logging.error(f"Error fetching price for {symbol}: {e}")
        return None

    if result is not None:
        _price_cache[cache_key] = (result, time.monotonic())
    return result


def format_large_number(num):
    """Formats large numbers with K/M/B suffixes."""
    if num is None or num == 0:
        return "N/A"
    if num >= 1_000_000_000:
        return f"${num / 1_000_000_000:.2f}B"
    if num >= 1_000_000:
        return f"${num / 1_000_000:.2f}M"
    if num >= 1_000:
        return f"${num / 1_000:.2f}K"
    return f"${num:.2f}"


# --- Calendar Feature Functions ---
def generate_calendar_keyboard(year, month, chat_id):
    keyboard = []
    cal = calendar.Calendar()
    events_this_month = get_events_for_month(chat_id, year, month)
    keyboard.append([InlineKeyboardButton(f"{calendar.month_name[month]} {year}", callback_data="cal_ignore")])
    keyboard.append([InlineKeyboardButton(day, callback_data="cal_ignore") for day in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]])
    for week in cal.monthdayscalendar(year, month):
        row = [InlineKeyboardButton(f"{day}{' 🗓️' if day in events_this_month else ''}", callback_data=f"cal_day_{year}_{month}_{day}") if day != 0 else InlineKeyboardButton(" ", callback_data="cal_ignore") for day in week]
        keyboard.append(row)
    prev_month_date = datetime.date(year, month, 1) - datetime.timedelta(days=1)
    next_month_date = (datetime.date(year, month, 1) + datetime.timedelta(days=32)).replace(day=1)
    keyboard.append([
        InlineKeyboardButton("⬅️ Prev", callback_data=f"cal_nav_{prev_month_date.year}_{prev_month_date.month}"),
        InlineKeyboardButton("Close Calendar", callback_data="cal_close"),
        InlineKeyboardButton("Next ➡️", callback_data=f"cal_nav_{next_month_date.year}_{next_month_date.month}")
    ])
    return InlineKeyboardMarkup(keyboard)

async def check_and_announce_events(context: ContextTypes.DEFAULT_TYPE):
    """Job to check for and announce today's events based on chat-specific timezones."""
    message_keys = db.prefix("messages:")
    chat_ids = set(key.split(":")[1] for key in message_keys)

    for chat_id_str in chat_ids:
        try:
            chat_id = int(chat_id_str)
            tz_name = db.get(_get_timezone_key(chat_id), "UTC")

            chat_tz = pytz.timezone(tz_name)
            now_in_tz = datetime.datetime.now(chat_tz)

            today_date = now_in_tz.date()
            announced_key = _get_announced_key(chat_id, today_date)

            if now_in_tz.hour == 8 and announced_key not in db:
                event_key = _get_event_key(chat_id, today_date)
                event_text = db.get(event_key)
                if event_text:
                    await context.bot.send_message(chat_id=chat_id, text=f"📢 <b>Today's Event</b>\n\n{event_text}", parse_mode=ParseMode.HTML)
                    db[announced_key] = True 
        except Exception as e:
            logging.error(f"Failed during event announcement check for chat {chat_id_str}: {e}")


# --- Core Bot Logic ---
def analyze_user_messages(username: str, message_count: int, messages_text: str) -> dict:
    cache_key = _analysis_cache_key(username, message_count, messages_text)
    prompt = f"User {username} posted {message_count} messages. Evaluate contributions on quality, tone, helpfulness, humor (0-20). Return valid JSON with keys: \"quality\", \"tone\", \"helpfulness\", \"humor\". Messages:\n\n{messages_text}"
    try:
        client = get_openai_client()
        response = client.chat.completions.create(model="gpt-3.5-turbo", messages=[{"role": "system", "content": "You are an analytical assistant."}, {"role": "user", "content": prompt}], temperature=0.1, max_tokens=100, timeout=30.0)
        breakdown = json.loads(response.choices[0].message.content.strip())
        return breakdown
    except Exception as e:
        logging.error(f"Error analyzing messages for {username}: {e}")
        return {"quality": 8, "tone": 10, "helpfulness": 8, "humor": 8}

def summarize_chat_history(chat_transcript: str, days: int = None, topic: str = None) -> str:
    """Uses OpenAI to summarize a chat transcript, optionally focusing on a topic."""

    summary_context = f"from the last {days} day(s)" if days is not None else "from the recent chat history"

    if topic:
        prompt = f"""
Analyze the following Telegram chat transcript {summary_context}, focusing specifically on discussions related to "{topic}".
Provide a detailed summary of these specific conversations. Include key points made and the users who made them. Be specific about entities mentioned. For example, instead of saying "user discussed a certain coin," specifically state the coin described. Make sure to format usernames as clickable links (e.g., @Username). Use only HTML tags for formatting (e.g., <b>, <i>, <blockquote>). Do not use Markdown (e.g., **, __).

Transcript:
{chat_transcript}
"""
    else:
        prompt = f"""
Analyze the following Telegram chat transcript {summary_context}.
Provide a summary of the conversation in chronological order, grouped by date.

For each date, create a heading using the HTML bold tag in <b>MM/DD/YYYY</b> format. Do not use Markdown for dates.
Under each date, provide 3-5 bullet points summarizing the key conversations.
It is crucial that you include specific names of cryptocurrencies, projects, or campaigns directly within these bullet points.
In each bullet point, mention the key participants by their clickable usernames (e.g., @Username).

Transcript:
{chat_transcript}
"""
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that provides detailed, chronologically ordered summaries of chat conversations in HTML format. Do not include `<html>`, `<head>`, or `<body>` tags in your response. Only use HTML tags like <b> for formatting."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=700,
            timeout=120.0
        )
        summary = response.choices[0].message.content.strip()
        return sanitize_html_for_telegram(summary)
    except Exception as e:
        logging.error(f"Error summarizing chat history: {e}")
        return "Sorry, I was unable to generate a summary at this time."

def get_best_of_messages(chat_transcript: str, days: int) -> str:
    """Uses OpenAI to select the best messages from a transcript."""
    prompt = f"""
Act as a community moderator reviewing a Telegram chat transcript from the last {days} day(s).
Your task is to select 1-3 of the best messages for each of the following categories: Most Humorous, Most Degen (ridiculous trade suggestions, big bets), Best Alpha (thoughtful advice/information), and Most Helpful.

Format your response exactly as follows, using HTML and emojis:

😂 <b>Most Humorous</b>
<blockquote>@Username: [Full text of the first humorous message]</blockquote>
<blockquote>@Username: [Full text of the second humorous message]</blockquote>

💰 <b>Most Degen</b>
<blockquote>@Username: [Full text of the first degen message]</blockquote>

🧠 <b>Best Alpha</b>
<blockquote>@Username: [Full text of the first alpha message]</blockquote>

🙏 <b>Most Helpful</b>
<blockquote>@Username: [Full text of the first helpful message]</blockquote>

If you can't find any messages for a category, omit that category entirely from your response.

Transcript:
{chat_transcript}
"""
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a community moderator who highlights the best contributions in a chat using HTML formatting. Do not include `<html>`, `<head>`, or `<body>` tags in your response."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=800,
            timeout=120.0
        )
        best_of = response.choices[0].message.content.strip()
        return sanitize_html_for_telegram(best_of)
    except Exception as e:
        logging.error(f"Error getting 'best of' messages: {e}")
        return "Could not generate the 'Best Of' digest at this time."

def get_vibe_check(chat_transcript: str, topic: str = None) -> dict:
    """Uses OpenAI to analyze the sentiment of a chat transcript."""
    topic_prompt = f"focusing on the topic '{topic}'" if topic else "on the overall conversation"

    prompt = f"""
Analyze the sentiment of the following Telegram chat transcript {topic_prompt}.
Your response must be a valid JSON object with three keys:
1. "key_messages": An array of 3-4 strings, each containing a key message that exemplifies the sentiment. Format them as "@Username: [Full message text]".
2. "sentiment": A single string qualifier from this list: "Mega-bearish", "Bearish", "Neutral", "Bullish", "Mega-bullish".
3. "summary": A brief summary (less than 200 characters) of the sentiment.

Transcript:
{chat_transcript}
"""
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You are a sentiment analysis expert who analyzes Telegram chat transcripts and responds with structured JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
            max_tokens=600,
            timeout=120.0
        )
        vibe_data = json.loads(response.choices[0].message.content)
        return vibe_data
    except Exception as e:
        logging.error(f"Error getting vibe check: {e}")
        return None

def generate_copypasta(user_transcript: str) -> str:
    """Uses OpenAI to generate a copypasta based on a user's message history."""
    prompt = f"""
Based on the following messages from a user, write a short, unhinged, and deranged-sounding copypasta that captures their personality, writing style, and common topics. The copypasta should be from their perspective.

Messages:
{user_transcript}
"""
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a creative writer who specializes in internet humor and copypastas."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.8,
            max_tokens=250,
            timeout=60.0
        )
        copypasta = response.choices[0].message.content.strip()
        return copypasta
    except Exception as e:
        logging.error(f"Error generating copypasta: {e}")
        return "I tried to get weird, but something went wrong."

async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or update.message.from_user.is_bot: return
    text = update.message.text.strip()
    if text.startswith('/') or len(text.split()) < 3: return
    user = update.message.from_user
    await store_message_db(context, update.effective_chat.id, user.id, user.username or user.first_name, user.first_name, user.last_name, update.message.text, update.message.date, update.message.reply_to_message is not None, update.message.message_id)

async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcomes new members joining the group chat, if enabled by admins."""
    if not update.message or not update.message.new_chat_members:
        return
    # Check if welcome messages are enabled for this group (default: off)
    chat_id = update.effective_chat.id
    welcome_enabled = db.get(_get_welcome_key(chat_id), False)
    if not welcome_enabled:
        return
    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        name = f"@{member.username}" if member.username else html.escape(member.first_name)
        welcome_text = (
            f"👋 Welcome to the group, <b>{name}</b>!\n\n"
            f"Type /help to see what I can do!"
        )
        await update.message.reply_text(welcome_text + FOOTER_HTML, parse_mode=ParseMode.HTML)

async def _parse_date_range(args):
    if not args: return None, None, None

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    # Handle /command 7 days format
    if len(args) >= 2 and args[1].lower().startswith('day'):
        try:
            days = int(args[0])
            # To include the full current day, we set the end time to the very end of today.
            end_date = now_utc.replace(hour=23, minute=59, second=59, microsecond=999999)
            # And the start time to the beginning of the start day.
            start_date = (end_date - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start_date, end_date, days
        except ValueError:
            pass # Fall through to other formats

    # Handle /command 7days format
    date_range_str = args[0].lower()
    if date_range_str.endswith(('day', 'days')):
        import re
        match = re.match(r'^(\d+)days?$', date_range_str)
        if match: 
            days = int(match.group(1))
            end_date = now_utc.replace(hour=23, minute=59, second=59, microsecond=999999)
            start_date = (end_date - datetime.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return start_date, end_date, days
        return None, None, None

    # Handle /command MM/DD/YYYY-MM/DD/YYYY format
    try:
        start_str, end_str = date_range_str.split("-", 1)
        start_date = datetime.datetime.strptime(start_str.strip(), "%m/%d/%Y").replace(tzinfo=datetime.timezone.utc)
        end_date = datetime.datetime.strptime(end_str.strip(), "%m/%d/%Y").replace(hour=23, minute=59, second=59, tzinfo=datetime.timezone.utc)
        days = (end_date - start_date).days + 1
        return start_date, end_date, days
    except ValueError:
        return None, None, None

async def generate_leaderboard(context: ContextTypes.DEFAULT_TYPE, chat_id, start_date, end_date, start_str, end_str, export=False):
    filtered_messages = await asyncio.to_thread(get_messages_by_date_range, chat_id, start_date, end_date)
    if not filtered_messages: return None, "No messages found in this date range."

    # Stability improvement for high-volume chats
    filtered_messages, _ = await asyncio.to_thread(get_stable_proportional_sample, filtered_messages, MAX_MESSAGES_TO_PROCESS)

    user_messages = defaultdict(list)
    for msg in filtered_messages: user_messages[msg["user_id"]].append(msg)
    if not user_messages: return None, "No eligible messages found."
    leaderboard = []
    for user_id, msgs in user_messages.items():
        username, n = msgs[0]["username"], len(msgs)
        sample = random.sample(msgs, k=min(n, 200))
        metrics = await asyncio.to_thread(analyze_user_messages, username, n, "\n".join(m["text"] for m in sample))
        metrics["helpfulness"] += min(sum(1 for m in sample if m["is_reply"]) * 0.2, 20 - metrics["helpfulness"])
        metrics["total"] = (sum(metrics.values()) / 4) * (math.log1p(min(n, 200)) * 1.37)
        leaderboard.append((username, metrics, n, user_id))
    leaderboard.sort(key=lambda x: x[1]["total"], reverse=True)

    detailed_text = format_detailed_leaderboard(leaderboard[:20], start_str, end_str, len(filtered_messages), len(user_messages))
    csv_data = await generate_csv_from_leaderboard(leaderboard, chat_id) if export else None
    return (detailed_text, csv_data, leaderboard), None

def format_detailed_leaderboard(top_20, start_str, end_str, msg_count, user_count):
    text = (f"🏆 <b>Top 20 Leaderboard</b> ({start_str} → {end_str})\n"
            f"📊 Analyzed {msg_count} messages from {user_count} users\n\n<pre>"
            "Rank | User                 | Msgs | Quality | Tone | Help | Humor | Total\n"
            "---------------------------------------------------------------------------\n")
    for idx, (uname, met, count, _) in enumerate(top_20, 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "  "
        safe_uname = html.escape(uname[:19])
        text += (f"{medal}{idx:2d} | {safe_uname:<19} | {count:4d} | {met['quality']:6.1f} | "
                 f"{met['tone']:4.1f} | {met['helpfulness']:4.1f} | {met['humor']:4.1f} | {met['total']:7.1f}\n")
    return text + "</pre>"

async def generate_csv_from_leaderboard(leaderboard_data, chat_id):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Username", "Messages", "Quality", "Tone", "Helpfulness", "Humor", "Total", "Wallet"])
    for uname, met, count, user_id in leaderboard_data:
        wallet_data = await asyncio.to_thread(get_wallet, chat_id, user_id)
        writer.writerow([uname, count, met["quality"], met["tone"], met["helpfulness"], met["humor"], met["total"], wallet_data['wallet_address'] if wallet_data else ""])
    return output.getvalue().encode("utf-8")

# --- Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command, used for deep-linking and showing help."""
    logging.info(f"Start command received from user {update.effective_user.id} with args: {context.args}")

    if context.args and context.args[0].startswith('wallet_'):
        user_id = update.effective_user.id
        try:
            target_chat_id = int(context.args[0].split('_')[1])
            context.user_data['target_chat_id'] = target_chat_id
            logging.info(f"Wallet flow initiated for user {user_id} and chat {target_chat_id}")
        except (IndexError, ValueError) as e:
            logging.error(f"Invalid wallet start link from user {user_id}: {e}")
            await update.message.reply_text("Invalid start link. Please use the button from a group chat.")
            return ConversationHandler.END

        try:
            # Get only group-specific wallet, don't fall back to global
            wallet_data = await asyncio.to_thread(get_group_specific_wallet, target_chat_id, user_id)
            chat_info = await context.bot.get_chat(target_chat_id)
            chat_name = chat_info.title or f"Group (ID: {target_chat_id})"

            if wallet_data:
                await update.message.reply_text(
                    f"You have a wallet submitted for *{escape_markdown(chat_name)}*:\n\n`{wallet_data['wallet_address']}`\n\nTo replace it, simply reply with your new wallet address\\. To keep it, type /cancel\\.",
                    parse_mode='MarkdownV2'
                )
                logging.info(f"Existing wallet found for user {user_id} in chat {target_chat_id}")
            else:
                await update.message.reply_text(
                    f"Please reply to this message with your wallet address to submit it for the group: *{escape_markdown(chat_name)}*",
                    parse_mode='MarkdownV2'
                )
                logging.info(f"No existing wallet found for user {user_id} in chat {target_chat_id}, awaiting submission")
            return AWAITING_WALLET
        except Exception as e:
            logging.error(f"Error in wallet flow for user {user_id}: {e}")
            await update.message.reply_text("There was an error accessing the group information. Please try again from the group chat.")
            return ConversationHandler.END

    help_text = (
        "Hello! I'm an all-in-one community management and leaderboard bot. Here's what I can do:\n\n"
        "<b>🤖 AI Analysis</b>\n"
        "/summarize &lt;number&gt; - Get an AI summary of the last number of messages.\n"
        "/bestof &lt;number&gt; - See the best messages from the last number of posts.\n"
        "/vibecheck &lt;number&gt; - Check the group's vibe on the last number of messages.\n"
        "<i>You can add a topic to any of the above, like /summarize 500 bitcoin</i>\n\n"
        "<b>📊 Stats & Fun</b>\n"
        "/score - Get a detailed, AI-integrated contribution leaderboard (admin).\n"
        "/publicscore - Show a simple leaderboard in the chat (admin).\n"
        "/mystats - See your personal stats for this group.\n"
        "/mybadges - View the badges you've earned.\n"
        "/allbadges - See all available badges.\n"
        "/copypasta - Create a copypasta based on your message history.\n\n"
        "<b>💰 Crypto</b>\n"
        "/price &lt;symbol&gt; - Look up a cryptocurrency price (including SUI ecosystem tokens like SUI, DEEP, WAL, NS).\n"
        "/airdrop &lt;count&gt; &lt;amount&gt; - Airdrop tokens to top scorers (admin, reply to /score).\n"
        "/settoken &lt;coin_type&gt; - Set the airdrop token for this group (admin).\n\n"
        "<b>🗓️ Group Management</b>\n"
        "/calendar - Manage the group's event calendar (admin).\n"
        "/events - List all upcoming events.\n"
        "/wallet - Submit or check your wallet address.\n"
        "/setwelcome on|off - Toggle welcome messages (admin).\n"
        "/settimezone - Set the timezone for event announcements (admin).\n"
    )
    await update.message.reply_text(help_text + FOOTER_HTML, parse_mode='HTML')
    return ConversationHandler.END


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    start_date, end_date = None, None
    score_invalid_format = "Invalid format. Try \"# messages\", \"# days\", or \"MM/DD/YYYY-MM/DD/YYYY\""

    # Check for message count format
    if len(context.args) >= 2 and context.args[1].lower().startswith('message'):
        try:
            count = int(context.args[0])
            all_messages = db.get(_get_messages_key(update.effective_chat.id), [])
            messages = all_messages[-count:]
            if messages:
                start_date = datetime.datetime.fromisoformat(messages[0]['date'])
                end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
        except (ValueError, IndexError):
            await update.message.reply_text(score_invalid_format)
            return
    else: # Fallback to date-based format
        start_date, end_date, _ = await _parse_date_range(context.args)

    if not start_date or not end_date:
        await update.message.reply_text(score_invalid_format)
        return

    user_id = update.effective_user.id
    context.user_data['score_params'] = {'start_date': start_date, 'end_date': end_date, 'start_str': start_date.strftime("%m/%d/%Y"), 'end_str': end_date.strftime("%m/%d/%Y"), 'export': len(context.args) > 2 and context.args[2].lower() == "export", 'chat_id': update.effective_chat.id}
    keyboard = [[InlineKeyboardButton("📩 Send to Private Chat", callback_data=f"score_private_{user_id}")], [InlineKeyboardButton("📢 Broadcast in Group", callback_data=f"score_broadcast_{user_id}")]]
    await update.message.reply_text(f"📊 **Leaderboard for {context.user_data['score_params']['start_str']} → {context.user_data['score_params']['end_str']}**\n\nWhere would you like to receive the result?", reply_markup=InlineKeyboardMarkup(keyboard))

async def public_score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    start_date, end_date = None, None
    score_invalid_format = "Invalid format. Try \"# messages\", \"# days\", or \"MM/DD/YYYY-MM/DD/YYYY\""

    # Check for message count format
    if len(context.args) >= 2 and context.args[1].lower().startswith('message'):
        try:
            count = int(context.args[0])
            all_messages = db.get(_get_messages_key(update.effective_chat.id), [])
            messages = all_messages[-count:]
            if messages:
                start_date = datetime.datetime.fromisoformat(messages[0]['date'])
                end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
        except (ValueError, IndexError):
            await update.message.reply_text(score_invalid_format)
            return
    else:  # Fallback to date-based format
        start_date, end_date, _ = await _parse_date_range(context.args)

    if not start_date or not end_date:
        await update.message.reply_text(score_invalid_format)
        return

    await update.message.reply_text("🔍 Generating public score...")
    result, error = await generate_leaderboard(context, update.effective_chat.id, start_date, end_date, start_date.strftime("%m/%d/%Y"), end_date.strftime("%m/%d/%Y"))
    if error: await update.message.reply_text(f"❌ {error}"); return
    leaderboard_data = result[2]
    public_text = f"🏆 <b>Public Score</b> ({start_date:%m/%d/%Y} → {end_date:%m/%d/%Y})\n\n<pre>"
    public_text += "Rank | User                 | Total Score\n" + "-" * 40 + "\n"
    for idx, (uname, met, _, _) in enumerate(leaderboard_data[:20], 1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else "  "
        safe_uname = html.escape(uname[:19])
        public_text += f"{medal}{idx:2d} | {safe_uname:<19} | {met['total']:7.1f}\n"
    public_text += "</pre>"
    await update.message.reply_text(public_text + FOOTER_HTML, parse_mode=ParseMode.HTML)

async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates an AI-powered summary of recent chat activity."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)
        return

    try:
        count = int(context.args[0])
        topic = " ".join(context.args[1:]) if len(context.args) > 1 else None

        await update.message.reply_text("Summarizing conversation, this may take a moment...")

        all_messages = db.get(_get_messages_key(update.effective_chat.id), [])
        messages = all_messages[-count:]

        if not messages:
            await update.message.reply_text("No messages found to summarize.")
            return

        summary_title_context = f"the last {len(messages)} messages"

        days = None
        if len(messages) > 1:
            start_date = datetime.datetime.fromisoformat(messages[0]['date'])
            end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
            days = (end_date - start_date).days + 1
        elif len(messages) == 1:
            days = 1

        if topic:
            messages = [msg for msg in messages if topic.lower() in msg['text'].lower()]
            if not messages:
                await update.message.reply_text(f"No messages found about '{topic}' in the specified range.")
                return

        sampled_messages = await asyncio.to_thread(get_proportionally_sampled_messages, messages)

        if len(sampled_messages) < len(messages):
            await update.message.reply_text(f"⚠️ Your request was too large. Summarizing a representative sample of messages.")

        formatter = lambda msg: f"[{datetime.datetime.fromisoformat(msg['date']).strftime('%m/%d/%Y')}] @{msg['username']}: {msg['text']}"
        transcript = build_safe_transcript(sampled_messages, formatter)

        summary = await asyncio.to_thread(summarize_chat_history, transcript, days, topic)

        title = f"<b>Summary for {summary_title_context} on '{topic}':</b>\n\n" if topic else f"<b>Summary for {summary_title_context}:</b>\n\n"
        await update.message.reply_text(title + summary + FOOTER_HTML, parse_mode=ParseMode.HTML)

    except (ValueError, IndexError):
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)

async def bestof_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a 'Best Of' digest of recent messages."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)
        return

    try:
        count = int(context.args[0])

        await update.message.reply_text("Curating the best messages, please wait...")

        all_messages = db.get(_get_messages_key(update.effective_chat.id), [])
        messages = all_messages[-count:]

        if not messages:
            await update.message.reply_text("No messages found to create a digest from.")
            return

        title_context = f"the last {len(messages)} messages"
        days = None
        if len(messages) > 1:
            start_date = datetime.datetime.fromisoformat(messages[0]['date'])
            end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
            days = (end_date - start_date).days + 1
        elif len(messages) == 1:
            days = 1

        sampled_messages = await asyncio.to_thread(get_proportionally_sampled_messages, messages)

        if len(sampled_messages) < len(messages):
            await update.message.reply_text(f"⚠️ Your request was too large. Curating from a representative sample of messages.")

        formatter = lambda msg: f"@{msg['username']}: {msg['text']}"
        transcript = build_safe_transcript(sampled_messages, formatter)

        best_of_digest = await asyncio.to_thread(get_best_of_messages, transcript, days)
        best_of_digest = best_of_digest.replace(' "</blockquote>', '</blockquote>')

        await update.message.reply_text(f"🏆 <b>Best of {title_context}:</b>\n\n{best_of_digest}" + FOOTER_HTML, parse_mode=ParseMode.HTML)

    except (ValueError, IndexError):
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)


async def vibecheck_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyzes the sentiment of recent chat activity."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)
        return

    try:
        count = int(context.args[0])
        topic = " ".join(context.args[1:]) if len(context.args) > 1 else None

        await update.message.reply_text("Checking the vibe, please wait...")

        all_messages = db.get(_get_messages_key(update.effective_chat.id), [])
        messages = all_messages[-count:]

        if not messages:
            await update.message.reply_text("No messages found to analyze.")
            return

        title_context = f"the last {len(messages)} messages"

        if topic:
            messages = [msg for msg in messages if topic.lower() in msg['text'].lower()]
            if not messages:
                await update.message.reply_text(f"No messages found about '{topic}' in the specified range.")
                return

        sampled_messages = await asyncio.to_thread(get_proportionally_sampled_messages, messages)

        if len(sampled_messages) < len(messages):
            await update.message.reply_text(f"⚠️ Your request was too large. Analyzing a representative sample of messages.")

        formatter = lambda msg: f"@{msg['username']}: {msg['text']}"
        transcript = build_safe_transcript(sampled_messages, formatter)

        vibe_data = await asyncio.to_thread(get_vibe_check, transcript, topic)

        if not vibe_data:
            await update.message.reply_text("Sorry, I couldn't get a vibe check at this time.")
            return

        key_messages_html = "".join([f"<blockquote>{html.escape(msg)}</blockquote>" for msg in vibe_data.get("key_messages", [])])

        sentiment_emoji = {
            "Mega-bullish": "🚀", "Bullish": "📈", "Neutral": "😐",
            "Bearish": "📉", "Mega-bearish": "💀"
        }.get(vibe_data.get("sentiment"), "")

        topic_str = f"on <b>{html.escape(topic)}</b>" if topic else ""
        response_text = (
            f"📊 <b>Vibe Check for {title_context} {topic_str}</b>\n\n"
            f"<b>Key Messages:</b>\n{key_messages_html}\n"
            f"<b>Sentiment:</b> {vibe_data.get('sentiment')} {sentiment_emoji}\n\n"
            f"<i>{html.escape(vibe_data.get('summary', ''))}</i>"
        )

        await update.message.reply_text(response_text + FOOTER_HTML, parse_mode=ParseMode.HTML)

    except (ValueError, IndexError):
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)


async def copypasta_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates a copypasta based on the user's message history."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    await update.message.reply_text("Digging through your post history to get your essence...")

    all_messages = db.get(_get_messages_key(chat_id), [])
    user_messages = [msg for msg in all_messages if msg['user_id'] == user_id][-200:]

    if len(user_messages) < 10:
        await update.message.reply_text("I don't have enough of your message history to create a copypasta yet. Keep chatting!")
        return

    transcript = "\n".join([msg['text'] for msg in user_messages])
    copypasta = await asyncio.to_thread(generate_copypasta, transcript)

    await update.message.reply_text(copypasta)


async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to toggle welcome messages on or off."""
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    if not context.args or context.args[0].lower() not in ('on', 'off'):
        await update.message.reply_text("Usage: /setwelcome on or /setwelcome off")
        return

    enabled = context.args[0].lower() == 'on'
    db[_get_welcome_key(update.effective_chat.id)] = enabled
    status = "enabled ✅" if enabled else "disabled ❌"
    await update.message.reply_text(f"Welcome messages have been {status}.")


async def settoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to set the airdrop token type for the group."""
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    if not context.args:
        current_token = db.get(_get_airdrop_token_key(update.effective_chat.id), DEFAULT_SUI_COIN_TYPE)
        await update.message.reply_text(
            f"Current airdrop token: <code>{html.escape(current_token)}</code>\n\n"
            f"Usage: /settoken &lt;coin_type&gt;\n"
            f"Example: /settoken 0x2::sui::SUI",
            parse_mode=ParseMode.HTML
        )
        return

    coin_type = context.args[0].strip()
    db[_get_airdrop_token_key(update.effective_chat.id)] = coin_type
    await update.message.reply_text(
        f"✅ Airdrop token set to: <code>{html.escape(coin_type)}</code>",
        parse_mode=ParseMode.HTML
    )


async def airdrop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Airdrops tokens to top users from a /score leaderboard via SUI blockchain.

    Must be used as a reply to a leaderboard message generated by /score.
    """
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: Reply to a /score leaderboard message with:\n"
            "/airdrop &lt;count&gt; &lt;amount&gt;\n\n"
            "Example:\n"
            "1. Run /score 30 days\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /airdrop 10 1000000000\n\n"
            "<i>Count = number of top leaderboard users to receive tokens.\n"
            "Amount = token amount per user in smallest unit (e.g. 1000000000 MIST = 1 SUI).</i>",
            parse_mode=ParseMode.HTML
        )
        return

    try:
        count = int(context.args[0])
        amount = int(context.args[1])
    except ValueError:
        await update.message.reply_text("❌ Both count and amount must be whole numbers.")
        return

    if count < 1 or amount < 1:
        await update.message.reply_text("❌ Count and amount must be positive numbers.")
        return

    # Must be a reply to a leaderboard message
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Please reply to a /score leaderboard message to use /airdrop.\n\n"
            "1. Run /score (e.g. /score 30 days)\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /airdrop &lt;count&gt; &lt;amount&gt;",
            parse_mode=ParseMode.HTML
        )
        return

    replied_msg_id = update.message.reply_to_message.message_id
    leaderboard_messages = context.chat_data.get('leaderboard_messages', {})
    leaderboard = leaderboard_messages.get(replied_msg_id)

    if not leaderboard:
        await update.message.reply_text("❌ The replied message is not a recognized leaderboard. Please reply to a leaderboard broadcasted by /score.")
        return

    if not os.environ.get("SUI_PRIVATE_KEY"):
        await update.message.reply_text("❌ SUI airdrop is not configured. The bot operator must set the SUI_PRIVATE_KEY environment variable.")
        return

    chat_id = update.effective_chat.id
    coin_type = db.get(_get_airdrop_token_key(chat_id), DEFAULT_SUI_COIN_TYPE)

    # Take the top N users from the score-based leaderboard
    # Leaderboard entries are tuples: (username, metrics_dict, message_count, user_id)
    top_entries = leaderboard[:count]
    if not top_entries:
        await update.message.reply_text("❌ No eligible users found in the leaderboard.")
        return

    await update.message.reply_text(
        f"🪂 Starting airdrop of <code>{html.escape(coin_type)}</code> to the top {len(top_entries)} users from the leaderboard...",
        parse_mode=ParseMode.HTML
    )

    results = []
    success_count = 0
    skip_count = 0
    fail_count = 0

    for username, _, msg_count, user_id_str in top_entries:
        safe_username = html.escape(username)

        # Look up wallet
        wallet_data = await asyncio.to_thread(get_wallet, chat_id, int(user_id_str))
        if not wallet_data or not wallet_data.get("wallet_address"):
            results.append(f"⏭️ @{safe_username}: No wallet registered — skipped")
            skip_count += 1
            continue

        wallet_address = wallet_data["wallet_address"]

        try:
            tx_result = await sui_transfer_token(wallet_address, amount, coin_type)
            tx_digest = tx_result.get("digest", "unknown")
            results.append(f"✅ @{safe_username}: <code>{tx_digest}</code>")
            success_count += 1
        except Exception as e:
            logging.error(f"Airdrop transfer failed for {username} ({wallet_address}): {e}")
            results.append(f"❌ @{safe_username}: {html.escape(str(e)[:60])}")
            fail_count += 1

    results_text = "\n".join(results)
    summary = (
        f"🪂 <b>Airdrop Complete</b>\n\n"
        f"Token: <code>{html.escape(coin_type)}</code>\n"
        f"Amount per user: {amount}\n"
        f"✅ Sent: {success_count} | ⏭️ Skipped (no wallet): {skip_count} | ❌ Failed: {fail_count}\n\n"
        f"<b>Details:</b>\n{results_text}"
    )
    await update.message.reply_text(summary + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fetches the current price of a cryptocurrency."""
    if not context.args:
        await update.message.reply_text(
            "Usage: /price &lt;symbol&gt;\n"
            "Example: /price BTC, /price SUI, or /price DEEP",
            parse_mode=ParseMode.HTML
        )
        return

    symbol = context.args[0].strip()
    await update.message.reply_text(f"🔍 Looking up {html.escape(symbol.upper())}...", parse_mode=ParseMode.HTML)

    price_data = await fetch_crypto_price(symbol)

    if not price_data:
        await update.message.reply_text(
            f"❌ Could not find price data for <b>{html.escape(symbol.upper())}</b>. "
            "Try the full name (e.g., 'bitcoin') or symbol (e.g., 'BTC', 'SUI', 'DEEP', 'WAL', 'NS').",
            parse_mode=ParseMode.HTML
        )
        return

    change = price_data["change_24h"] or 0
    change_emoji = "🟢" if change >= 0 else "🔴"
    price = price_data['price'] or 0
    price_fmt = f"${price:,.2f}" if price >= 1 else f"${price:.6f}"

    response = (
        f"💰 <b>{html.escape(price_data['name'])} ({html.escape(price_data['symbol'])})</b>\n\n"
        f"<b>Price:</b> {price_fmt}\n"
        f"{change_emoji} <b>24h Change:</b> {change:+.2f}%\n"
        f"📊 <b>Market Cap:</b> {format_large_number(price_data['market_cap'])}\n"
        f"💎 <b>24h Volume:</b> {format_large_number(price_data['volume_24h'])}"
    )

    await update.message.reply_text(response + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the help message with all available commands."""
    help_text = (
        "Hello! I'm an all-in-one community management and leaderboard bot. Here's what I can do:\n\n"
        "<b>🤖 AI Analysis</b>\n"
        "/summarize &lt;number&gt; - Get an AI summary of the last number of messages.\n"
        "/bestof &lt;number&gt; - See the best messages from the last number of posts.\n"
        "/vibecheck &lt;number&gt; - Check the group's vibe on the last number of messages.\n"
        "<i>You can add a topic to any of the above, like /summarize 500 bitcoin</i>\n\n"
        "<b>📊 Stats & Fun</b>\n"
        "/score - Get a detailed, AI-integrated contribution leaderboard (admin).\n"
        "/publicscore - Show a simple leaderboard in the chat (admin).\n"
        "/mystats - See your personal stats for this group.\n"
        "/mybadges - View the badges you've earned.\n"
        "/allbadges - See all available badges.\n"
        "/copypasta - Create a copypasta based on your message history.\n\n"
        "<b>💰 Crypto</b>\n"
        "/price &lt;symbol&gt; - Look up a cryptocurrency price (including SUI ecosystem tokens like SUI, DEEP, WAL, NS).\n"
        "/airdrop &lt;count&gt; &lt;amount&gt; - Airdrop tokens to top scorers (admin, reply to /score).\n"
        "/settoken &lt;coin_type&gt; - Set the airdrop token for this group (admin).\n\n"
        "<b>🗓️ Group Management</b>\n"
        "/calendar - Manage the group's event calendar (admin).\n"
        "/events - List all upcoming events.\n"
        "/wallet - Submit or check your wallet address.\n"
        "/setwelcome on|off - Toggle welcome messages (admin).\n"
        "/settimezone - Set the timezone for event announcements (admin).\n"
    )
    await update.message.reply_text(help_text + FOOTER_HTML, parse_mode='HTML')


async def mybadges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the user's earned badges."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    badges_key = _get_badges_key(chat_id, user_id)
    user_badges = db.get(badges_key, [])

    if not user_badges:
        await update.message.reply_text("You haven't earned any badges yet. Keep participating!")
        return

    message = "🏅 <b>Your Badges</b>\n\n"
    for badge_id in user_badges:
        badge = BADGES.get(badge_id)
        if badge:
            message += f"{badge['emoji']} <b>{badge['name']}</b>: {badge['description']}\n"

    await update.message.reply_text(message + FOOTER_HTML, parse_mode=ParseMode.HTML)

async def allbadges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays all available badges."""
    message = "🏆 <b>All Available Badges</b>\n\n"
    for badge_id, badge in BADGES.items():
        message += f"{badge['emoji']} <b>{badge['name']}</b>: {badge['description']}\n"

    await update.message.reply_text(message + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = await asyncio.to_thread(get_chat_stats, update.effective_chat.id)
    if stats['total_messages'] == 0: await update.message.reply_text("No messages stored yet."); return

    top_users_text = ""
    if stats['top_users']:
        top_users_text += "<b>Top Contributors:</b>\n"
        for i, (u, c) in enumerate(stats['top_users'], 1): 
            safe_username = html.escape(u)
            top_users_text += f"{i}. {safe_username}: {c} messages\n"

    stats_text = (
        f"📈 <b>Chat Statistics</b>\n\n"
        f"💬 Total messages: {stats['total_messages']}\n"
        f"👥 Active users: {stats['user_count']}\n"
    )
    if stats['oldest_date']: 
        stats_text += f"📅 Date range: {stats['oldest_date']:%m/%d/%Y} - {stats['newest_date']:%m/%d/%Y}\n\n"

    await update.message.reply_text(stats_text + top_users_text + FOOTER_HTML, parse_mode=ParseMode.HTML)

async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the user their personal stats in a private message."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    await update.message.reply_text("Checking your stats... I'll send them to you in a private message.")

    # --- OPTIMIZATION: Fetch stats directly, not all messages ---
    chat_id_str = str(chat_id)
    user_id_str = str(user_id)

    # Get this user's message count
    stats_key = _get_user_stats_key(chat_id_str, user_id_str)
    user_stats = db.get(stats_key, {"message_count": 0})
    user_message_count = user_stats["message_count"]

    # Calculate rank efficiently
    rank = -1
    if user_message_count > 0:
        all_stats_keys = db.prefix(f"user_stats:{chat_id_str}:")
        # Create a list of (user_id, count) tuples
        user_counts = []
        for key in all_stats_keys:
            try:
                # Extract user_id from key f"user_stats:{chat_id}:{user_id}"
                key_user_id = int(key.split(":")[-1])
                stats = db.get(key)
                if stats and "message_count" in stats:
                    user_counts.append((key_user_id, stats["message_count"]))
            except (ValueError, IndexError):
                continue # Skip malformed keys

        # Sort by message count (descending)
        sorted_users = sorted(user_counts, key=lambda item: item[1], reverse=True)

        # Find rank
        for i, (uid, _) in enumerate(sorted_users):
            if uid == user_id:
                rank = i + 1
                break

    # Get badge info (this part is already efficient)
    badges_key = _get_badges_key(chat_id, user_id)
    user_badges = db.get(badges_key, [])
    badge_text = ""
    if user_badges:
        badge_text = "<b>Badges Earned:</b> " + " ".join([BADGES[b]['emoji'] for b in user_badges if b in BADGES])
    else:
        badge_text = "<b>Badges Earned:</b> None yet!"

    stats_message = (
        f"📊 <b>Your Stats for this Group</b>\n\n"
        f"<b>Total Messages:</b> {user_message_count}\n"
        f"<b>All-Time Rank:</b> {'N/A' if rank == -1 else f'#{rank}'}\n"
        f"{badge_text}"
    )

    try:
        await context.bot.send_message(chat_id=user_id, text=stats_message + FOOTER_HTML, parse_mode=ParseMode.HTML)
    except Exception as e:
        logging.error(f"Failed to send private stats to {user_id}: {e}")
        await update.message.reply_text("I couldn't send you a private message. Please start a chat with me first!")


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return ConversationHandler.END
    now = datetime.datetime.now()
    await update.message.reply_text("📅 <b>Event Calendar</b>\nClick a date to add or view an event.", reply_markup=generate_calendar_keyboard(now.year, now.month, update.effective_chat.id), parse_mode=ParseMode.HTML)
    return SELECTING_ACTION

async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    prefix = f"event:{chat_id}:"
    event_keys = db.prefix(prefix)
    upcoming_events = []
    today = datetime.date.today()
    for key in event_keys:
        try:
            date_str = key.split(":")[-1]
            event_date = datetime.date.fromisoformat(date_str)
            if event_date >= today:
                event_text = db.get(key)
                if event_text:
                    upcoming_events.append((event_date, event_text))
        except (ValueError, IndexError):
            continue
    upcoming_events.sort(key=lambda x: x[0])
    if not upcoming_events:
        await update.message.reply_text("🗓️ There are no upcoming events scheduled.")
        return
    message = "🗓️ <b>Upcoming Events</b>\n\n"
    for event_date, event_text in upcoming_events:
        message += f"<b>{event_date.strftime('%B %d, %Y')}</b>\n- {html.escape(event_text)}\n\n"
    await update.message.reply_text(message + FOOTER_HTML, parse_mode=ParseMode.HTML)

async def set_timezone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sets the timezone for the chat."""
    chat_member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can use this command.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /settimezone <Timezone>\nExample: /settimezone America/New_York\nFind timezones here: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones")
        return

    tz_name = context.args[0]
    try:
        pytz.timezone(tz_name)
        db[_get_timezone_key(update.effective_chat.id)] = tz_name
        await update.message.reply_text(f"✅ Timezone successfully set to {tz_name}.")
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text(f"❌ Unknown timezone: {tz_name}. Please use a valid timezone name.")

async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /wallet command to initiate wallet submission/checking."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Check if this is a private message
    if update.effective_chat.type == 'private':
        await update.message.reply_text("Please use this command in a group chat to submit your wallet for that group.")
        return

    try:
        bot_username = context.bot.username
        if not bot_username:
            # Fallback to get bot info if username is not available
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

        keyboard = [[InlineKeyboardButton("🔒 Submit or Check Wallet", url=f"https://t.me/{bot_username}?start=wallet_{chat_id}")]]
        await update.message.reply_text(
            "To protect your privacy, please click the button below to submit or check your wallet in a private chat with me.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        logging.info(f"Wallet command used by user {user_id} in chat {chat_id}")
    except Exception as e:
        logging.error(f"Error in wallet command for user {user_id} in chat {chat_id}: {e}")
        await update.message.reply_text("There was an error generating the wallet submission link. Please try again.")

async def receive_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives and validates the wallet address from the user."""
    wallet_address = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    logging.info(f"Received wallet address from user {user_id}: {wallet_address[:10]}...")

    # Removed validation to allow any string format
    if not wallet_address:
        await update.message.reply_text("❌ Wallet address cannot be empty. Please try again or type /cancel.")
        return AWAITING_WALLET

    target_chat_id = context.user_data.get('target_chat_id')
    if not target_chat_id:
        await update.message.reply_text("Error: Could not find the original group. Please start the process again from the group chat.")
        logging.error(f"Missing target_chat_id for user {user_id}")
        return ConversationHandler.END

    try:
        success = await asyncio.to_thread(store_wallet, target_chat_id, user_id, username, wallet_address)
        chat_info = await context.bot.get_chat(target_chat_id)
        chat_name = chat_info.title or f"Group (ID: {target_chat_id})"
        if success:
            await update.message.reply_text(f"✅ Wallet address `{wallet_address}` successfully submitted for *{escape_markdown(chat_name)}*\\.", parse_mode='MarkdownV2')
            logging.info(f"Wallet successfully stored for user {user_id} in chat {target_chat_id}")
        else:
            await update.message.reply_text("❌ Error storing wallet address. Please try again.")
            logging.error(f"Wallet storage failed for user {user_id}")
    except Exception as e:
        logging.error(f"Error in wallet submission for '{username}' (ID: {user_id}): {e}")
        await update.message.reply_text("❌ Error storing wallet address. Please try again.")

    context.user_data.pop('target_chat_id', None)
    return ConversationHandler.END


async def handle_calendar_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query, data, chat_id = update.callback_query, update.callback_query.data, update.callback_query.message.chat_id
    user = query.from_user
    chat_member = await context.bot.get_chat_member(chat_id, user.id)
    if chat_member.status not in ['administrator', 'creator']:
        await query.answer("❌ Only administrators can interact with the calendar.", show_alert=True)
        return SELECTING_ACTION

    await query.answer()
    try:
        parts = data.split("_")
        action = parts[1]

        if action == "nav":
            _, _, year, month = parts
            await query.edit_message_text(text="📅 <b>Event Calendar</b>\nClick a date to add or view an event.", reply_markup=generate_calendar_keyboard(int(year), int(month), chat_id), parse_mode=ParseMode.HTML)
            return SELECTING_ACTION
        elif action == "day":
            _, _, year, month, day = parts
            selected_date = datetime.date(int(year), int(month), int(day))
            event_text = db.get(_get_event_key(chat_id, selected_date))
            context.chat_data['selected_date'] = selected_date.isoformat()
            keyboard = [[InlineKeyboardButton("Delete Event", callback_data=f"cal_delete_{selected_date.isoformat()}"), InlineKeyboardButton("Back to Calendar", callback_data="cal_back")]] if event_text else []
            msg = f"🗓️ <b>Event on {selected_date:%B %d, %Y}</b>\n\n{event_text}\n\nReply to overwrite." if event_text else f"📝 No event for {selected_date:%B %d, %Y}. Reply with event text."
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, parse_mode=ParseMode.HTML)
            return AWAITING_EVENT_TEXT
        elif action == "delete":
            date_str = parts[2]
            del db[_get_event_key(chat_id, datetime.date.fromisoformat(date_str))]
            await query.answer("Event deleted!", show_alert=True)

        elif action == "close":
            await query.message.delete()
            return ConversationHandler.END

        now = datetime.datetime.now()
        await query.edit_message_text("📅 <b>Event Calendar</b>", reply_markup=generate_calendar_keyboard(now.year, now.month, chat_id), parse_mode=ParseMode.HTML)
        return SELECTING_ACTION
    except Exception as e:
        logging.error(f"Error in calendar interaction '{data}': {e}")
        await query.answer("An error occurred.", show_alert=True)
        return SELECTING_ACTION

async def handle_event_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    if chat_member.status not in ['administrator', 'creator']:
        await update.message.reply_text("❌ Only administrators can add events.")
        context.chat_data.pop('selected_date', None)
        return SELECTING_ACTION

    selected_date_iso = context.chat_data.pop('selected_date', None)
    if not selected_date_iso:
        await update.message.reply_text("Action timed out. Please select a date again.")
        return ConversationHandler.END
    selected_date = datetime.date.fromisoformat(selected_date_iso)
    db[_get_event_key(chat_id, selected_date)] = update.message.text
    await update.message.reply_text(f"✅ Event for {selected_date:%B %d, %Y} scheduled!")
    now = datetime.datetime.now()
    await update.message.reply_text("📅 <b>Event Calendar</b>", reply_markup=generate_calendar_keyboard(now.year, now.month, chat_id), parse_mode=ParseMode.HTML)
    return SELECTING_ACTION

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Action cancelled.')
    context.user_data.pop('target_chat_id', None)
    return ConversationHandler.END

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, data, user_id = update.callback_query, update.callback_query.data, update.callback_query.from_user.id
    await query.answer()

    if data.startswith("score_"):
        if user_id != int(data.split("_")[-1]):
            await query.answer("❌ Only the requester can use these buttons.", show_alert=True)
            return
        score_params = context.user_data.get('score_params')
        if not score_params:
            await query.edit_message_text("❌ Session expired. Please run /score again.")
            return
        await query.edit_message_text("🔍 Generating leaderboard...")
        try:
            result, error = await generate_leaderboard(context, **score_params)
            if error: await query.edit_message_text(f"❌ {error}"); return
            leaderboard_text, csv_data, raw_data = result
            context.user_data['leaderboard_data'] = (leaderboard_text, csv_data, raw_data)
            is_private = data.startswith("score_private_")
            target_chat_id = user_id if is_private else score_params['chat_id']
            confirm_text = "✅ Leaderboard sent to your private chat!" if is_private else "✅ Leaderboard broadcasted!"
            keyboard = [[InlineKeyboardButton("📄 Download CSV", callback_data=f"export_csv_{user_id}")]]
            sent_msg = await context.bot.send_message(target_chat_id, leaderboard_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            # Store leaderboard data keyed by message ID so /airdrop can reference it via reply
            if not is_private:
                if 'leaderboard_messages' not in context.chat_data:
                    context.chat_data['leaderboard_messages'] = {}
                context.chat_data['leaderboard_messages'][sent_msg.message_id] = raw_data
            if csv_data and score_params['export']:
                await context.bot.send_document(target_chat_id, document=io.BytesIO(csv_data), filename="leaderboard.csv")
            await query.edit_message_text(confirm_text)
        except Exception as e:
            logging.error(f"Error in leaderboard generation: {e}")
            await query.edit_message_text("⚠️ An error occurred during generation.")

    elif data.startswith("export_csv_"):
        if user_id != int(data.split("_")[-1]):
            await query.answer("❌ Only the requester can download the CSV.", show_alert=True)
            return

        leaderboard_data = context.user_data.get('leaderboard_data')
        if not leaderboard_data:
            await query.answer("❌ Leaderboard data has expired. Please run /score again.", show_alert=True)
            return

        _, csv_data, raw_data = leaderboard_data

        if not csv_data:
            score_params = context.user_data.get('score_params', {})
            chat_id = score_params.get('chat_id', query.message.chat.id)
            csv_data = await generate_csv_from_leaderboard(raw_data, chat_id)

        if csv_data:
            await context.bot.send_document(chat_id=user_id, document=io.BytesIO(csv_data), filename="leaderboard_export.csv", caption="Here is your leaderboard CSV export.")
            await query.answer("✅ CSV sent to your private chat.", show_alert=True)
        else:
            await query.answer("❌ Could not generate CSV data.", show_alert=True)

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception while handling an update:", exc_info=context.error)

async def setup_bot_commands(application):
    commands = [
        BotCommand("start", "Show help and command list"),
        BotCommand("help", "Show help and command list"),
        BotCommand("score", "Generate detailed leaderboard (admin only)"),
        BotCommand("publicscore", "Display a simple leaderboard (admin only)"),
        BotCommand("summarize", "AI summary: /summarize <#> [topic]"),
        BotCommand("bestof", "AI digest: /bestof <#>"),
        BotCommand("vibecheck", "AI sentiment: /vibecheck <#> [topic]"),
        BotCommand("copypasta", "Generate a copypasta of your history"),
        BotCommand("price", "Crypto price: /price <symbol>"),
        BotCommand("airdrop", "Airdrop tokens: reply to /score with /airdrop <count> <amount>"),
        BotCommand("settoken", "Set airdrop token (admin)"),
        BotCommand("mybadges", "View your earned badges"),
        BotCommand("allbadges", "See all available badges"),
        BotCommand("mystats", "View your personal stats"),
        BotCommand("calendar", "View and manage events (admin only)"),
        BotCommand("events", "List all upcoming events"),
        BotCommand("settimezone", "Set timezone for announcements (admin only)"),
        BotCommand("setwelcome", "Toggle welcome messages (admin)"),
        BotCommand("stats", "Show chat statistics"),
        BotCommand("wallet", "Submit or check your wallet address"),
        BotCommand("cancel", "Cancel current operation")
    ]
    await application.bot.set_my_commands(commands)

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logging.error("TELEGRAM_BOT_TOKEN not set")
        return

    # Set custom timeouts to make the bot more resilient to network issues
    request = HTTPXRequest(read_timeout=30.0, connect_timeout=30.0)
    application = Application.builder().token(token).request(request).build()

    job_queue = application.job_queue
    job_queue.run_repeating(check_and_announce_events, interval=datetime.timedelta(minutes=30), first=10)


    calendar_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('calendar', calendar_command)],
        states={
            SELECTING_ACTION: [CallbackQueryHandler(handle_calendar_interaction, pattern="^cal_")],
            AWAITING_EVENT_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_event_text)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False,
        conversation_timeout=300
    )

    wallet_conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start_command)],
        states={
            AWAITING_WALLET: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_wallet_address)],
        },
        fallbacks=[CommandHandler('cancel', cancel_conversation)],
        per_message=False,
        conversation_timeout=300
    )

    application.add_handler(calendar_conv_handler)
    application.add_handler(wallet_conv_handler)

    application.add_handler(CommandHandler("score", score_command))
    application.add_handler(CommandHandler("publicscore", public_score_command))
    application.add_handler(CommandHandler("summarize", summarize_command))
    application.add_handler(CommandHandler("bestof", bestof_command))
    application.add_handler(CommandHandler("vibecheck", vibecheck_command))
    application.add_handler(CommandHandler("copypasta", copypasta_command))
    application.add_handler(CommandHandler("price", price_command))
    application.add_handler(CommandHandler("airdrop", airdrop_command))
    application.add_handler(CommandHandler("settoken", settoken_command))
    application.add_handler(CommandHandler("setwelcome", setwelcome_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mybadges", mybadges_command))
    application.add_handler(CommandHandler("allbadges", allbadges_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("settimezone", set_timezone_command))
    application.add_handler(CommandHandler("wallet", wallet_command))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(score_|export_csv_)"))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))
    application.add_error_handler(error_handler)
    application.post_init = setup_bot_commands

    def handle_signal(signum, frame):
        logging.info("Shutdown signal received, stopping bot.")
        application.stop()
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
