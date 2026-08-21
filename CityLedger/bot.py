import os
import re
import math
import random
import datetime
import calendar
import asyncio
import logging
import io
import csv
import html
import json
import time
import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import quote
import pytz
from ai_services import analyze_user_messages, summarize_chat_history, get_best_of_messages, get_vibe_check, generate_copypasta
from buy_tracker import canonicalize_sui_type, detect_buy
from db import db
from http_clients import close_shared_async_client, get_shared_async_client
from sui_service import (
    DEFAULT_SUI_GRPC_URL,
    close_sui_service,
    get_sui_service,
    parse_grpc_headers,
)
from sui_utils import (
    DEFAULT_SUI_COIN_DECIMALS,
    DEFAULT_SUI_COIN_TYPE as SUI_DEFAULT_COIN_TYPE,
    build_airdrop_balance_requirements,
    ENCRYPTION_KEY_ENV,
    derive_sui_address,
    encrypt_private_key,
    format_token_amount,
    normalize_sui_private_key,
    parse_token_amount,
    resolve_airdrop_sender_config,
)
from raffle_utils import RAFFLE_MAX_RANK, select_weighted_raffle_winner
from name_guard import NameGuardMatch, evaluate_name_guard
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    ChatPermissions,
)
from telegram.constants import ChatType, ParseMode
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
from telegram_utils import HELP_TEXT, normalize_wallet_address, require_admin, sanitize_html_for_telegram, user_is_admin

# Configure logging for debugging.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Conversation states
SELECTING_ACTION, AWAITING_EVENT_TEXT, AWAITING_WALLET, AWAITING_AIRDROP_PRIVATE_KEY = range(4)


# --- Constants ---
FOOTER_HTML = "\n\n<i>Product of Alpha City (<a href=\"https://app.noodles.fi/coins/0x308fa16c7aead43e3a49a4ff2e76205ba2a12697234f4fe80a2da66515284060::city::CITY\">$CITY</a>)</i>"
MAX_MESSAGES_FOR_SUMMARY = 1000
# Set a safe upper limit for messages to load into memory at once to prevent crashes.
MAX_MESSAGES_TO_PROCESS = 1500
# Maximum message count a user may request for AI commands.
MAX_MESSAGES_INPUT_LIMIT = 5000
INVALID_FORMAT_MESSAGE = "Invalid format. Usage: /command #, or /command # topic. For example /command 100 or /command 500 Bitcoin"


# --- Badge Definitions ---
BADGES = {
    'contributor_100': {'name': 'Contributor', 'emoji': '✍️', 'description': 'Sent over 100 messages.'},
    'hero_500': {'name': 'Hero', 'emoji': '🦸', 'description': 'Sent over 500 messages.'},
    'godlike_1000': {'name': 'God-like', 'emoji': '⚡️', 'description': 'Sent over 1,000 messages.'},
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
DEXSCREENER_API_URL = "https://api.dexscreener.com"
TOKEN_VOLUME_CACHE_TTL = 60
TOKEN_VOLUME_REQUEST_TIMEOUT = 5.0
_token_volume_cache: dict[str, tuple[dict[str, Decimal] | None, float]] = {}

# --- AI Rate Limiting ---
# One AI command per user per group per 60 seconds to prevent API cost abuse.
AI_COOLDOWN_SECONDS = 60
_ai_cooldowns: dict[tuple[int, int], float] = {}  # (user_id, chat_id) -> last_call_monotonic
_AI_RATE_LIMIT_MESSAGE = "⏳ Please wait {remaining:.0f}s before using AI commands again."

# How often (in transfer count) to edit the airdrop progress message.
_AIRDROP_PROGRESS_UPDATE_INTERVAL = 3
_BUYBOT_SEEN_DIGEST_LIMIT = 500
_BUYBOT_CHECKPOINT_BATCH_SIZE = 50
_BUYBOT_MAX_CHECKPOINTS_PER_RUN = 250
_BUYBOT_LIVE_GAP_LOOKBACK = 100
_BUYBOT_POLL_SECONDS = 3
_BUYBOT_EMOJI = "🔥"
_BUYBOT_EMOJI_USD_STEP = Decimal("5")
_BUYBOT_MAX_EMOJIS = 100
_BUYBOT_BUYER_DATE_HISTORY_LIMIT = 32
_MIST_PER_SUI = Decimal("1000000000")

try:
    _BUYBOT_WHALE_USD_THRESHOLD = Decimal(
        os.environ.get("BUYBOT_WHALE_USD_THRESHOLD", "100")
    )
    if not _BUYBOT_WHALE_USD_THRESHOLD.is_finite() or _BUYBOT_WHALE_USD_THRESHOLD <= 0:
        raise InvalidOperation
except (InvalidOperation, TypeError, ValueError):
    logging.warning(
        "Invalid BUYBOT_WHALE_USD_THRESHOLD; falling back to $100."
    )
    _BUYBOT_WHALE_USD_THRESHOLD = Decimal("100")

_BUYBOT_BADGES = {
    "whale": ("🐋", "Whale Buy"),
    "first_time": ("🆕", "First-Time Buyer"),
    "returning": ("💎", "Returning Holder"),
    "three_day_streak": ("🔥", "Three-Day Streak"),
}


def _check_ai_rate_limit(user_id: int, chat_id: int) -> float:
    """Return seconds remaining in cooldown, or 0.0 if the user may proceed."""
    last = _ai_cooldowns.get((user_id, chat_id), 0.0)
    remaining = AI_COOLDOWN_SECONDS - (time.monotonic() - last)
    return max(0.0, remaining)


def _record_ai_rate_limit(user_id: int, chat_id: int) -> None:
    _ai_cooldowns[(user_id, chat_id)] = time.monotonic()


# In-memory set of chat IDs whose messages have already been migrated from the
# legacy blob format.  Avoids a DB round-trip on every stored message.
_migrated_chats: set[int] = set()
_coin_metadata_cache: dict[str, dict | None] = {}

SUI_PRICE_ALIASES = {
    "afsui": "aftermath-staked-sui",
    "blub": "blub",
    "buck": "bucket-protocol-buck-stablecoin",
    "cetus": "cetus-protocol",
    "sui": "sui",
    "deep": "deep",
    "deepbook": "deep",
    "fud": "fud-the-pug",
    "hasui": "haedal-staked-sui",
    "hippo": "sudeng",
    "navx": "navi",
    "wal": "walrus-2",
    "walrus": "walrus-2",
    "ns": "suins-token",
    "sca": "scallop-2",
    "sudeng": "sudeng",
    "suins": "suins-token",
    "turbos": "turbos-finance",
    "vsui": "volo-staked-sui",
    "usdy": "ondo-us-dollar-yield",
    "lbtc": "lombard-staked-btc",
    "mbtc": "merlin-s-seal-btc",
    "fdusd": "first-digital-usd",
    "tbtc": "tbtc",
    "enzobtc": "lorenzo-wrapped-bitcoin",
    "ausd": "agora-dollar",
    "xaum": "matrixdock-gold",
    "magma": "magma-finance",
    "bce": "bitcastle-token",
    "xbtc": "okx-wrapped-btc",
    "truth": "swarm-network",
    "mmt": "momentum-3",
    "stbtc": "lorenzo-stbtc",
    "us": "talus",
    "bluai": "bluwhale",
    "suiusde": "esui-dollar",
    "sbusdt": "sui-bridged-usdt-sui",
    "ethird": "ember-third-eye",
    "alpha": "alpha-fi",
    "sweat": "sweatcoin",
    "ssui": "spring-staked-sui",
    "aia": "aia",
    "blue": "bluefin",
    "take": "overtake",
    "ika": "ika",
    "xagm": "matrixdock-silver",
    "lofi": "lofi-2",
    "esui": "ember-sui",
    "send": "suilend",
    "xmn": "xmoney-2",
    "lwa": "onbuff",
    "miu": "miu-2",
    "tato": "pawtato",
    "memefi": "memefi-2",
    "eearn": "ember-earn",
    "axol": "axol",
    "pans": "pandasui-coin",
    "attn": "attention",
    "alkimi": "alkimi-2",
    "warped": "warped-games",
    "flx": "flowx-finance",
    "but": "bucket-token",
    "manifest": "manifest-3",
    "usdz": "usdz",
    "up": "doubleup",
    "chirp": "chirp-token",
    "aaa": "aaa-cat",
    "suai": "suiai",
    "xsui": "xsui",
    "shr0": "sroomai-dao",
    "musd": "meta-usd",
    "seed": "seed-3",
    "koban": "koban",
    "artfi": "artfi",
    "brat": "brat-2",
    "suijak": "suijak",
    "s": "agent-s",
    "tardi": "tardi",
    "suitrump": "sui-trump",
    "toilet": "toilet-dust",
    "pumpkin": "pumpkin-token",
    "beeg": "beeg-blue-whale",
    "hsui": "suicune-on-sui",
    "suimon": "suimon",
    "suiai": "sui-agents",
    "pigu": "pigu",
    "city": "alpha-city-2",
    "typus": "typus",
    "sonic": "sonic-snipe-bot",
    "pstake": "pstake-finance",
    "wav": "wave",
    "victory": "victory-2",
    "scb": "sacabam",
    "skelsui": "skeleton",
    "suiyan": "super-suiyan",
    "culo": "culosui",
    "pugwif": "pugwifhat",
    "zen": "zenfrogs",
    "sail": "full-sail",
}

GENERAL_PRICE_ALIASES = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "bnb": "binancecoin",
    "sol": "solana",
    "xrp": "ripple",
    "ada": "cardano",
    "doge": "dogecoin",
    "dot": "polkadot",
    "ltc": "litecoin",
    "link": "chainlink",
    "uni": "uniswap",
    "near": "near",
    "matic": "matic-network",
    "pol": "polygon-ecosystem-token",
    "pepe": "pepe",
    "wif": "dogwifhat",
    "bonk": "bonk",
    "ton": "the-open-network",
    "avax": "avalanche-2",
    "shib": "shiba-inu",
    "op": "optimism",
    "arb": "arbitrum",
    "rndr": "render-token",
    "fet": "artificial-superintelligence-alliance",
    "usdc": "usd-coin",
    "usdt": "tether",
    "wbtc": "wrapped-bitcoin",
    "weth": "ethereum",
    "wusdc": "usd-coin",
    "wusdt": "tether",
}

ALL_PRICE_ALIASES = {**SUI_PRICE_ALIASES, **GENERAL_PRICE_ALIASES}

# --- SUI Blockchain ---
SUI_GRPC_URL = os.environ.get("SUI_GRPC_URL", DEFAULT_SUI_GRPC_URL)
DEFAULT_SUI_COIN_TYPE = SUI_DEFAULT_COIN_TYPE
SUI_GAS_BUDGET = int(os.environ.get("SUI_GAS_BUDGET", "50000000"))  # 0.05 SUI
SUI_EXPLORER_TX_URL = os.environ.get("SUI_EXPLORER_TX_URL", "https://suivision.xyz/txblock")
SUI_EXPLORER_ADDRESS_URL = os.environ.get(
    "SUI_EXPLORER_ADDRESS_URL",
    "https://suivision.xyz/account",
)

try:
    SUI_GRPC_HEADERS = parse_grpc_headers(os.environ.get("SUI_GRPC_HEADERS_JSON"))
except ValueError as exc:
    logging.warning(f"{exc} Provider headers will not be sent.")
    SUI_GRPC_HEADERS = {}

try:
    _dex_packages_from_env = json.loads(os.environ.get("SUI_DEX_PACKAGES_JSON", "{}"))
    SUI_DEX_PACKAGES = (
        {str(package): str(label) for package, label in _dex_packages_from_env.items()}
        if isinstance(_dex_packages_from_env, dict)
        else {}
    )
except json.JSONDecodeError:
    logging.warning("SUI_DEX_PACKAGES_JSON is invalid JSON; custom DEX labels are disabled.")
    SUI_DEX_PACKAGES = {}


# --- Helper Functions ---
def escape_markdown(text: str) -> str:
    """Escapes special characters for Telegram's MarkdownV2 parse mode."""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in text)

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


def _get_name_guard_key(chat_id):
    """Returns the database key for the group name-guard toggle."""
    return f"name_guard:{chat_id}"


def _get_achievements_enabled_key(chat_id):
    """Returns the database key for the achievements enabled toggle."""
    return f"achievements_enabled:{chat_id}"

def _get_airdrop_token_key(chat_id):
    """Returns the database key for the group's airdrop token type."""
    return f"airdrop_token:{chat_id}"


def _get_airdrop_wallet_key(chat_id):
    """Returns the database key for the group's encrypted airdrop wallet config."""
    return f"airdrop_wallet:{chat_id}"


def _get_buybot_enabled_key(chat_id):
    return f"buybot_enabled:{chat_id}"


def _get_buybot_seen_key(chat_id):
    return f"buybot_seen:{chat_id}"


def _get_buybot_start_checkpoint_key(chat_id):
    return f"buybot_start_checkpoint:{chat_id}"


def _get_buybot_media_key(chat_id):
    return f"buybot_media:{chat_id}"


def _get_buybot_minimum_usd_key(chat_id):
    return f"buybot_minimum_usd:{chat_id}"


