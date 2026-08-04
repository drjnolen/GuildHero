"""Pure name-matching helpers for Telegram join moderation."""

import unicodedata
from dataclasses import dataclass


PROTECTED_NAME_WORDS = frozenset({"dev", "admin", "support"})

# Common Cyrillic and Greek characters used to visually imitate Latin names.
# This is deliberately small and conservative: it covers the protected words
# and common administrator names without attempting broad transliteration.
_CONFUSABLES = {
    "а": "a",
    "α": "a",
    "ԁ": "d",
    "е": "e",
    "ε": "e",
    "і": "i",
    "ι": "i",
    "ј": "j",
    "к": "k",
    "κ": "k",
    "м": "m",
    "μ": "m",
    "п": "n",
    "ո": "n",
    "о": "o",
    "ο": "o",
    "օ": "o",
    "р": "p",
    "ρ": "p",
    "с": "c",
    "ѕ": "s",
    "т": "t",
    "τ": "t",
    "υ": "u",
    "ս": "u",
    "ν": "v",
    "х": "x",
    "χ": "x",
    "у": "y",
}


@dataclass(frozen=True)
class NameGuardMatch:
    """Why a joining member should be silenced."""

    kind: str
    value: str | None = None


def _normalized_characters(value: str):
    normalized = unicodedata.normalize("NFKD", (value or "").casefold())
    for character in normalized:
        category = unicodedata.category(character)
        if category in {"Cf", "Mn"}:
            # Remove invisible formatting characters and combining marks so
            # they cannot split a protected word or disguise an identity.
            continue
        yield _CONFUSABLES.get(character, character)


def normalized_name_words(value: str) -> tuple[str, ...]:
    """Return case-folded alphanumeric words with punctuation as boundaries."""

    words: list[str] = []
    current: list[str] = []
    for character in _normalized_characters(value):
        if character.isalnum():
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return tuple(words)


def _user_display_name(user) -> str:
    full_name = getattr(user, "full_name", None)
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()
    return " ".join(
        part.strip()
        for part in (
            getattr(user, "first_name", None),
            getattr(user, "last_name", None),
        )
        if isinstance(part, str) and part.strip()
    )


def _user_identity_values(user) -> tuple[str, ...]:
    values = [_user_display_name(user)]
    username = getattr(user, "username", None)
    if isinstance(username, str) and username.strip():
        values.append(username.strip().lstrip("@"))
    return tuple(value for value in values if value)


def _identity_keys(user) -> set[str]:
    keys: set[str] = set()
    for value in _user_identity_values(user):
        canonical = " ".join(normalized_name_words(value))
        if not canonical:
            continue
        keys.add(canonical)
        compact = canonical.replace(" ", "")
        if len(compact) >= 6:
            # Also catch names with inserted or removed separators while
            # avoiding aggressive matches for very short identities.
            keys.add(compact)
    return keys


def _administrator_users(administrators) -> list:
    users = []
    for administrator in administrators or ():
        user = getattr(administrator, "user", administrator)
        if user is not None:
            users.append(user)
    return users


def evaluate_name_guard(user, administrators=()) -> NameGuardMatch | None:
    """Return a moderation match for a joining user, or ``None`` if allowed."""

    if getattr(user, "is_bot", False):
        return None

    administrator_users = _administrator_users(administrators)
    administrator_ids = {
        getattr(administrator, "id", None) for administrator in administrator_users
    }
    if getattr(user, "id", None) in administrator_ids:
        return None

    for value in _user_identity_values(user):
        for word in normalized_name_words(value):
            if word in PROTECTED_NAME_WORDS:
                return NameGuardMatch("protected_word", word)

    protected_identities: set[str] = set()
    for administrator in administrator_users:
        if getattr(administrator, "is_bot", False):
            continue
        protected_identities.update(_identity_keys(administrator))

    if _identity_keys(user) & protected_identities:
        return NameGuardMatch("admin_identity")
    return None
