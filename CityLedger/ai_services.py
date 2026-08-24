import copy
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict

from openai import OpenAI

from telegram_utils import sanitize_html_for_telegram

logger = logging.getLogger(__name__)

_openai_client = None

AI_MODEL = "gpt-5-nano"
AI_REASONING_EFFORT = "minimal"
AI_RESULT_CACHE_TTL_SECONDS = 15 * 60
AI_RESULT_CACHE_MAX_ENTRIES = 512

# Bump only the affected entry when a prompt or output contract changes. The
# version is part of every cache key so stale results can never cross a prompt
# migration boundary.
_PROMPT_VERSIONS = {
    "analyze_user_messages": "1",
    "summarize_chat_history": "1",
    "get_best_of_messages": "1",
    "get_vibe_check": "1",
    "generate_copypasta": "1",
}
_CACHE_MISS = object()


class _AIResultCache:
    """Small thread-safe TTL/LRU cache with per-key request coalescing."""

    def __init__(self, ttl_seconds: int, max_entries: int):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._items = OrderedDict()
        self._inflight: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def reserve(self, key: str):
        """Return a cached value or reserve *key* for the calling thread."""

        while True:
            with self._lock:
                now = time.monotonic()
                expired = [
                    item_key
                    for item_key, (expires_at, _) in self._items.items()
                    if expires_at <= now
                ]
                for item_key in expired:
                    self._items.pop(item_key, None)

                cached = self._items.get(key)
                if cached is not None:
                    self._items.move_to_end(key)
                    return copy.deepcopy(cached[1]), None

                event = self._inflight.get(key)
                if event is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    return _CACHE_MISS, event

            # An identical request is already running. Wait for it to populate
            # the cache (or release the reservation after an error), then retry.
            event.wait()

    def store(self, key: str, value, reservation: threading.Event) -> None:
        with self._lock:
            self._items[key] = (
                time.monotonic() + self._ttl_seconds,
                copy.deepcopy(value),
            )
            self._items.move_to_end(key)
            while len(self._items) > self._max_entries:
                self._items.popitem(last=False)
            self._inflight.pop(key, None)
            reservation.set()

    def release(self, key: str, reservation: threading.Event) -> None:
        with self._lock:
            self._inflight.pop(key, None)
            reservation.set()

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            for event in self._inflight.values():
                event.set()
            self._inflight.clear()


_ai_result_cache = _AIResultCache(
    ttl_seconds=AI_RESULT_CACHE_TTL_SECONDS,
    max_entries=AI_RESULT_CACHE_MAX_ENTRIES,
)


def clear_ai_result_cache() -> None:
    """Clear cached AI results. Primarily useful for tests and maintenance."""

    _ai_result_cache.clear()


