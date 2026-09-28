"""Validated contribution scores and deterministic, bounded activity signals."""
import datetime
import math
import re
import unicodedata
from collections import Counter

SCORE_FIELDS = ("quality", "tone", "helpfulness", "humor")


def validate_scores(value):
    if not isinstance(value, dict) or set(value) != set(SCORE_FIELDS):
        raise ValueError("Expected exactly quality, tone, helpfulness and humor.")
    for score in value.values():
        if type(score) not in (int, float) or not 0 <= score <= 20 or not math.isfinite(score):
            raise ValueError("Every category must be a finite number from 0 to 20.")
    return {field: float(value[field]) for field in SCORE_FIELDS}


def contribution_messages(messages):
    """Keep first occurrences and count capped substantive activity by UTC day.

    Short messages remain evidence for the AI, but do not inflate activity.
    Length is only an activity heuristic, not a judgement of helpfulness.
    """
    unique, seen, days = [], set(), Counter()
    for message in sorted(messages, key=lambda item: item['date']):
        text = unicodedata.normalize('NFKC', str(message.get('text') or ''))
        normalized = ' '.join(re.findall(r'\w+', text.casefold()))
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(message)
        words = re.findall(r'\w+', re.sub(r'https?://\S+', '', text), re.UNICODE)
        letters = ''.join(words)
        if len(letters) < 12 or len(set(letters.casefold())) < 4:
            continue
        date = datetime.datetime.fromisoformat(message['date'])
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        days[date.astimezone(datetime.timezone.utc).date()] += 1
    return unique, sum(min(count, 10) for count in days.values()), len(days)


def contribution_total(metrics, activity_count, active_days):
    scores = validate_scores(metrics)
    quality = (scores['quality'] * .50 + scores['helpfulness'] * .30
               + scores['tone'] * .15 + scores['humor'] * .05) * 5
    # Tone and humor alone cannot lift low-quality chatter to the top.
    quality *= min(1, scores['quality'] / 10)
    activity = .65 + .20 * math.log1p(min(activity_count, 100)) / math.log1p(100)
    activity += .15 * min(active_days, 7) / 7
    return round(min(100, max(0, quality * activity)), 2)


def score_transcript(messages, max_messages=200, max_chars=12000):
    """Spread bounded evidence across the full period, including long messages."""
    if not messages:
        return ''
    count = min(len(messages), max_messages, max(1, max_chars // 240))
    indices = [round(i * (len(messages) - 1) / max(1, count - 1)) for i in range(count)]
    per_line = max(1, (max_chars - count + 1) // count)
    lines = []
    for index in indices:
        message = messages[index]
        text = ' '.join(str(message.get('text') or '').split())
        prefix = '[reply] ' if message.get('is_reply') else ''
        line = prefix + text
        lines.append(line if len(line) <= per_line else line[:max(0, per_line - 1)] + '…')
    return '\n'.join(lines)[:max_chars]