def _get_buybot_buyer_key(chat_id: int, coin_type: str, wallet: str) -> str:
    token = canonicalize_sui_type(coin_type)
    return f"buybot_buyer:{chat_id}:{token}:{wallet.lower()}"


def _get_buybot_checkpoint_key():
    return "buybot:checkpoint"


def _get_buybot_live_checkpoint_key():
    return "buybot:live_checkpoint"


def _track_chat(chat_id):
    db.enroll_chat(int(chat_id))


def _ensure_messages_migrated(chat_id):
    chat_id_int = int(chat_id)
    if chat_id_int in _migrated_chats:
        return
    if db.has_messages(chat_id_int):
        db.enroll_chat(chat_id_int)
        _migrated_chats.add(chat_id_int)
        return
    legacy_messages = db.get(_get_messages_key(chat_id_int), [])
    if legacy_messages:
        db.migrate_legacy_messages(chat_id_int, legacy_messages)
    db.enroll_chat(chat_id_int)
    _migrated_chats.add(chat_id_int)


def _get_recent_messages(chat_id, limit):
    _ensure_messages_migrated(chat_id)
    return db.get_recent_messages(int(chat_id), int(limit))


def _get_recent_user_messages(chat_id, user_id, limit):
    _ensure_messages_migrated(chat_id)
    return db.get_recent_user_messages(int(chat_id), int(user_id), int(limit))


def _new_flow_token() -> str:
    return uuid.uuid4().hex[:16]


def _get_score_requests(context):
    return context.application.bot_data.setdefault('score_requests', {})


def _get_score_results(context):
    return context.application.bot_data.setdefault('score_results', {})


def _get_calendar_sessions(context):
    return context.application.bot_data.setdefault('calendar_sessions', {})


def _get_wallet_flows(context):
    return context.application.bot_data.setdefault('wallet_flows', {})


def _get_airdrop_wallet_flows(context):
    return context.application.bot_data.setdefault('airdrop_wallet_flows', {})


def _get_leaderboard_messages(context):
    return context.application.bot_data.setdefault('leaderboard_messages', {})


def _short_address(address: str | None) -> str:
    if not address:
        return "unknown"
    return f"{address[:10]}...{address[-6:]}"

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
        _track_chat(chat_id)
        _ensure_messages_migrated(chat_id)
        user_key = _get_user_key(chat_id_str, user_id_str)
        existing_user_data = db.get(user_key)
        new_user_data = {"username": username, "first_name": first_name, "last_name": last_name}
        if not existing_user_data or any(existing_user_data.get(k) != v for k, v in new_user_data.items()):
            db[user_key] = {**new_user_data, "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

        db.add_message(int(chat_id), int(message_id), int(user_id), username, text, date, is_reply)

        stats_key = _get_user_stats_key(chat_id_str, user_id_str)
        user_stats = db.get(stats_key, {"message_count": 0})
        user_stats["message_count"] += 1
        db[stats_key] = user_stats

        if db.get(_get_achievements_enabled_key(chat_id), False):
            message_count = user_stats["message_count"]
            if message_count == 100:
                await award_badge(context, chat_id, user_id, 'contributor_100')
            elif message_count == 500:
                await award_badge(context, chat_id, user_id, 'hero_500')
            elif message_count == 1000:
                await award_badge(context, chat_id, user_id, 'godlike_1000')

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


def store_airdrop_wallet(chat_id, configured_by_user_id, private_key_hex):
    normalized_private_key = normalize_sui_private_key(private_key_hex)
    if not normalized_private_key:
        raise ValueError("Please send a valid SUI private key (suiprivkey1... or 64 hexadecimal characters, optionally prefixed with 0x).")

    wallet_address = derive_sui_address(normalized_private_key)
    wallet_key = _get_airdrop_wallet_key(chat_id)
    wallet_data = {
        "wallet_address": wallet_address,
        "encrypted_private_key": encrypt_private_key(normalized_private_key),
        "configured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "configured_by_user_id": str(configured_by_user_id),
    }
    db[wallet_key] = wallet_data
    if not db.get(wallet_key):
        raise Exception("Failed to verify airdrop wallet storage")
    return wallet_data


def get_airdrop_wallet(chat_id):
    return db.get(_get_airdrop_wallet_key(chat_id))


def delete_airdrop_wallet(chat_id):
    wallet_key = _get_airdrop_wallet_key(chat_id)
    if wallet_key in db:
        del db[wallet_key]


def resolve_airdrop_sender(chat_id):
    return resolve_airdrop_sender_config(get_airdrop_wallet(chat_id), os.environ.get("SUI_PRIVATE_KEY"))

def get_messages_by_date_range(chat_id, start_date, end_date):
    """Get messages from normalized storage, lazily migrating legacy chat blobs."""
    _ensure_messages_migrated(chat_id)
    if start_date.tzinfo is None:
        start_date = start_date.replace(tzinfo=datetime.timezone.utc)
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=datetime.timezone.utc)
    return db.get_messages(int(chat_id), start_date, end_date)


def get_chat_stats(chat_id):
    _ensure_messages_migrated(chat_id)
    return db.get_chat_stats(int(chat_id))


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
    _ensure_messages_migrated(chat_id)
    return [(str(user_id), message_count) for user_id, _, message_count in db.get_top_message_counts(int(chat_id), int(count))]


async def sui_get_total_balance(owner: str, coin_type: str) -> int:
    service = get_sui_service(SUI_GRPC_URL, SUI_GRPC_HEADERS)
    return await service.get_balance(owner, coin_type)


async def sui_get_coin_metadata(coin_type: str) -> dict | None:
    if coin_type in _coin_metadata_cache:
        return _coin_metadata_cache[coin_type]

    try:
        service = get_sui_service(SUI_GRPC_URL, SUI_GRPC_HEADERS)
        metadata = await service.get_coin_metadata(coin_type)
    except Exception as exc:
        logging.warning(f"Failed to fetch coin metadata for {coin_type}: {exc}")
        metadata = None

    _coin_metadata_cache[coin_type] = metadata
    return metadata


async def get_coin_amount_config(coin_type: str) -> dict:
    metadata = await sui_get_coin_metadata(coin_type)
    decimals = metadata.get("decimals") if isinstance(metadata, dict) else None
    symbol = metadata.get("symbol") if isinstance(metadata, dict) else None
    total_supply = (
        _positive_decimal(metadata.get("totalSupply"))
        if isinstance(metadata, dict)
        else None
    )
    if not isinstance(decimals, int):
        decimals = DEFAULT_SUI_COIN_DECIMALS
    if not symbol:
        symbol = coin_type.split("::")[-1]
    return {
        "decimals": decimals,
        "symbol": str(symbol).upper(),
        "total_supply": total_supply,
    }


async def preflight_airdrop(sender_address: str, recipient_count: int, amount: int, coin_type: str) -> dict:
    requirements = build_airdrop_balance_requirements(recipient_count, amount, coin_type, SUI_GAS_BUDGET)
    required_token_balance = requirements["required_token_balance"]
    required_sui_balance = requirements["required_sui_balance"]

    available_sui_balance = await sui_get_total_balance(sender_address, DEFAULT_SUI_COIN_TYPE)
    available_token_balance = available_sui_balance if coin_type == DEFAULT_SUI_COIN_TYPE else await sui_get_total_balance(sender_address, coin_type)

    if available_token_balance < required_token_balance:
        raise ValueError(
            f"Insufficient token balance in sender wallet. Need {required_token_balance}, have {available_token_balance}."
        )
    if available_sui_balance < required_sui_balance:
        raise ValueError(
            f"Insufficient SUI balance for transfers and gas. Need {required_sui_balance}, have {available_sui_balance}."
        )

    return {
        "available_sui_balance": available_sui_balance,
        "available_token_balance": available_token_balance,
        "required_sui_balance": required_sui_balance,
        "required_token_balance": required_token_balance,
    }


async def sui_transfer_token(recipient: str, amount: int, coin_type: str, sender_private_key_hex: str) -> dict | None:
    """Build, sign, and execute a Sui v2 programmable transaction over gRPC."""

    service = get_sui_service(SUI_GRPC_URL, SUI_GRPC_HEADERS)
    return await service.transfer_token(
        recipient=recipient,
        amount=amount,
        coin_type=coin_type,
        sender_private_key_hex=sender_private_key_hex,
        gas_budget=SUI_GAS_BUDGET,
    )


def _load_buybot_chat_tokens() -> dict[str, list[tuple[int, int | None]]]:
    """Return selected token types, chats, and their activation checkpoints."""

    token_chats: dict[str, list[tuple[int, int | None]]] = defaultdict(list)
    for key in db.prefix("buybot_enabled:"):
        if not db.get(key, False):
            continue
        try:
            chat_id = int(key.split(":", 1)[1])
        except (IndexError, ValueError):
            continue
        coin_type = db.get(_get_airdrop_token_key(chat_id))
        if coin_type:
            start_checkpoint = db.get(_get_buybot_start_checkpoint_key(chat_id))
            token_chats[str(coin_type)].append(
                (
                    chat_id,
                    int(start_checkpoint) if start_checkpoint is not None else None,
                )
            )
    return dict(token_chats)


def _initialize_buybot_start_checkpoints(
    token_chats: dict[str, list[tuple[int, int | None]]],
    latest_sequence: int,
) -> dict[str, list[tuple[int, int]]]:
    initialized: dict[str, list[tuple[int, int]]] = {}
    for coin_type, chats in token_chats.items():
        initialized[coin_type] = []
        for chat_id, start_checkpoint in chats:
            if start_checkpoint is None:
                start_checkpoint = latest_sequence
                db[_get_buybot_start_checkpoint_key(chat_id)] = start_checkpoint
            initialized[coin_type].append((chat_id, start_checkpoint))
    return initialized


def _buybot_checkpoint_batches(
    start_sequence: int,
    end_sequence: int,
):
    """Yield bounded checkpoint ranges while preserving processing order."""

    for batch_start in range(
        start_sequence,
        end_sequence + 1,
        _BUYBOT_CHECKPOINT_BATCH_SIZE,
    ):
        yield range(
            batch_start,
            min(
                batch_start + _BUYBOT_CHECKPOINT_BATCH_SIZE - 1,
                end_sequence,
            )
            + 1,
        )


def _buybot_digest_seen(chat_id: int, digest: str) -> bool:
    return digest in db.get(_get_buybot_seen_key(chat_id), [])


def _remember_buybot_digest(chat_id: int, digest: str) -> None:
    key = _get_buybot_seen_key(chat_id)
    seen = db.get(key, [])
    if digest not in seen:
        seen.append(digest)
        db[key] = seen[-_BUYBOT_SEEN_DIGEST_LIMIT:]


def _buy_event_date(
    timestamp,
    fallback: datetime.date | None = None,
) -> datetime.date:
    """Return the finalized transaction's UTC date."""

    fallback = fallback or datetime.datetime.now(datetime.timezone.utc).date()
    try:
        if isinstance(timestamp, datetime.datetime):
            value = timestamp
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value.astimezone(datetime.timezone.utc).date()
        if isinstance(timestamp, dict):
            timestamp = timestamp.get("seconds")
        if isinstance(timestamp, (int, float)) or (
            isinstance(timestamp, str) and timestamp.strip().lstrip("-").isdigit()
        ):
            return datetime.datetime.fromtimestamp(
                int(timestamp),
                tz=datetime.timezone.utc,
            ).date()
        if isinstance(timestamp, str) and timestamp.strip():
            value = datetime.datetime.fromisoformat(
                timestamp.strip().replace("Z", "+00:00")
            )
            if value.tzinfo is None:
                value = value.replace(tzinfo=datetime.timezone.utc)
            return value.astimezone(datetime.timezone.utc).date()
    except (OverflowError, TypeError, ValueError):
        pass
    return fallback


def _load_buybot_buyer_profile(
    chat_id: int,
    coin_type: str,
    wallet: str,
) -> dict:
    profile = db.get(_get_buybot_buyer_key(chat_id, coin_type, wallet), {})
    return profile if isinstance(profile, dict) else {}


def _classify_buy_badges(
    profile: dict | None,
    usd_value: Decimal | None,
    purchase_date: datetime.date,
) -> list[str]:
    """Classify a buy using only history recorded before this transaction."""

    profile = profile if isinstance(profile, dict) else {}
    try:
        buy_count = max(0, int(profile.get("buy_count", 0)))
    except (TypeError, ValueError):
        buy_count = 0

    badges = []
    value = _positive_decimal(usd_value)
    if value is not None and value >= _BUYBOT_WHALE_USD_THRESHOLD:
        badges.append("whale")
    badges.append("first_time" if buy_count == 0 else "returning")

    observed_dates = set()
    for raw_date in profile.get("buy_dates", []) or []:
        try:
            observed_dates.add(datetime.date.fromisoformat(str(raw_date)))
        except (TypeError, ValueError):
            continue
    if all(
        purchase_date - datetime.timedelta(days=offset) in observed_dates
        for offset in (1, 2)
    ):
        badges.append("three_day_streak")
    return badges