def _completion_cache_key(
    operation: str,
    messages: list[dict],
    max_completion_tokens: int,
    response_format: dict | None,
) -> str:
    request_identity = {
        "model": AI_MODEL,
        "reasoning_effort": AI_REASONING_EFFORT,
        "operation": operation,
        "prompt_version": _PROMPT_VERSIONS[operation],
        "messages": messages,
        "max_completion_tokens": max_completion_tokens,
        "response_format": response_format,
    }
    serialized = json.dumps(
        request_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _create_cached_completion(
    operation: str,
    messages: list[dict],
    *,
    max_completion_tokens: int,
    timeout: float,
    response_format: dict | None = None,
    validator=None,
) -> str:
    """Create one GPT-5 nano completion, caching only validated successes."""

    cache_key = _completion_cache_key(
        operation,
        messages,
        max_completion_tokens,
        response_format,
    )
    cached, reservation = _ai_result_cache.reserve(cache_key)
    if cached is not _CACHE_MISS:
        return cached

    try:
        request = {
            "model": AI_MODEL,
            "messages": messages,
            "reasoning_effort": AI_REASONING_EFFORT,
            "max_completion_tokens": max_completion_tokens,
            "timeout": timeout,
        }
        if response_format is not None:
            request["response_format"] = response_format

        response = get_openai_client().chat.completions.create(**request)
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("OpenAI returned an empty completion")
        if validator is not None:
            validator(content)
    except Exception:
        _ai_result_cache.release(cache_key, reservation)
        raise

    _ai_result_cache.store(cache_key, content, reservation)
    return content


def get_openai_client():
    """Return a singleton OpenAI client instance."""
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def analyze_user_messages(username: str, message_count: int, messages_text: str) -> dict:
    """Score a user's messages on quality, tone, helpfulness, and humor (0–20 each)."""
    prompt = (
        f"User {username} posted {message_count} messages. "
        "Evaluate contributions on quality, tone, helpfulness, humor (0-20). "
        'Return valid JSON with keys: "quality", "tone", "helpfulness", "humor". '
        f"Messages:\n\n{messages_text}"
    )
    try:
        content = _create_cached_completion(
            "analyze_user_messages",
            [
                {"role": "system", "content": "You are an analytical assistant."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=300,
            timeout=30.0,
            validator=json.loads,
        )
        return json.loads(content)
    except Exception as exc:
        logger.error("Error analyzing messages for %s: %s", username, exc)
        return {"quality": 8, "tone": 10, "helpfulness": 8, "humor": 8}


def summarize_chat_history(chat_transcript: str, days: int = None, topic: str = None) -> str:
    """Use OpenAI to summarize a chat transcript, optionally focusing on a topic."""
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
        content = _create_cached_completion(
            "summarize_chat_history",
            [
                {
                    "role": "system",
                    "content": "You are a helpful assistant that provides detailed, chronologically ordered summaries of chat conversations in HTML format. Do not include `<html>`, `<head>`, or `<body>` tags in your response. Only use HTML tags like <b> for formatting.",
                },
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=1000,
            timeout=120.0,
        )
        return sanitize_html_for_telegram(content)
    except Exception as exc:
        logger.error("Error summarizing chat history: %s", exc)
        return "Sorry, I was unable to generate a summary at this time."


def get_best_of_messages(chat_transcript: str, days: int) -> str:
    """Use OpenAI to select the best messages from a transcript."""
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
        content = _create_cached_completion(
            "get_best_of_messages",
            [
                {
                    "role": "system",
                    "content": "You are a community moderator who highlights the best contributions in a chat using HTML formatting. Do not include `<html>`, `<head>`, or `<body>` tags in your response.",
                },
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=1100,
            timeout=120.0,
        )
        return sanitize_html_for_telegram(content)
    except Exception as exc:
        logger.error("Error getting 'best of' messages: %s", exc)
        return "Could not generate the 'Best Of' digest at this time."


def get_vibe_check(chat_transcript: str, topic: str = None) -> dict | None:
    """Use OpenAI to analyze the sentiment of a chat transcript."""
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
        content = _create_cached_completion(
            "get_vibe_check",
            [
                {
                    "role": "system",
                    "content": "You are a sentiment analysis expert who analyzes Telegram chat transcripts and responds with structured JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=800,
            timeout=120.0,
            validator=json.loads,
        )
        return json.loads(content)
    except Exception as exc:
        logger.error("Error getting vibe check: %s", exc)
        return None


def generate_copypasta(user_transcript: str) -> str:
    """Use OpenAI to generate a copypasta based on a user's message history."""
    prompt = f"""
Based on the following messages from a user, write a short, unhinged, and deranged-sounding copypasta that captures their personality, writing style, and common topics. The copypasta should be from their perspective.

Messages:
{user_transcript}
"""
    try:
        return _create_cached_completion(
            "generate_copypasta",
            [
                {"role": "system", "content": "You are a creative writer who specializes in internet humor and copypastas."},
                {"role": "user", "content": prompt},
            ],
            max_completion_tokens=400,
            timeout=60.0,
        )
    except Exception as exc:
        logger.error("Error generating copypasta: %s", exc)
        return "I tried to get weird, but something went wrong."
