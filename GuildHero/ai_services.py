import json
import logging
import os
from functools import lru_cache

from openai import OpenAI

from telegram_utils import sanitize_html_for_telegram

logger = logging.getLogger(__name__)

_openai_client = None


def get_openai_client():
    """Return a singleton OpenAI client instance."""
    global _openai_client
    if _openai_client is None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY environment variable not set")
        _openai_client = OpenAI(api_key=api_key)
    return _openai_client


@lru_cache(maxsize=128)
def _cached_analysis_response(username: str, message_count: int, messages_text: str) -> str:
    prompt = (
        f"User {username} posted {message_count} messages. "
        "Evaluate contributions on quality, tone, helpfulness, humor (0-20). "
        'Return valid JSON with keys: "quality", "tone", "helpfulness", "humor". '
        f"Messages:\n\n{messages_text}"
    )
    client = get_openai_client()
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You are an analytical assistant."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=100,
        timeout=30.0,
    )
    return response.choices[0].message.content.strip()


def analyze_user_messages(username: str, message_count: int, messages_text: str) -> dict:
    try:
        return json.loads(_cached_analysis_response(username, message_count, messages_text))
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
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "You are a helpful assistant that provides detailed, chronologically ordered summaries of chat conversations in HTML format. Do not include `<html>`, `<head>`, or `<body>` tags in your response. Only use HTML tags like <b> for formatting.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=700,
            timeout=120.0,
        )
        return sanitize_html_for_telegram(response.choices[0].message.content.strip())
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
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {
                    "role": "system",
                    "content": "You are a community moderator who highlights the best contributions in a chat using HTML formatting. Do not include `<html>`, `<head>`, or `<body>` tags in your response.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.6,
            max_tokens=800,
            timeout=120.0,
        )
        return sanitize_html_for_telegram(response.choices[0].message.content.strip())
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
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            response_format={"type": "json_object"},
            messages=[
                {
                    "role": "system",
                    "content": "You are a sentiment analysis expert who analyzes Telegram chat transcripts and responds with structured JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=600,
            timeout=120.0,
        )
        return json.loads(response.choices[0].message.content)
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
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a creative writer who specializes in internet humor and copypastas."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.8,
            max_tokens=250,
            timeout=60.0,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        logger.error("Error generating copypasta: %s", exc)
        return "I tried to get weird, but something went wrong."