def _updated_buybot_buyer_profile(
    profile: dict | None,
    purchase_date: datetime.date,
) -> dict:
    """Return bounded buyer history after one successfully announced buy."""

    profile = profile if isinstance(profile, dict) else {}
    try:
        buy_count = max(0, int(profile.get("buy_count", 0)))
    except (TypeError, ValueError):
        buy_count = 0

    observed_dates = {purchase_date}
    for raw_date in profile.get("buy_dates", []) or []:
        try:
            observed_dates.add(datetime.date.fromisoformat(str(raw_date)))
        except (TypeError, ValueError):
            continue
    recent_dates = sorted(observed_dates)[-_BUYBOT_BUYER_DATE_HISTORY_LIMIT:]

    first_buy_date = profile.get("first_buy_date")
    if not isinstance(first_buy_date, str):
        first_buy_date = min(recent_dates).isoformat()
    return {
        "buy_count": buy_count + 1,
        "first_buy_date": first_buy_date,
        "last_buy_date": max(recent_dates).isoformat(),
        "buy_dates": [value.isoformat() for value in recent_dates],
    }


def _remember_buybot_buyer(
    chat_id: int,
    coin_type: str,
    wallet: str,
    profile: dict,
) -> None:
    db[_get_buybot_buyer_key(chat_id, coin_type, wallet)] = profile


def _buybot_media_from_message(message) -> dict[str, str] | None:
    """Extract reusable Telegram media from a replied-to message."""

    if message is None:
        return None
    animation = getattr(message, "animation", None)
    animation_file_id = getattr(animation, "file_id", None)
    if animation_file_id:
        return {"type": "animation", "file_id": str(animation_file_id)}

    video = getattr(message, "video", None)
    video_file_id = getattr(video, "file_id", None)
    if video_file_id:
        return {"type": "video", "file_id": str(video_file_id)}

    photos = getattr(message, "photo", None) or []
    photo_file_id = getattr(photos[-1], "file_id", None) if photos else None
    if photo_file_id:
        return {"type": "photo", "file_id": str(photo_file_id)}

    document = getattr(message, "document", None)
    document_file_id = getattr(document, "file_id", None)
    mime_type = str(getattr(document, "mime_type", "") or "").lower()
    file_name = str(getattr(document, "file_name", "") or "").lower()
    supported_extensions = (
        ".gif",
        ".heic",
        ".heif",
        ".jpeg",
        ".jpg",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".png",
        ".webm",
        ".webp",
    )
    if document_file_id and (
        mime_type.startswith(("image/", "video/"))
        or file_name.endswith(supported_extensions)
    ):
        return {"type": "document", "file_id": str(document_file_id)}
    return None


def _get_buybot_media(chat_id: int) -> dict[str, str] | None:
    media = db.get(_get_buybot_media_key(chat_id))
    if not isinstance(media, dict):
        return None
    media_type = media.get("type")
    file_id = media.get("file_id")
    if (
        media_type not in {"photo", "animation", "video", "document"}
        or not isinstance(file_id, str)
        or not file_id
    ):
        return None
    return {"type": media_type, "file_id": file_id}


def _clear_buybot_media(chat_id: int) -> None:
    key = _get_buybot_media_key(chat_id)
    if key in db:
        del db[key]


def _positive_decimal(value) -> Decimal | None:
    try:
        decimal_value = Decimal(str(value))
        return decimal_value if decimal_value.is_finite() and decimal_value > 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _nonnegative_decimal(value) -> Decimal | None:
    try:
        decimal_value = Decimal(str(value))
        return (
            decimal_value
            if decimal_value.is_finite() and decimal_value >= 0
            else None
        )
    except (InvalidOperation, TypeError, ValueError):
        return None


def _buy_meets_minimum(
    current_buy_usd: Decimal | None,
    minimum_buy_usd: Decimal | None,
) -> bool:
    """Return whether a buy is eligible for a group's announcements."""

    minimum = _positive_decimal(minimum_buy_usd)
    if minimum is None:
        return True
    current_value = _nonnegative_decimal(current_buy_usd)
    return current_value is not None and current_value >= minimum


def _get_buybot_minimum_usd(chat_id: int) -> Decimal | None:
    return _positive_decimal(db.get(_get_buybot_minimum_usd_key(chat_id)))


def _aggregate_token_volumes(
    pairs,
    coin_type: str,
) -> dict[str, Decimal] | None:
    """Sum rolling USD volume across unique Sui pools for one token."""

    if not isinstance(pairs, list):
        return None

    selected = canonicalize_sui_type(coin_type)
    seen_pairs: set[str] = set()
    totals = {"h1": Decimal(0), "h24": Decimal(0)}
    matched_pair = False

    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            continue
        if str(pair.get("chainId") or "").lower() != "sui":
            continue
        token_addresses = {
            canonicalize_sui_type((pair.get(side) or {}).get("address"))
            for side in ("baseToken", "quoteToken")
            if isinstance(pair.get(side), dict)
        }
        if selected not in token_addresses:
            continue

        pair_address = str(pair.get("pairAddress") or "").lower()
        pair_key = pair_address or f"response-index:{index}"
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)
        matched_pair = True

        volume = pair.get("volume")
        if not isinstance(volume, dict):
            continue
        for period in totals:
            value = _nonnegative_decimal(volume.get(period))
            if value is not None:
                totals[period] += value

    return totals if matched_pair else None


async def fetch_token_volume(coin_type: str) -> dict[str, Decimal] | None:
    """Fetch cached token-wide 24h and 1h USD DEX volume from DEX Screener."""

    cache_key = canonicalize_sui_type(coin_type)
    cached = _token_volume_cache.get(cache_key)
    if cached:
        result, cached_at = cached
        if time.monotonic() - cached_at < TOKEN_VOLUME_CACHE_TTL:
            return result

    result = None
    try:
        client = await get_shared_async_client()
        response = await client.get(
            f"{DEXSCREENER_API_URL}/token-pairs/v1/sui/"
            f"{quote(str(coin_type), safe='')}",
            timeout=TOKEN_VOLUME_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        result = _aggregate_token_volumes(response.json(), coin_type)
    except Exception as exc:
        logging.warning(
            "Unable to fetch Sui token volume for %s: %s",
            coin_type,
            exc,
        )

    # Negative-cache unavailable data too, so a provider outage cannot create
    # one external request per buy announcement.
    _token_volume_cache[cache_key] = (result, time.monotonic())
    return result


def _volume_including_current_buy(
    volume: dict[str, Decimal] | None,
    current_buy_usd: Decimal | None,
) -> dict[str, Decimal] | None:
    """Add this finalized buy to both provider volume windows."""

    current_value = _positive_decimal(current_buy_usd)
    if current_value is None:
        return volume

    baseline = volume if isinstance(volume, dict) else {}
    return {
        period: (
            _nonnegative_decimal(baseline.get(period)) or Decimal(0)
        ) + current_value
        for period in ("h1", "h24")
    }


def _calculate_buy_valuation(
    event,
    amount_config: dict,
    *,
    token_usd_price=None,
    sui_usd_price=None,
) -> dict[str, Decimal | None]:
    """Calculate exact SUI spend when available, otherwise market equivalents."""

    sui_price = _positive_decimal(sui_usd_price)
    token_price = _positive_decimal(token_usd_price)
    raw_sui_spent = getattr(event, "sui_spent", None)

    if raw_sui_spent:
        sui_value = Decimal(int(raw_sui_spent)) / _MIST_PER_SUI
        valuation = {
            "sui": sui_value,
            "usd": sui_value * sui_price if sui_price else None,
        }
        valuation["market_cap"] = _calculate_post_buy_market_cap(
            event,
            amount_config,
            valuation.get("usd"),
        )
        return valuation

    try:
        token_value = Decimal(int(event.amount)) / (
            Decimal(10) ** int(amount_config["decimals"])
        )
    except (KeyError, TypeError, ValueError):
        return {"sui": None, "usd": None, "market_cap": None}

    usd_value = token_value * token_price if token_price else None
    valuation = {
        "sui": usd_value / sui_price if usd_value is not None and sui_price else None,
        "usd": usd_value,
    }
    valuation["market_cap"] = _calculate_post_buy_market_cap(
        event,
        amount_config,
        valuation.get("usd"),
    )
    return valuation


def _calculate_post_buy_market_cap(
    event,
    amount_config: dict,
    buy_usd: Decimal | None,
) -> Decimal | None:
    """Estimate market cap from this finalized buy's effective token price."""

    usd_value = _positive_decimal(buy_usd)
    raw_supply = _positive_decimal(amount_config.get("total_supply"))
    try:
        raw_amount = _positive_decimal(event.amount)
        decimals = int(amount_config["decimals"])
    except (KeyError, TypeError, ValueError):
        return None
    if usd_value is None or raw_supply is None or raw_amount is None or decimals < 0:
        return None

    scale = Decimal(10) ** decimals
    purchased_tokens = raw_amount / scale
    total_tokens = raw_supply / scale
    return (usd_value / purchased_tokens) * total_tokens


async def _get_buy_valuation(event, amount_config: dict) -> dict[str, Decimal | None]:
    """Fetch cached market prices without making announcement delivery depend on them."""

    symbol = str(amount_config.get("symbol") or "").upper()
    if getattr(event, "sui_spent", None) or symbol == "SUI":
        sui_market = await fetch_crypto_price("SUI")
        sui_price = (sui_market or {}).get("price")
        return _calculate_buy_valuation(
            event,
            amount_config,
            token_usd_price=sui_price if symbol == "SUI" else None,
            sui_usd_price=sui_price,
        )

    sui_market, token_market = await asyncio.gather(
        fetch_crypto_price("SUI"),
        fetch_crypto_price(symbol),
    )
    return _calculate_buy_valuation(
        event,
        amount_config,
        token_usd_price=(token_market or {}).get("price"),
        sui_usd_price=(sui_market or {}).get("price"),
    )


def _buy_emoji_count(usd_value: Decimal | None) -> int:
    value = _positive_decimal(usd_value) or Decimal(0)
    return 1 + int(value // _BUYBOT_EMOJI_USD_STEP)


def _format_buy_emojis(usd_value: Decimal | None) -> str:
    count = _buy_emoji_count(usd_value)
    shown = min(count, _BUYBOT_MAX_EMOJIS)
    emojis = _BUYBOT_EMOJI * shown
    if count > shown:
        emojis += f" <b>×{count:,}</b>"
    return emojis


def _format_sui_value(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{value.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"


def _format_buy_token_amount(raw_amount: int, decimals: int) -> str:
    try:
        amount = Decimal(int(raw_amount)) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, TypeError, ValueError):
        return "N/A"
    return f"{amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"


def _format_usd_value(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    if 0 < value < Decimal("0.01"):
        return "&lt;$0.01"
    return f"${value:,.2f}"


def _format_volume_usd(value: Decimal | None) -> str:
    value = _nonnegative_decimal(value)
    if value is None:
        return "N/A"
    if value >= Decimal("1000000000"):
        return f"${value / Decimal('1000000000'):.2f}B"
    if value >= Decimal("1000000"):
        return f"${value / Decimal('1000000'):.2f}M"
    if value >= Decimal("1000"):
        return f"${value / Decimal('1000'):.2f}K"
    return f"${value:.2f}"


def _abbreviate_wallet(wallet: str) -> str:
    wallet = str(wallet or "")
    return f"{wallet[:4]}...{wallet[-4:]}" if len(wallet) > 11 else wallet


def _wallet_link(wallet: str) -> str:
    wallet = str(wallet or "")
    wallet_url = (
        f"{SUI_EXPLORER_ADDRESS_URL.rstrip('/')}/"
        f"{quote(wallet, safe='')}"
    )
    return (
        f'<a href="{html.escape(wallet_url, quote=True)}">'
        f"{html.escape(_abbreviate_wallet(wallet))}</a>"
    )


def _format_buy_announcement(
    event,
    amount_config: dict,
    valuation: dict[str, Decimal | None] | None = None,
    badges: list[str] | None = None,
    volume: dict[str, Decimal] | None = None,
) -> str:
    valuation = valuation or {"sui": None, "usd": None}
    volume = volume or {}
    symbol = html.escape(amount_config["symbol"])
    digest = html.escape(event.digest)
    tx_url = f"{SUI_EXPLORER_TX_URL.rstrip('/')}/{event.digest}"
    lines = [
        f"🟢 <b>{symbol} Buy!</b>",
        _format_buy_emojis(valuation.get("usd")),
    ]
    badge_text = [
        f"{_BUYBOT_BADGES[badge][0]} <b>{_BUYBOT_BADGES[badge][1]}</b>"
        for badge in badges or []
        if badge in _BUYBOT_BADGES
    ]
    if badge_text:
        lines.append(" · ".join(badge_text))
    lines.extend(
        [
            "",
            f"<b>Amount:</b> {_format_buy_token_amount(event.amount, amount_config['decimals'])} {symbol}",
            (
                f"<b>Value:</b> {_format_sui_value(valuation.get('sui'))} SUI"
                f" / {_format_usd_value(valuation.get('usd'))} USD"
            ),
            f"<b>Buyer:</b> {_wallet_link(event.wallet)}",
            f"<b>Market Cap:</b> {_format_volume_usd(valuation.get('market_cap'))}",
            f"<b>24h Volume:</b> {_format_volume_usd(volume.get('h24'))}",
            f"<b>1h Volume:</b> {_format_volume_usd(volume.get('h1'))}",
        ]
    )
    if event.sender and event.sender.lower() != event.wallet.lower():
        lines.append(f"<b>Transaction sender:</b> {_wallet_link(event.sender)}")
    lines.extend(
        [
            f'<a href="{html.escape(tx_url, quote=True)}">View transaction</a>',
            f"<code>{digest}</code>",
        ]
    )
    return "\n".join(lines) + FOOTER_HTML


async def _send_buy_announcement(context, chat_id: int, text: str) -> None:
    """Send one text or media-caption announcement using group customization."""

    media = await asyncio.to_thread(_get_buybot_media, chat_id)
    if not media:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    try:
        if media["type"] == "photo":
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=media["file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        elif media["type"] == "animation":
            await context.bot.send_animation(
                chat_id=chat_id,
                animation=media["file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        elif media["type"] == "video":
            await context.bot.send_video(
                chat_id=chat_id,
                video=media["file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_document(
                chat_id=chat_id,
                document=media["file_id"],
                caption=text,
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        if exc.__class__.__name__ != "BadRequest":
            raise
        logging.warning(
            "Telegram rejected custom buy media for chat %s; clearing it and "
            "falling back to text: %s",
            chat_id,
            exc,
        )
        await asyncio.to_thread(_clear_buybot_media, chat_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def _announce_checkpoint_buys(
    context,
    checkpoint,
    token_chats: dict[str, list[tuple[int, int]]],
) -> bool:
    """Announce buys in one checkpoint; return False when a transient send failed."""

    all_sent = True
    amount_configs: dict[str, dict] = {}
    minimums_by_chat: dict[int, Decimal | None] = {}
    for transaction in checkpoint.transactions or []:
        for coin_type, chats in token_chats.items():
            event = detect_buy(transaction, coin_type, SUI_DEX_PACKAGES)
            if not event or not event.digest:
                continue
            if coin_type not in amount_configs:
                amount_configs[coin_type] = await get_coin_amount_config(coin_type)
            valuation, volume = await asyncio.gather(
                _get_buy_valuation(event, amount_configs[coin_type]),
                fetch_token_volume(coin_type),
            )
            display_volume = _volume_including_current_buy(
                volume,
                valuation.get("usd"),
            )

            for chat_id, start_checkpoint in chats:
                if checkpoint.sequence_number <= start_checkpoint:
                    continue
                if await asyncio.to_thread(_buybot_digest_seen, chat_id, event.digest):
                    continue
                if chat_id not in minimums_by_chat:
                    minimums_by_chat[chat_id] = await asyncio.to_thread(
                        _get_buybot_minimum_usd,
                        chat_id,
                    )
                minimum_buy_usd = minimums_by_chat[chat_id]
                if not _buy_meets_minimum(
                    valuation.get("usd"),
                    minimum_buy_usd,
                ):
                    logging.info(
                        "Skipped buy announcement for %s in chat %s: USD value %s "
                        "is below or unavailable for minimum %s",
                        event.digest,
                        chat_id,
                        valuation.get("usd"),
                        minimum_buy_usd,
                    )
                    continue
                purchase_date = _buy_event_date(event.timestamp)
                buyer_profile = await asyncio.to_thread(
                    _load_buybot_buyer_profile,
                    chat_id,
                    coin_type,
                    event.wallet,
                )
                badges = _classify_buy_badges(
                    buyer_profile,
                    valuation.get("usd"),
                    purchase_date,
                )
                text = _format_buy_announcement(
                    event,
                    amount_configs[coin_type],
                    valuation,
                    badges,
                    display_volume,
                )
                try:
                    await _send_buy_announcement(context, chat_id, text)
                except Exception as exc:
                    error_name = exc.__class__.__name__
                    logging.error(
                        f"Failed to send buy announcement for {event.digest} to chat {chat_id}: {error_name}: {exc}"
                    )
                    if error_name in {"Forbidden", "BadRequest"}:
                        await asyncio.to_thread(
                            db.__setitem__,
                            _get_buybot_enabled_key(chat_id),
                            False,
                        )
                        logging.warning(f"Disabled buy bot for unreachable chat {chat_id}.")
                    else:
                        all_sent = False
                    continue
                updated_profile = _updated_buybot_buyer_profile(
                    buyer_profile,
                    purchase_date,
                )
                await asyncio.to_thread(
                    _remember_buybot_buyer,
                    chat_id,
                    coin_type,
                    event.wallet,
                    updated_profile,
                )
                await asyncio.to_thread(_remember_buybot_digest, chat_id, event.digest)

    return all_sent


async def _announce_and_advance_buybot_cursor(
    context,
    checkpoint,
    token_chats: dict[str, list[tuple[int, int]]],
    cursor_key: str,
) -> bool:
    if not await _announce_checkpoint_buys(context, checkpoint, token_chats):
        return False
    await asyncio.to_thread(
        db.__setitem__,
        cursor_key,
        checkpoint.sequence_number,
    )
    return True


async def check_sui_buys(context: ContextTypes.DEFAULT_TYPE):
    """Stream live buys first, then advance the durable historical cursor."""

    lock = context.application.bot_data.setdefault("buybot_checkpoint_lock", asyncio.Lock())
    if lock.locked():
        return

    async with lock:
        token_chats = await asyncio.to_thread(_load_buybot_chat_tokens)
        if not token_chats:
            return

        service = get_sui_service(SUI_GRPC_URL, SUI_GRPC_HEADERS)
        try:
            latest = await service.get_latest_checkpoint()
            latest_sequence = int(latest.sequence_number)
            token_chats = await asyncio.to_thread(
                _initialize_buybot_start_checkpoints,
                token_chats,
                latest_sequence,
            )
            subscribed = await service.get_subscribed_checkpoints(
                max_items=100,
                wait_ms=500,
            )
            live_cursor_key = _get_buybot_live_checkpoint_key()
            live_cursor = await asyncio.to_thread(db.get, live_cursor_key)
            for checkpoint in subscribed:
                if (
                    live_cursor is not None
                    and checkpoint.sequence_number <= int(live_cursor)
                ):
                    continue

                gap_start = checkpoint.sequence_number
                if live_cursor is not None:
                    gap_start = max(
                        int(live_cursor) + 1,
                        checkpoint.sequence_number - _BUYBOT_LIVE_GAP_LOOKBACK,
                    )
                if gap_start < checkpoint.sequence_number:
                    for sequence_numbers in _buybot_checkpoint_batches(
                        gap_start,
                        checkpoint.sequence_number - 1,
                    ):
                        gap_checkpoints = await service.get_checkpoints(
                            sequence_numbers
                        )
                        for gap_checkpoint in gap_checkpoints:
                            if not await _announce_and_advance_buybot_cursor(
                                context,
                                gap_checkpoint,
                                token_chats,
                                live_cursor_key,
                            ):
                                return
                            live_cursor = gap_checkpoint.sequence_number

                if not await _announce_and_advance_buybot_cursor(
                    context,
                    checkpoint,
                    token_chats,
                    live_cursor_key,
                ):
                    return
                live_cursor = checkpoint.sequence_number

            cursor = await asyncio.to_thread(db.get, _get_buybot_checkpoint_key())
            if cursor is None or int(cursor) > latest_sequence:
                await asyncio.to_thread(
                    db.__setitem__,
                    _get_buybot_checkpoint_key(),
                    latest_sequence,
                )
                return

            earliest_active_checkpoint = min(
                start_checkpoint
                for chats in token_chats.values()
                for _, start_checkpoint in chats
            )
            if int(cursor) < earliest_active_checkpoint:
                cursor = earliest_active_checkpoint
                await asyncio.to_thread(
                    db.__setitem__,
                    _get_buybot_checkpoint_key(),
                    cursor,
                )

            end_sequence = min(
                latest_sequence,
                int(cursor) + _BUYBOT_MAX_CHECKPOINTS_PER_RUN,
            )
            for sequence_numbers in _buybot_checkpoint_batches(
                int(cursor) + 1,
                end_sequence,
            ):
                checkpoints = await service.get_checkpoints(sequence_numbers)
                checkpoints_by_sequence = {
                    checkpoint.sequence_number: checkpoint for checkpoint in checkpoints
                }
                for sequence_number in sequence_numbers:
                    checkpoint = checkpoints_by_sequence.get(sequence_number)
                    if checkpoint is None:
                        raise RuntimeError(
                            f"Sui gRPC batch omitted checkpoint {sequence_number}."
                        )
                    if not await _announce_and_advance_buybot_cursor(
                        context,
                        checkpoint,
                        token_chats,
                        _get_buybot_checkpoint_key(),
                    ):
                        return
        except Exception as exc:
            logging.error(f"Sui buy tracker checkpoint poll failed: {exc}")


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
        client = await get_shared_async_client()
        normalized_query = re.sub(r"[^a-z0-9]+", "", symbol.lower())
        preferred_coin_id = ALL_PRICE_ALIASES.get(normalized_query)

        search_coin_map: dict[str, tuple[dict, int]] = {}

        if preferred_coin_id:
            coin_ids = [preferred_coin_id]
        else:
            search_resp = await client.get(
                f"{COINGECKO_API_URL}/search",
                params={"query": symbol},
            )
            search_resp.raise_for_status()
            coins = search_resp.json().get("coins", [])

            if not coins:
                return None

            coin_ids = [coin["id"] for coin in coins[:10]]
            search_coin_map = {coin["id"]: (coin, i) for i, coin in enumerate(coins[:10])}

        markets_resp = await client.get(
            f"{COINGECKO_API_URL}/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ",".join(coin_ids),
                "price_change_percentage": "24h",
                "order": "market_cap_desc",
                "per_page": str(len(coin_ids)),
                "page": "1",
            },
        )
        markets_resp.raise_for_status()
        markets: list[dict] = markets_resp.json()

        if not markets:
            return None

        def _normalize(v: str | None) -> str:
            return re.sub(r"[^a-z0-9]+", "", (v or "").lower())

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
    chat_ids = set(db.get_enrolled_chat_ids())
    for key in db.prefix('event:'):
        try:
            chat_ids.add(int(key.split(':')[1]))
        except (ValueError, IndexError):
            continue

    for chat_id in chat_ids:
        try:
            tz_name = db.get(_get_timezone_key(chat_id), "UTC")
            chat_tz = pytz.timezone(tz_name)
            now_in_tz = datetime.datetime.now(chat_tz)
            today_date = now_in_tz.date()
            announced_key = _get_announced_key(chat_id, today_date)

            if now_in_tz.hour == 8 and announced_key not in db:
                event_key = _get_event_key(chat_id, today_date)
                event_text = db.get(event_key)
                if event_text:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"📢 <b>Today's Event</b>\n\n{event_text}",
                        parse_mode=ParseMode.HTML,
                    )
                    db[announced_key] = True
        except Exception as e:
            logging.error(f"Failed during event announcement check for chat {chat_id}: {e}")


# --- Core Bot Logic ---
async def store_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text or update.message.from_user.is_bot: return
    text = update.message.text.strip()
    if text.startswith('/') or len(text.split()) < 3: return
    user = update.message.from_user
    await store_message_db(context, update.effective_chat.id, user.id, user.username or user.first_name, user.first_name, user.last_name, update.message.text, update.message.date, update.message.reply_to_message is not None, update.message.message_id)

def _name_guard_reason_text(match: NameGuardMatch) -> str:
    if match.kind == "protected_word":
        return f'their name contains the protected word "{match.value}"'
    return "their name matches a protected admin identity"


async def _silence_name_guard_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    member,
    match: NameGuardMatch,
) -> bool:
    chat_id = update.effective_chat.id
    safe_name = html.escape(member.full_name)
    mention = f'<a href="tg://user?id={member.id}">{safe_name}</a>'
    try:
        restricted = await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=member.id,
            permissions=ChatPermissions(can_send_messages=False),
        )
        if restricted is not True:
            raise RuntimeError("Telegram did not confirm the restriction")
    except Exception as exc:
        logging.error(
            "Name Guard could not silence user %s in chat %s: %s",
            member.id,
            chat_id,
            exc,
        )
        await update.message.reply_text(
            f"⚠️ <b>Name Guard</b> matched {mention}, but could not silence "
            "the account. Check that the bot has the Ban users permission.",
            parse_mode=ParseMode.HTML,
        )
        return False

    logging.info(
        "Name Guard silenced user %s in chat %s (%s)",
        member.id,
        chat_id,
        match.kind,
    )
    await update.message.reply_text(
        f"🛡️ <b>Name Guard</b> silenced {mention} because "
        f"{html.escape(_name_guard_reason_text(match))}.",
        parse_mode=ParseMode.HTML,
    )
    return True


async def welcome_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Apply join protection and optionally welcome new group members."""
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.effective_chat.id
    name_guard_enabled = db.get(_get_name_guard_key(chat_id), False)
    welcome_enabled = db.get(_get_welcome_key(chat_id), False)

    administrators = []
    if name_guard_enabled:
        try:
            administrators = await context.bot.get_chat_administrators(chat_id)
        except Exception as exc:
            # Keyword screening can still operate. Protected identities will be
            # retried on the next join rather than disabling the entire guard.
            logging.warning(
                "Name Guard could not load admins for chat %s: %s",
                chat_id,
                exc,
            )

    for member in update.message.new_chat_members:
        if member.is_bot:
            continue
        if name_guard_enabled:
            match = evaluate_name_guard(member, administrators)
            if match:
                await _silence_name_guard_member(update, context, member, match)
                continue
        if not welcome_enabled:
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
    # Shared across the wallet and airdrop-wallet deep-link flows below.
    user_id = update.effective_user.id

    if context.args and context.args[0].startswith('wallet_'):
        try:
            target_chat_id = int(context.args[0].split('_')[1])
            logging.info(f"Wallet flow requested for user {user_id} and chat {target_chat_id}")
        except (IndexError, ValueError) as e:
            logging.error(f"Invalid wallet start link from user {user_id}: {e}")
            await update.message.reply_text("Invalid start link. Please use the button from a group chat.")
            return ConversationHandler.END

        try:
            wallet_data = await asyncio.to_thread(get_group_specific_wallet, target_chat_id, user_id)
            chat_info = await context.bot.get_chat(target_chat_id)
            chat_name = chat_info.title or f"Group (ID: {target_chat_id})"

            if wallet_data:
                _get_wallet_flows(context)[user_id] = target_chat_id
                await update.message.reply_text(
                    f"You have a wallet submitted for *{escape_markdown(chat_name)}*:\n\n"
                    f"`{wallet_data['wallet_address']}`\n\n"
                    "To replace it, simply reply with your new wallet address\\. To keep it, type /cancel\\.",
                    parse_mode='MarkdownV2'
                )
                logging.info(f"Existing wallet found for user {user_id} in chat {target_chat_id}")
            else:
                _get_wallet_flows(context)[user_id] = target_chat_id
                await update.message.reply_text(
                    f"Please reply to this message with your SUI wallet address to submit it for the group: *{escape_markdown(chat_name)}*",
                    parse_mode='MarkdownV2'
                )
                logging.info(f"No existing wallet found for user {user_id} in chat {target_chat_id}, awaiting submission")
            return AWAITING_WALLET
        except Exception as e:
            logging.error(f"Error in wallet flow for user {user_id}: {e}")
            await update.message.reply_text("There was an error accessing the group information. Please try again from the group chat.")
            return ConversationHandler.END

    if context.args and context.args[0].startswith('airdropwallet_'):
        try:
            target_chat_id = int(context.args[0].split('_')[1])
            logging.info(f"Airdrop wallet flow requested for user {user_id} and chat {target_chat_id}")
        except (IndexError, ValueError) as e:
            logging.error(f"Invalid airdrop wallet start link from user {user_id}: {e}")
            await update.message.reply_text("Invalid start link. Please use the button from your group chat.")
            return ConversationHandler.END

        try:
            if not await user_is_admin(context, target_chat_id, user_id):
                await update.message.reply_text("❌ Only group administrators can configure that group's airdrop wallet.")
                return ConversationHandler.END

            wallet_data = await asyncio.to_thread(get_airdrop_wallet, target_chat_id)
            chat_info = await context.bot.get_chat(target_chat_id)
            chat_name = chat_info.title or f"Group (ID: {target_chat_id})"
            _get_airdrop_wallet_flows(context)[user_id] = target_chat_id

            if wallet_data:
                await update.message.reply_text(
                    (
                        f"🔐 <b>{html.escape(chat_name)}</b> already has an airdrop wallet configured.\n\n"
                        f"Current sender address: <code>{html.escape(wallet_data.get('wallet_address', 'unknown'))}</code>\n\n"
                        "Reply with a new SUI private key (<code>suiprivkey1...</code> or 64 hex characters) to replace it, or send <code>remove</code> to delete it."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            else:
                await update.message.reply_text(
                        (
                            f"🔐 Send the SUI private key for <b>{html.escape(chat_name)}</b> in this DM.\n\n"
                            "Send the wallet export in <code>suiprivkey1...</code> format (preferred) or as 64 hexadecimal characters.\n"
                            "The key will be encrypted before it is stored, and only decrypted in memory when signing airdrops.\n"
                            "Send <code>remove</code> if you want to clear an existing group wallet instead."
                        ),
                    parse_mode=ParseMode.HTML,
                )
            return AWAITING_AIRDROP_PRIVATE_KEY
        except Exception as e:
            logging.error(f"Error in airdrop wallet flow for user {user_id}: {e}")
            await update.message.reply_text("There was an error accessing the group information. Please try again from the group chat.")
            return ConversationHandler.END

    await update.message.reply_text(HELP_TEXT + FOOTER_HTML, parse_mode='HTML')
    return ConversationHandler.END


async def score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    start_date, end_date = None, None
    score_invalid_format = 'Invalid format. Try "# messages", "# days", or "MM/DD/YYYY-MM/DD/YYYY"'

    if len(context.args) >= 2 and context.args[1].lower().startswith('message'):
        try:
            count = int(context.args[0])
            messages = await asyncio.to_thread(_get_recent_messages, update.effective_chat.id, count)
            if messages:
                start_date = datetime.datetime.fromisoformat(messages[0]['date'])
                end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
        except (ValueError, IndexError):
            await update.message.reply_text(score_invalid_format)
            return
    else:
        start_date, end_date, _ = await _parse_date_range(context.args)

    if not start_date or not end_date:
        await update.message.reply_text(score_invalid_format)
        return

    request_token = _new_flow_token()
    _get_score_requests(context)[request_token] = {
        'requester_id': update.effective_user.id,
        'start_date': start_date,
        'end_date': end_date,
        'start_str': start_date.strftime('%m/%d/%Y'),
        'end_str': end_date.strftime('%m/%d/%Y'),
        'export': len(context.args) > 2 and context.args[2].lower() == 'export',
        'chat_id': update.effective_chat.id,
    }
    keyboard = [
        [InlineKeyboardButton('📩 Send to Private Chat', callback_data=f'score_private_{request_token}')],
        [InlineKeyboardButton('📢 Broadcast in Group', callback_data=f'score_broadcast_{request_token}')],
    ]
    await update.message.reply_text(
        f"📊 <b>Leaderboard for {start_date:%m/%d/%Y} → {end_date:%m/%d/%Y}</b>\n\n"
        "Where would you like to receive the result?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def public_score_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    start_date, end_date = None, None
    score_invalid_format = 'Invalid format. Try "# messages", "# days", or "MM/DD/YYYY-MM/DD/YYYY"'

    if len(context.args) >= 2 and context.args[1].lower().startswith('message'):
        try:
            count = int(context.args[0])
            messages = await asyncio.to_thread(_get_recent_messages, update.effective_chat.id, count)
            if messages:
                start_date = datetime.datetime.fromisoformat(messages[0]['date'])
                end_date = datetime.datetime.fromisoformat(messages[-1]['date'])
        except (ValueError, IndexError):
            await update.message.reply_text(score_invalid_format)
            return
    else:
        start_date, end_date, _ = await _parse_date_range(context.args)

    if not start_date or not end_date:
        await update.message.reply_text(score_invalid_format)
        return

    await update.message.reply_text('🔍 Generating public score...')
    result, error = await generate_leaderboard(context, update.effective_chat.id, start_date, end_date, start_date.strftime('%m/%d/%Y'), end_date.strftime('%m/%d/%Y'))
    if error:
        await update.message.reply_text(f'❌ {error}')
        return
    leaderboard_data = result[2]
    public_text = f"🏆 <b>Public Score</b> ({start_date:%m/%d/%Y} → {end_date:%m/%d/%Y})\n\n<pre>"
    public_text += "Rank | User                 | Total Score\n" + "-" * 40 + "\n"
    for idx, (uname, met, _, _) in enumerate(leaderboard_data[:20], 1):
        medal = '🥇' if idx == 1 else '🥈' if idx == 2 else '🥉' if idx == 3 else '  '
        safe_uname = html.escape(uname[:19])
        public_text += f"{medal}{idx:2d} | {safe_uname:<19} | {met['total']:7.1f}\n"
    public_text += '</pre>'
    await update.message.reply_text(public_text + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def summarize_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Generates an AI-powered summary of recent chat activity."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(INVALID_FORMAT_MESSAGE)
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    remaining = _check_ai_rate_limit(user_id, chat_id)
    if remaining > 0:
        await update.message.reply_text(_AI_RATE_LIMIT_MESSAGE.format(remaining=remaining))
        return

    try:
        count = min(int(context.args[0]), MAX_MESSAGES_INPUT_LIMIT)
        topic = " ".join(context.args[1:]) if len(context.args) > 1 else None

        await update.message.reply_text("Summarizing conversation, this may take a moment...")
        _record_ai_rate_limit(user_id, chat_id)

        _track_chat(chat_id)
        messages = await asyncio.to_thread(_get_recent_messages, chat_id, count)

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

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    remaining = _check_ai_rate_limit(user_id, chat_id)
    if remaining > 0:
        await update.message.reply_text(_AI_RATE_LIMIT_MESSAGE.format(remaining=remaining))
        return

    try:
        count = min(int(context.args[0]), MAX_MESSAGES_INPUT_LIMIT)

        await update.message.reply_text("Curating the best messages, please wait...")
        _record_ai_rate_limit(user_id, chat_id)

        _track_chat(chat_id)
        messages = await asyncio.to_thread(_get_recent_messages, chat_id, count)

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

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    remaining = _check_ai_rate_limit(user_id, chat_id)
    if remaining > 0:
        await update.message.reply_text(_AI_RATE_LIMIT_MESSAGE.format(remaining=remaining))
        return

    try:
        count = min(int(context.args[0]), MAX_MESSAGES_INPUT_LIMIT)
        topic = " ".join(context.args[1:]) if len(context.args) > 1 else None

        await update.message.reply_text("Checking the vibe, please wait...")
        _record_ai_rate_limit(user_id, chat_id)

        _track_chat(chat_id)
        messages = await asyncio.to_thread(_get_recent_messages, chat_id, count)

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

    remaining = _check_ai_rate_limit(user_id, chat_id)
    if remaining > 0:
        await update.message.reply_text(_AI_RATE_LIMIT_MESSAGE.format(remaining=remaining))
        return

    await update.message.reply_text("Digging through your post history to get your essence...")
    _record_ai_rate_limit(user_id, chat_id)

    _track_chat(chat_id)
    user_messages = await asyncio.to_thread(_get_recent_user_messages, chat_id, user_id, 200)

    if len(user_messages) < 10:
        await update.message.reply_text("I don't have enough of your message history to create a copypasta yet. Keep chatting!")
        return

    transcript = "\n".join([msg['text'] for msg in user_messages])
    copypasta = await asyncio.to_thread(generate_copypasta, transcript)

    await update.message.reply_text(copypasta)


async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to toggle welcome messages on or off."""
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    if not context.args or context.args[0].lower() not in ('on', 'off'):
        await update.message.reply_text('Usage: /setwelcome on or /setwelcome off')
        return

    enabled = context.args[0].lower() == 'on'
    db[_get_welcome_key(update.effective_chat.id)] = enabled
    status = 'enabled ✅' if enabled else 'disabled ❌'
    await update.message.reply_text(f'Welcome messages have been {status}.')


async def nameguard_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to toggle join-time impersonation protection."""
    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text("Please use /nameguard in a group chat.")
        return
    if not await require_admin(update, context):
        return

    if len(context.args) != 1 or context.args[0].lower() not in ("on", "off"):
        await update.message.reply_text("Usage: /nameguard on or /nameguard off")
        return

    chat_id = update.effective_chat.id
    _track_chat(chat_id)
    enabled = context.args[0].lower() == "on"
    db[_get_name_guard_key(chat_id)] = enabled
    if enabled:
        await update.message.reply_text(
            "🛡️ Name Guard enabled. New members will be silenced when their "
            "display name or username contains the complete word dev, admin, "
            "or support, or matches a current admin identity.\n\n"
            "The bot must have the Ban users permission."
        )
    else:
        await update.message.reply_text(
            "Name Guard disabled. Existing member restrictions were not changed."
        )


async def setachievements_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to toggle achievement tracking on or off."""
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    if not context.args or context.args[0].lower() not in ('on', 'off'):
        await update.message.reply_text('Usage: /setachievements on or /setachievements off')
        return

    enabled = context.args[0].lower() == 'on'
    db[_get_achievements_enabled_key(update.effective_chat.id)] = enabled
    status = 'enabled ✅' if enabled else 'disabled ❌'
    await update.message.reply_text(f'Achievement tracking has been {status}.')


async def settoken_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to set the airdrop token type for the group."""
    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id
    _track_chat(chat_id)
    if not context.args:
        selected_token = db.get(_get_airdrop_token_key(chat_id))
        current_token = selected_token or DEFAULT_SUI_COIN_TYPE
        selection_note = (
            "This token is explicitly selected and can be tracked by the buy bot."
            if selected_token
            else "No token is explicitly selected. Airdrops fall back to SUI, and the buy bot stays idle."
        )
        await update.message.reply_text(
            f"Current airdrop token: <code>{html.escape(current_token)}</code>\n\n"
            f"{selection_note}\n\n"
            "Usage: /settoken &lt;coin_type&gt;\n"
            "Example: /settoken 0x2::sui::SUI\n"
            "Use /settoken off to clear the selected token.",
            parse_mode=ParseMode.HTML,
        )
        return

    coin_type = context.args[0].strip()
    if coin_type.lower() in {"off", "none", "clear"}:
        key = _get_airdrop_token_key(chat_id)
        if key in db:
            del db[key]
        db[_get_buybot_enabled_key(chat_id)] = False
        start_key = _get_buybot_start_checkpoint_key(chat_id)
        if start_key in db:
            del db[start_key]
        await update.message.reply_text(
            "✅ The selected token has been cleared. Airdrops will fall back to SUI, and buy announcements are disabled."
        )
        return

    db[_get_airdrop_token_key(chat_id)] = coin_type
    if db.get(_get_buybot_enabled_key(chat_id), False):
        start_key = _get_buybot_start_checkpoint_key(chat_id)
        if start_key in db:
            del db[start_key]
    await update.message.reply_text(
        f"✅ Airdrop token set to: <code>{html.escape(coin_type)}</code>",
        parse_mode=ParseMode.HTML,
    )


async def setbuybot_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enable or disable selected-token buy announcements for a group."""

    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id
    _track_chat(chat_id)
    selected_token = db.get(_get_airdrop_token_key(chat_id))
    enabled = bool(db.get(_get_buybot_enabled_key(chat_id), False))

    if not context.args or context.args[0].lower() not in {"on", "off"}:
        status = "on ✅" if enabled else "off ❌"
        token_text = (
            f"<code>{html.escape(selected_token)}</code>"
            if selected_token
            else "none"
        )
        await update.message.reply_text(
            f"Buy bot: {status}\nSelected token: {token_text}\n\n"
            "Usage: /setbuybot on or /setbuybot off",
            parse_mode=ParseMode.HTML,
        )
        return

    requested_enabled = context.args[0].lower() == "on"
    if requested_enabled and not selected_token:
        await update.message.reply_text(
            "❌ Select an airdrop token first with /settoken &lt;coin_type&gt;. "
            "The buy bot never uses the implicit SUI airdrop fallback.",
            parse_mode=ParseMode.HTML,
        )
        return

    start_key = _get_buybot_start_checkpoint_key(chat_id)
    if requested_enabled:
        try:
            latest = await get_sui_service(
                SUI_GRPC_URL,
                SUI_GRPC_HEADERS,
            ).get_latest_checkpoint()
        except Exception as exc:
            logging.error(f"Unable to enable buy bot because Sui is unavailable: {exc}")
            await update.message.reply_text(
                "❌ The Sui tracker is temporarily unavailable, so buy announcements "
                "were not enabled. Please try again shortly."
            )
            return
        db[_get_buybot_enabled_key(chat_id)] = True
        db[start_key] = latest.sequence_number
        await update.message.reply_text(
            f"✅ Buy announcements are enabled for <code>{html.escape(selected_token)}</code>.",
            parse_mode=ParseMode.HTML,
        )
    else:
        db[_get_buybot_enabled_key(chat_id)] = False
        if start_key in db:
            del db[start_key]
        await update.message.reply_text("✅ Buy announcements are disabled.")


async def setbuyimage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set, inspect, or remove a group's custom buy announcement media."""

    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "Use /setbuyimage in the group whose buy announcements you want to customize."
        )
        return
    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id
    _track_chat(chat_id)
    args = [str(arg).lower() for arg in (context.args or [])]
    if args:
        if args == ["off"]:
            _clear_buybot_media(chat_id)
            await update.message.reply_text(
                "✅ Custom buy announcement media has been removed."
            )
            return
        await update.message.reply_text(
            "Usage: Reply to a photo, GIF, or video with /setbuyimage, or use "
            "/setbuyimage off to remove it."
        )
        return

    replied_message = update.message.reply_to_message
    media = _buybot_media_from_message(replied_message)
    if media:
        db[_get_buybot_media_key(chat_id)] = media
        media_label = {
            "animation": "GIF/animation",
            "document": "image/video file",
            "photo": "image",
            "video": "video",
        }[media["type"]]
        await update.message.reply_text(
            f"✅ Custom buy announcement {media_label} saved for this group."
        )
        return

    if replied_message is not None:
        await update.message.reply_text(
            "❌ That message does not contain a supported photo, GIF, or video."
        )
        return

    configured = _get_buybot_media(chat_id)
    media_labels = {
        "animation": "GIF/animation",
        "document": "image/video file",
        "photo": "image",
        "video": "video",
    }
    status = f"{media_labels[configured['type']]} ✅" if configured else "not set"
    await update.message.reply_text(
        f"Custom buy media: {status}\n\n"
        "Reply to a photo, GIF, or video with /setbuyimage to use it in future buy "
        "announcements. Use /setbuyimage off to return to text-only announcements."
    )


async def setminbuy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set, inspect, or remove a group's minimum announced buy value in USD."""

    if update.effective_chat.type == ChatType.PRIVATE:
        await update.message.reply_text(
            "Use /setminbuy in the group whose buy announcements you want to configure."
        )
        return
    if not await require_admin(update, context):
        return

    chat_id = update.effective_chat.id
    _track_chat(chat_id)
    args = [str(arg).strip() for arg in (context.args or [])]
    key = _get_buybot_minimum_usd_key(chat_id)

    if not args:
        minimum = _get_buybot_minimum_usd(chat_id)
        status = _format_volume_usd(minimum) if minimum is not None else "off"
        await update.message.reply_text(
            f"Minimum announced buy: {status}\n\n"
            "Usage: /setminbuy &lt;USD amount&gt;\n"
            "Example: /setminbuy 5\n"
            "Use /setminbuy off (or 0) to announce buys of any size.",
            parse_mode=ParseMode.HTML,
        )
        return

    if len(args) != 1:
        await update.message.reply_text(
            "❌ Enter one USD amount, for example /setminbuy 5."
        )
        return

    raw_minimum = args[0]
    if raw_minimum.lower() in {"off", "none", "clear"}:
        minimum = Decimal(0)
    else:
        minimum = _nonnegative_decimal(raw_minimum)
        if minimum is None:
            await update.message.reply_text(
                "❌ Enter a valid non-negative USD amount, for example /setminbuy .5."
            )
            return

    if minimum == 0:
        if key in db:
            del db[key]
        await update.message.reply_text(
            "✅ The minimum buy has been removed. Buys of any size can be announced."
        )
        return

    db[key] = str(minimum)
    await update.message.reply_text(
        f"✅ Only buys worth at least {_format_volume_usd(minimum)} will be announced."
    )


async def setairdropwallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admins to configure an encrypted, per-group airdrop wallet in DM."""
    if update.effective_chat.type == ChatType.PRIVATE:
        active_chat_id = _get_airdrop_wallet_flows(context).get(update.effective_user.id)
        if active_chat_id:
            await update.message.reply_text(
                'You already have a secure airdrop wallet setup in progress here. Send the private key for a dedicated airdrop hot wallet, type <code>remove</code> to clear it, or use <code>/cancel</code> to abort.',
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text(
                'Use /setairdropwallet in the group where you want to configure the sender wallet, and I will give you a secure DM link.',
            )
        return

    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    existing_wallet = await asyncio.to_thread(get_airdrop_wallet, update.effective_chat.id)
    existing_wallet_text = (
        f"Current group sender: <code>{html.escape(existing_wallet['wallet_address'])}</code>\n\n"
        if existing_wallet and existing_wallet.get("wallet_address")
        else ""
    )

    if not os.environ.get(ENCRYPTION_KEY_ENV):
        await update.message.reply_text(
            (
                "❌ Secure per-group airdrop wallets are not enabled yet.\n\n"
                f"The bot operator must set <code>{html.escape(ENCRYPTION_KEY_ENV)}</code> to a 32-byte hex key first."
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        bot_username = context.bot.username
        if not bot_username:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

        chat_id = update.effective_chat.id
        keyboard = [[InlineKeyboardButton('🔐 Configure Group Airdrop Wallet', url=f'https://t.me/{bot_username}?start=airdropwallet_{chat_id}')]]
        await update.message.reply_text(
            (
                f"{existing_wallet_text}"
                "To protect the private key, continue in a private chat with me.\n"
                "Only group admins can set or replace the group's airdrop wallet."
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as e:
        logging.error(f'Error starting airdrop wallet flow for chat {update.effective_chat.id}: {e}')
        await update.message.reply_text('There was an error generating the secure setup link. Please try again.')


async def airdrop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Airdrops tokens to top users from a /score leaderboard via SUI blockchain.

    Must be used as a reply to a leaderboard message generated by /score.
    """
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: Reply to a /score leaderboard message with:\n"
            "/airdrop &lt;count&gt; &lt;amount&gt;\n\n"
            "Example:\n"
            "1. Run /score 30 days\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /airdrop 10 500\n\n"
            "<i>Count = number of top leaderboard users to receive tokens.\n"
            "Amount = token amount per user. Decimals are read from coin metadata when available; most Sui coins use 9 decimals.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        count = int(context.args[0])
    except ValueError:
        await update.message.reply_text('❌ Count must be a whole number.')
        return

    if count < 1:
        await update.message.reply_text('❌ Count must be a positive number.')
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Please reply to a /score leaderboard message to use /airdrop.\n\n"
            "1. Run /score (e.g. /score 30 days)\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /airdrop &lt;count&gt; &lt;amount&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    replied_msg_id = update.message.reply_to_message.message_id
    leaderboard = _get_leaderboard_messages(context).get((chat_id, replied_msg_id))

    if not leaderboard:
        await update.message.reply_text('❌ The replied message is not a recognized leaderboard. Please reply to a leaderboard broadcasted by /score.')
        return

    coin_type = db.get(_get_airdrop_token_key(chat_id), DEFAULT_SUI_COIN_TYPE)
    coin_amount_config = await get_coin_amount_config(coin_type)
    try:
        amount = parse_token_amount(context.args[1], coin_amount_config.get('decimals', DEFAULT_SUI_COIN_DECIMALS))
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid amount per user: {html.escape(str(e))}", parse_mode=ParseMode.HTML)
        return
    top_entries = leaderboard[:count]
    if not top_entries:
        await update.message.reply_text('❌ No eligible users found in the leaderboard.')
        return

    results = []
    recipients_with_wallets = []
    skip_count = 0

    for username, _metrics, _message_count, user_id_str in top_entries:
        safe_username = html.escape(username)
        wallet_data = await asyncio.to_thread(get_wallet, chat_id, int(user_id_str))
        if not wallet_data or not wallet_data.get('wallet_address'):
            results.append(f'⏭️ @{safe_username}: No wallet registered — skipped')
            skip_count += 1
            continue
        recipients_with_wallets.append((username, wallet_data['wallet_address']))

    if not recipients_with_wallets:
        await update.message.reply_text('❌ None of the selected leaderboard users have wallets registered.')
        return

    try:
        sender_config = await asyncio.to_thread(resolve_airdrop_sender, chat_id)
    except Exception as e:
        logging.error(f'Failed to resolve airdrop sender for chat {chat_id}: {e}')
        await update.message.reply_text(f'❌ {html.escape(str(e))}', parse_mode=ParseMode.HTML)
        return

    if not sender_config:
        await update.message.reply_text(
            '❌ No airdrop wallet is configured for this group. Use /setairdropwallet, or configure the legacy SUI_PRIVATE_KEY fallback.',
        )
        return

    try:
        preflight = await preflight_airdrop(sender_config['wallet_address'], len(recipients_with_wallets), amount, coin_type)
    except Exception as e:
        logging.error(f'Airdrop preflight failed for chat {chat_id}: {e}')
        await update.message.reply_text(f'❌ Airdrop preflight failed: {html.escape(str(e))}', parse_mode=ParseMode.HTML)
        return

    preflight_lines = [
        f"🪂 Starting airdrop of <code>{html.escape(coin_type)}</code> to {len(recipients_with_wallets)} users.",
        f"Sender: <code>{html.escape(_short_address(sender_config['wallet_address']))}</code> ({html.escape(sender_config['source'])})",
        f"Preflight: {format_token_amount(preflight['available_sui_balance'], DEFAULT_SUI_COIN_DECIMALS)} SUI available / {format_token_amount(preflight['required_sui_balance'], DEFAULT_SUI_COIN_DECIMALS)} SUI required",
    ]
    if coin_type != DEFAULT_SUI_COIN_TYPE:
        preflight_lines.append(
            f"Token preflight: {format_token_amount(preflight['available_token_balance'], coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])} available / {format_token_amount(preflight['required_token_balance'], coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])} required"
        )
    await update.message.reply_text("\n".join(preflight_lines), parse_mode=ParseMode.HTML)

    success_count = 0
    fail_count = 0
    total_recipients = len(recipients_with_wallets)

    # Send a live progress message for larger airdrops so the group knows it's working.
    progress_msg = None
    if total_recipients > 3:
        progress_msg = await update.message.reply_text(
            f"⏳ Sending transfers... (0 / {total_recipients})", parse_mode=ParseMode.HTML
        )

    for idx, (username, wallet_address) in enumerate(recipients_with_wallets, start=1):
        safe_username = html.escape(username)
        try:
            tx_result = await sui_transfer_token(wallet_address, amount, coin_type, sender_config['private_key_hex'])
            tx_digest = tx_result.get('digest', 'unknown')
            results.append(f'✅ @{safe_username}: <code>{tx_digest}</code>')
            success_count += 1
        except Exception as e:
            logging.error(f'Airdrop transfer failed for {username} ({wallet_address}): {e}')
            results.append(f'❌ @{safe_username}: {html.escape(str(e)[:60])}')
            fail_count += 1

        if progress_msg and idx % _AIRDROP_PROGRESS_UPDATE_INTERVAL == 0 and idx < total_recipients:
            try:
                await progress_msg.edit_text(
                    f"⏳ Sending transfers... ({idx} / {total_recipients})", parse_mode=ParseMode.HTML
                )
            except Exception:
                pass

    if progress_msg:
        try:
            await progress_msg.delete()
        except Exception:
            pass

    results_text = "\n".join(results)
    summary = (
        f"🪂 <b>Airdrop Complete</b>\n\n"
        f"Token: <code>{html.escape(coin_type)}</code>\n"
        f"Amount per user: {format_token_amount(amount, coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])}\n"
        f"✅ Sent: {success_count} | ⏭️ Skipped (no wallet): {skip_count} | ❌ Failed: {fail_count}\n\n"
        f"<b>Details:</b>\n{results_text}"
    )
    await update.message.reply_text(summary + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def raffle_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Picks a weighted winner from a replied leaderboard and airdrops the prize."""
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    if len(context.args) < 1:
        await update.message.reply_text(
            "Usage: Reply to a /score leaderboard message with:\n"
            "/raffle &lt;amount&gt;\n\n"
            "Example:\n"
            "1. Run /score 30 days\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /raffle 500\n\n"
            "<i>The winner is selected from the top 20 ranked users with registered wallets, with slightly better odds for higher ranks.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_id = update.effective_chat.id
    coin_type = db.get(_get_airdrop_token_key(chat_id), DEFAULT_SUI_COIN_TYPE)
    coin_amount_config = await get_coin_amount_config(coin_type)
    try:
        amount = parse_token_amount(context.args[0], coin_amount_config.get('decimals', DEFAULT_SUI_COIN_DECIMALS))
    except ValueError as e:
        await update.message.reply_text(f"❌ Invalid raffle prize amount: {html.escape(str(e))}", parse_mode=ParseMode.HTML)
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Please reply to a /score leaderboard message to use /raffle.\n\n"
            "1. Run /score (e.g. /score 30 days)\n"
            "2. Broadcast the leaderboard to the group\n"
            "3. Reply to that leaderboard message with /raffle &lt;amount&gt;",
            parse_mode=ParseMode.HTML,
        )
        return

    replied_msg_id = update.message.reply_to_message.message_id
    leaderboard = _get_leaderboard_messages(context).get((chat_id, replied_msg_id))

    if not leaderboard:
        await update.message.reply_text('❌ The replied message is not a recognized leaderboard. Please reply to a leaderboard broadcasted by /score.')
        return

    weighted_candidates = []

    for rank, (username, _metrics, _message_count, user_id_str) in enumerate(leaderboard[:RAFFLE_MAX_RANK], start=1):
        wallet_data = await asyncio.to_thread(get_wallet, chat_id, int(user_id_str))
        if not wallet_data or not wallet_data.get('wallet_address'):
            continue
        weighted_candidates.append(
            {
                'rank': rank,
                'username': username or f'user_{user_id_str}',
                'wallet_address': wallet_data['wallet_address'],
            }
        )

    if not weighted_candidates:
        await update.message.reply_text(f'❌ None of the top {RAFFLE_MAX_RANK} leaderboard users have wallets registered.')
        return

    winner = select_weighted_raffle_winner(weighted_candidates)
    if not winner:
        await update.message.reply_text('❌ Could not select a raffle winner.')
        return

    try:
        sender_config = await asyncio.to_thread(resolve_airdrop_sender, chat_id)
    except Exception as e:
        logging.error(f'Failed to resolve raffle sender for chat {chat_id}: {e}')
        await update.message.reply_text(f'❌ {html.escape(str(e))}', parse_mode=ParseMode.HTML)
        return

    if not sender_config:
        await update.message.reply_text(
            '❌ No airdrop wallet is configured for this group. Use /setairdropwallet, or configure the legacy SUI_PRIVATE_KEY fallback.',
        )
        return

    try:
        preflight = await preflight_airdrop(sender_config['wallet_address'], 1, amount, coin_type)
    except Exception as e:
        logging.error(f'Raffle preflight failed for chat {chat_id}: {e}')
        await update.message.reply_text(f'❌ Raffle preflight failed: {html.escape(str(e))}', parse_mode=ParseMode.HTML)
        return

    preflight_lines = [
        f"🎟️ Running a raffle for <code>{html.escape(coin_type)}</code> across {len(weighted_candidates)} registered wallets from the top {RAFFLE_MAX_RANK}.",
        f"Winner odds are weighted slightly by leaderboard place.",
        f"Sender: <code>{html.escape(_short_address(sender_config['wallet_address']))}</code> ({html.escape(sender_config['source'])})",
        f"Preflight: {format_token_amount(preflight['available_sui_balance'], DEFAULT_SUI_COIN_DECIMALS)} SUI available / {format_token_amount(preflight['required_sui_balance'], DEFAULT_SUI_COIN_DECIMALS)} SUI required",
    ]
    if coin_type != DEFAULT_SUI_COIN_TYPE:
        preflight_lines.append(
            f"Token preflight: {format_token_amount(preflight['available_token_balance'], coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])} available / {format_token_amount(preflight['required_token_balance'], coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])} required"
        )
    await update.message.reply_text("\n".join(preflight_lines), parse_mode=ParseMode.HTML)

    safe_username = html.escape(winner['username'])
    try:
        tx_result = await sui_transfer_token(winner['wallet_address'], amount, coin_type, sender_config['private_key_hex'])
    except Exception as e:
        logging.error(f"Raffle transfer failed for {winner['username']} ({winner['wallet_address']}): {e}")
        await update.message.reply_text(
            (
                "❌ <b>Raffle draw failed</b>\n\n"
                f"Winner: #{winner['rank']} @{safe_username}\n"
                f"Wallet: <code>{html.escape(winner['wallet_address'])}</code>\n"
                f"Error: {html.escape(str(e))}"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    tx_digest = tx_result.get('digest', 'unknown')
    summary = (
        "🎉 <b>Raffle Complete</b>\n\n"
        f"Token: <code>{html.escape(coin_type)}</code>\n"
        f"Prize: {format_token_amount(amount, coin_amount_config['decimals'])} {html.escape(coin_amount_config['symbol'])}\n"
        f"Winner: #{winner['rank']} @{safe_username}\n"
        f"Wallet: <code>{html.escape(winner['wallet_address'])}</code>\n"
        f"Eligible wallets: {len(weighted_candidates)} / {RAFFLE_MAX_RANK}\n"
        f"Transaction: <code>{html.escape(tx_digest)}</code>"
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
            "Try the full name (e.g., 'bitcoin') or symbol (e.g., 'BTC', 'SUI', 'DEEP', 'IKA', 'WAL', 'NS').",
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

    await update.message.reply_text(
        response + FOOTER_HTML,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the help message with all available commands."""
    await update.message.reply_text(HELP_TEXT + FOOTER_HTML, parse_mode='HTML')


async def mybadges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the user's earned badges."""
    chat_id = update.effective_chat.id
    if update.effective_chat.type != ChatType.PRIVATE and not db.get(_get_achievements_enabled_key(chat_id), False):
        await update.message.reply_text("Achievement tracking is currently disabled in this group. Admins can enable it using `/setachievements on`.")
        return

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
    chat_id = update.effective_chat.id
    if update.effective_chat.type != ChatType.PRIVATE and not db.get(_get_achievements_enabled_key(chat_id), False):
        await update.message.reply_text("Achievement tracking is currently disabled in this group. Admins can enable it using `/setachievements on`.")
        return

    message = "🏆 <b>All Available Badges</b>\n\n"
    for badge_id, badge in BADGES.items():
        message += f"{badge['emoji']} <b>{badge['name']}</b>: {badge['description']}\n"

    await update.message.reply_text(message + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _track_chat(update.effective_chat.id)
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
    """Sends the user their personal stats as a reply in the current chat."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    _track_chat(chat_id)
    await asyncio.to_thread(_ensure_messages_migrated, chat_id)
    rank_row = await asyncio.to_thread(db.get_user_rank, chat_id, user_id)
    if rank_row:
        rank, user_message_count = rank_row
    else:
        rank, user_message_count = -1, 0

    badges_key = _get_badges_key(chat_id, user_id)
    user_badges = db.get(badges_key, [])
    if user_badges:
        badge_text = '<b>Badges Earned:</b> ' + ' '.join([BADGES[b]['emoji'] for b in user_badges if b in BADGES])
    else:
        badge_text = '<b>Badges Earned:</b> None yet!'

    stats_message = (
        f"📊 <b>Your Stats for this Group</b>\n\n"
        f"<b>Total Messages:</b> {user_message_count}\n"
        f"<b>All-Time Rank:</b> {'N/A' if rank == -1 else f'#{rank}'}\n"
        f"{badge_text}"
    )

    await update.message.reply_text(stats_message + FOOTER_HTML, parse_mode=ParseMode.HTML)


async def calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await require_admin(update, context):
        return ConversationHandler.END
    _track_chat(update.effective_chat.id)
    now = datetime.datetime.now()
    await update.message.reply_text(
        "📅 <b>Event Calendar</b>\nClick a date to add or view an event.",
        reply_markup=generate_calendar_keyboard(now.year, now.month, update.effective_chat.id),
        parse_mode=ParseMode.HTML,
    )
    return SELECTING_ACTION


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _track_chat(chat_id)
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
    if not await require_admin(update, context):
        return

    _track_chat(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text(
            "Usage: /settimezone <Timezone>\n"
            "Example: /settimezone America/New_York\n"
            "Find timezones here: https://en.wikipedia.org/wiki/List_of_tz_database_time_zones"
        )
        return

    tz_name = context.args[0]
    try:
        pytz.timezone(tz_name)
        db[_get_timezone_key(update.effective_chat.id)] = tz_name
        await update.message.reply_text(f'✅ Timezone successfully set to {tz_name}.')
    except pytz.UnknownTimeZoneError:
        await update.message.reply_text(f'❌ Unknown timezone: {tz_name}. Please use a valid timezone name.')


async def wallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /wallet command to initiate wallet submission/checking."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    if update.effective_chat.type == 'private':
        await update.message.reply_text('Please use this command in a group chat to submit your wallet for that group.')
        return

    _track_chat(chat_id)
    try:
        bot_username = context.bot.username
        if not bot_username:
            bot_info = await context.bot.get_me()
            bot_username = bot_info.username

        keyboard = [[InlineKeyboardButton('🔒 Submit or Check Wallet', url=f'https://t.me/{bot_username}?start=wallet_{chat_id}')]]
        await update.message.reply_text(
            'To protect your privacy, please click the button below to submit or check your wallet in a private chat with me.',
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        logging.info(f'Wallet command used by user {user_id} in chat {chat_id}')
    except Exception as e:
        logging.error(f'Error in wallet command for user {user_id} in chat {chat_id}: {e}')
        await update.message.reply_text('There was an error generating the wallet submission link. Please try again.')


async def removewallet_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes the user's registered wallet for the current group."""
    if update.effective_chat.type == 'private':
        await update.message.reply_text('Please use this command in a group chat to remove your wallet for that group.')
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    chat_id_str = str(chat_id)
    user_id_str = str(user_id)

    wallet_key = _get_wallet_key(chat_id_str, user_id_str)
    old_wallet_key = f"{chat_id_str}:wallet:{user_id_str}"

    removed = False
    if wallet_key in db:
        del db[wallet_key]
        removed = True
    if old_wallet_key in db:
        del db[old_wallet_key]
        removed = True

    if removed:
        await update.message.reply_text('✅ Your wallet has been removed for this group. You will be skipped in future airdrops.')
        logging.info(f'Wallet removed for user {user_id} in chat {chat_id}')
    else:
        await update.message.reply_text("You don't have a wallet registered for this group.")


async def receive_airdrop_private_key(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives, encrypts, and stores the group's airdrop private key."""
    submitted_value = update.message.text.strip()
    user_id = update.effective_user.id
    target_chat_id = _get_airdrop_wallet_flows(context).get(user_id)
    # Keep the DM flow active when validation/storage fails so the admin can retry without restarting from the group.
    should_clear_flow = True

    if not target_chat_id:
        await update.message.reply_text('Error: Could not find the original group. Please start the process again from the group chat.')
        logging.error(f'Missing airdrop target_chat_id for user {user_id}')
        return ConversationHandler.END

    try:
        chat_info = await context.bot.get_chat(target_chat_id)
        chat_name = chat_info.title or f'Group (ID: {target_chat_id})'
        try:
            await update.message.delete()
        except Exception:
            logging.warning('Could not delete private key submission message for user %s', user_id)

        if submitted_value.lower() == 'remove':
            await asyncio.to_thread(delete_airdrop_wallet, target_chat_id)
            await update.message.reply_text(
                f"🗑️ Removed the stored airdrop wallet for <b>{html.escape(chat_name)}</b>.",
                parse_mode=ParseMode.HTML,
            )
            logging.info(f'Removed airdrop wallet for chat {target_chat_id} by user {user_id}')
            return ConversationHandler.END

        normalized_private_key = normalize_sui_private_key(submitted_value)
        if not normalized_private_key:
            should_clear_flow = False
            await update.message.reply_text(
                '❌ Please send a valid SUI private key (<code>suiprivkey1...</code> or 64 hexadecimal characters, optionally prefixed with <code>0x</code>), send <code>remove</code>, or type /cancel.',
                parse_mode=ParseMode.HTML,
            )
            return AWAITING_AIRDROP_PRIVATE_KEY

        wallet_data = await asyncio.to_thread(store_airdrop_wallet, target_chat_id, user_id, normalized_private_key)
        await update.message.reply_text(
            (
                f"✅ Stored an encrypted airdrop wallet for <b>{html.escape(chat_name)}</b>.\n\n"
                f"Derived sender address: <code>{html.escape(wallet_data['wallet_address'])}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
        logging.info(f'Configured airdrop wallet for chat {target_chat_id} by user {user_id}')
    except Exception as e:
        should_clear_flow = False
        logging.error(f'Error storing airdrop wallet for chat {target_chat_id}: {e}')
        await update.message.reply_text(f'❌ Error storing the airdrop wallet: {html.escape(str(e))}', parse_mode=ParseMode.HTML)
        return AWAITING_AIRDROP_PRIVATE_KEY
    finally:
        if should_clear_flow:
            _get_airdrop_wallet_flows(context).pop(user_id, None)

    return ConversationHandler.END


async def receive_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receives and validates the wallet address from the user."""
    submitted_wallet = update.message.text.strip()
    normalized_wallet = normalize_wallet_address(submitted_wallet)
    user_id = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name

    logging.info(f'Received wallet address from user {user_id}: {submitted_wallet[:10]}...')

    if not normalized_wallet:
        await update.message.reply_text('❌ Please send a valid SUI wallet address (64 hexadecimal characters, optionally prefixed with 0x) or type /cancel.')
        return AWAITING_WALLET

    target_chat_id = _get_wallet_flows(context).get(user_id)
    if not target_chat_id:
        await update.message.reply_text('Error: Could not find the original group. Please start the process again from the group chat.')
        logging.error(f'Missing target_chat_id for user {user_id}')
        return ConversationHandler.END

    try:
        success = await asyncio.to_thread(store_wallet, target_chat_id, user_id, username, normalized_wallet)
        chat_info = await context.bot.get_chat(target_chat_id)
        chat_name = chat_info.title or f'Group (ID: {target_chat_id})'
        if success:
            await update.message.reply_text(
                f"✅ Wallet address `{normalized_wallet}` successfully submitted for *{escape_markdown(chat_name)}*\\.",
                parse_mode='MarkdownV2',
            )
            logging.info(f'Wallet successfully stored for user {user_id} in chat {target_chat_id}')
        else:
            await update.message.reply_text('❌ Error storing wallet address. Please try again.')
            logging.error(f'Wallet storage failed for user {user_id}')
    except Exception as e:
        logging.error(f"Error in wallet submission for '{username}' (ID: {user_id}): {e}")
        await update.message.reply_text('❌ Error storing wallet address. Please try again.')
    finally:
        _get_wallet_flows(context).pop(user_id, None)

    return ConversationHandler.END


async def handle_calendar_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query, data, chat_id = update.callback_query, update.callback_query.data, update.callback_query.message.chat_id
    user = query.from_user
    if not await user_is_admin(context, chat_id, user.id):
        await query.answer('❌ Only administrators can interact with the calendar.', show_alert=True)
        return SELECTING_ACTION

    await query.answer()
    try:
        parts = data.split('_')
        action = parts[1]

        if action == 'nav':
            _, _, year, month = parts
            await query.edit_message_text(
                text="📅 <b>Event Calendar</b>\nClick a date to add or view an event.",
                reply_markup=generate_calendar_keyboard(int(year), int(month), chat_id),
                parse_mode=ParseMode.HTML,
            )
            return SELECTING_ACTION
        if action == 'day':
            _, _, year, month, day = parts
            selected_date = datetime.date(int(year), int(month), int(day))
            event_text = db.get(_get_event_key(chat_id, selected_date))
            _get_calendar_sessions(context)[(chat_id, user.id)] = selected_date.isoformat()
            keyboard = [[InlineKeyboardButton('Delete Event', callback_data=f'cal_delete_{selected_date.isoformat()}'), InlineKeyboardButton('Back to Calendar', callback_data='cal_back')]] if event_text else []
            msg = (
                f"🗓️ <b>Event on {selected_date:%B %d, %Y}</b>\n\n{event_text}\n\nReply to overwrite."
                if event_text
                else f"📝 No event for {selected_date:%B %d, %Y}. Reply with event text."
            )
            await query.edit_message_text(msg, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None, parse_mode=ParseMode.HTML)
            return AWAITING_EVENT_TEXT
        if action == 'delete':
            date_str = parts[2]
            del db[_get_event_key(chat_id, datetime.date.fromisoformat(date_str))]
            await query.answer('Event deleted!', show_alert=True)
        elif action == 'close':
            await query.message.delete()
            return ConversationHandler.END

        now = datetime.datetime.now()
        await query.edit_message_text('📅 <b>Event Calendar</b>', reply_markup=generate_calendar_keyboard(now.year, now.month, chat_id), parse_mode=ParseMode.HTML)
        return SELECTING_ACTION
    except Exception as e:
        logging.error(f"Error in calendar interaction '{data}': {e}")
        await query.answer('An error occurred.', show_alert=True)
        return SELECTING_ACTION


async def handle_event_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not await user_is_admin(context, chat_id, user_id):
        await update.message.reply_text('❌ Only administrators can add events.')
        _get_calendar_sessions(context).pop((chat_id, user_id), None)
        return SELECTING_ACTION

    selected_date_iso = _get_calendar_sessions(context).pop((chat_id, user_id), None)
    if not selected_date_iso:
        await update.message.reply_text('Action timed out. Please select a date again.')
        return ConversationHandler.END
    selected_date = datetime.date.fromisoformat(selected_date_iso)
    _track_chat(chat_id)
    event_text = update.message.text
    db[_get_event_key(chat_id, selected_date)] = event_text
    await update.message.reply_text(
        f'✅ Event for {selected_date:%B %d, %Y} scheduled!\n\n'
        f'<blockquote>{html.escape(event_text)}</blockquote>',
        parse_mode=ParseMode.HTML,
    )
    now = datetime.datetime.now()
    await update.message.reply_text('📅 <b>Event Calendar</b>', reply_markup=generate_calendar_keyboard(now.year, now.month, chat_id), parse_mode=ParseMode.HTML)
    return SELECTING_ACTION


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('Action cancelled.')
    _get_wallet_flows(context).pop(update.effective_user.id, None)
    _get_airdrop_wallet_flows(context).pop(update.effective_user.id, None)
    _get_calendar_sessions(context).pop((update.effective_chat.id, update.effective_user.id), None)
    return ConversationHandler.END


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, data, user_id = update.callback_query, update.callback_query.data, update.callback_query.from_user.id
    await query.answer()

    if data.startswith('score_'):
        token = data.rsplit('_', 1)[-1]
        score_params = _get_score_requests(context).get(token)
        if not score_params:
            await query.edit_message_text('❌ Session expired. Please run /score again.')
            return
        if user_id != score_params['requester_id']:
            await query.answer('❌ Only the requester can use these buttons.', show_alert=True)
            return
        await query.edit_message_text('🔍 Generating leaderboard...')
        try:
            result, error = await generate_leaderboard(
                context,
                score_params['chat_id'],
                score_params['start_date'],
                score_params['end_date'],
                score_params['start_str'],
                score_params['end_str'],
                score_params['export'],
            )
            if error:
                await query.edit_message_text(f'❌ {error}')
                return
            leaderboard_text, csv_data, raw_data = result
            _get_score_results(context)[token] = {
                'requester_id': user_id,
                'chat_id': score_params['chat_id'],
                'csv_data': csv_data,
                'raw_data': raw_data,
            }
            is_private = data.startswith('score_private_')
            target_chat_id = user_id if is_private else score_params['chat_id']
            confirm_text = '✅ Leaderboard sent to your private chat!' if is_private else '✅ Leaderboard broadcasted!'
            keyboard = [[InlineKeyboardButton('📄 Download CSV', callback_data=f'export_csv_{token}')]]
            sent_msg = await context.bot.send_message(target_chat_id, leaderboard_text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(keyboard))
            if not is_private:
                _get_leaderboard_messages(context)[(target_chat_id, sent_msg.message_id)] = raw_data
            if csv_data and score_params['export']:
                await context.bot.send_document(target_chat_id, document=io.BytesIO(csv_data), filename='leaderboard.csv')
            _get_score_requests(context).pop(token, None)
            await query.edit_message_text(confirm_text)
        except Exception as e:
            logging.error(f'Error in leaderboard generation: {e}')
            await query.edit_message_text('⚠️ An error occurred during generation.')

    elif data.startswith('export_csv_'):
        token = data.rsplit('_', 1)[-1]
        leaderboard_data = _get_score_results(context).get(token)
        if not leaderboard_data:
            await query.answer('❌ Leaderboard data has expired. Please run /score again.', show_alert=True)
            return
        if user_id != leaderboard_data['requester_id']:
            await query.answer('❌ Only the requester can download the CSV.', show_alert=True)
            return

        csv_data = leaderboard_data['csv_data']
        raw_data = leaderboard_data['raw_data']
        if not csv_data:
            csv_data = await generate_csv_from_leaderboard(raw_data, leaderboard_data['chat_id'])
            leaderboard_data['csv_data'] = csv_data

        if csv_data:
            await context.bot.send_document(chat_id=user_id, document=io.BytesIO(csv_data), filename='leaderboard_export.csv', caption='Here is your leaderboard CSV export.')
            await query.answer('✅ CSV sent to your private chat.', show_alert=True)
        else:
            await query.answer('❌ Could not generate CSV data.', show_alert=True)


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
        BotCommand("raffle", "Raffle prize: reply to /score with /raffle <amount>"),
        BotCommand("setairdropwallet", "Set this group's encrypted airdrop wallet (admin)"),
        BotCommand("settoken", "Set airdrop token (admin)"),
        BotCommand("setbuybot", "Toggle selected-token buy announcements (admin)"),
        BotCommand("setbuyimage", "Set a custom buy image, GIF, or video (admin)"),
        BotCommand("setminbuy", "Set minimum announced buy value in USD (admin)"),
        BotCommand("mybadges", "View your earned badges"),
        BotCommand("allbadges", "See all available badges"),
        BotCommand("mystats", "View your personal stats"),
        BotCommand("calendar", "View and manage events (admin only)"),
        BotCommand("events", "List all upcoming events"),
        BotCommand("settimezone", "Set timezone for announcements (admin only)"),
        BotCommand("setwelcome", "Toggle welcome messages (admin)"),
        BotCommand("nameguard", "Toggle join impersonation protection (admin)"),
        BotCommand("setachievements", "Toggle achievement tracking (admin)"),
        BotCommand("stats", "Show chat statistics"),
        BotCommand("wallet", "Submit or check your wallet address"),
        BotCommand("removewallet", "Remove your registered wallet from this group"),
        BotCommand("cancel", "Cancel current operation")
    ]
    private_commands = [
        BotCommand("start", "Show help and command list"),
        BotCommand("help", "Show help and command list"),
        BotCommand("price", "Crypto price: /price <symbol>"),
        BotCommand("wallet", "Submit or check your wallet address"),
        BotCommand("removewallet", "Remove your registered wallet from this group"),
        BotCommand("mybadges", "View your earned badges"),
        BotCommand("allbadges", "See all available badges"),
        BotCommand("mystats", "View your personal stats"),
        BotCommand("copypasta", "Generate a copypasta of your history"),
        BotCommand("cancel", "Cancel current operation"),
    ]
    await asyncio.gather(
        application.bot.set_my_commands(commands, scope=BotCommandScopeDefault()),
        application.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats()),
        application.bot.set_my_commands(private_commands, scope=BotCommandScopeAllPrivateChats()),
    )

async def shutdown_services(application):
    await asyncio.gather(
        close_shared_async_client(),
        close_sui_service(),
    )


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
    job_queue.run_repeating(check_sui_buys, interval=_BUYBOT_POLL_SECONDS, first=5)


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
            AWAITING_AIRDROP_PRIVATE_KEY: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_airdrop_private_key)],
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
    application.add_handler(CommandHandler("raffle", raffle_command))
    application.add_handler(CommandHandler("setairdropwallet", setairdropwallet_command))
    application.add_handler(CommandHandler("settoken", settoken_command))
    application.add_handler(CommandHandler("setbuybot", setbuybot_command))
    application.add_handler(CommandHandler("setbuyimage", setbuyimage_command))
    application.add_handler(CommandHandler("setminbuy", setminbuy_command))
    application.add_handler(CommandHandler("setwelcome", setwelcome_command))
    application.add_handler(CommandHandler("nameguard", nameguard_command))
    application.add_handler(CommandHandler("setachievements", setachievements_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("mybadges", mybadges_command))
    application.add_handler(CommandHandler("allbadges", allbadges_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("events", events_command))
    application.add_handler(CommandHandler("settimezone", set_timezone_command))
    application.add_handler(CommandHandler("wallet", wallet_command))
    application.add_handler(CommandHandler("removewallet", removewallet_command))
    application.add_handler(CallbackQueryHandler(button_callback, pattern="^(score_|export_csv_)"))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome_new_member))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, store_message))
    application.add_error_handler(error_handler)
    application.post_init = setup_bot_commands
    application.post_shutdown = shutdown_services

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
