import html
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from telegram import Update

HELP_TEXT = (
    "Hello! I'm an all-in-one community management and leaderboard bot. Here's what I can do:\n\n"
    "<b>⭐ Group Access</b>\n"
    "Chat tracking, AI analysis, stats, badge progress, and leaderboard-based rewards require an active group subscription or operator whitelist.\n"
    "/subscribe - An admin can subscribe for the whole group with Stars.\n"
    "/subscription - View this group's ID, access, and renewal status.\n"
    "/terms - Read subscription and data-use terms.\n"
    "/paysupport - Contact the operator about payments or refunds.\n"
    "New chat history is collected only while access is active. Ordinary private messages are not tracked.\n\n"
    "<b>🤖 AI Analysis</b>\n"
    "/summarize &lt;number&gt; - Get an AI summary of the last number of messages.\n"
    "/bestof &lt;number&gt; - See the best messages from the last number of posts.\n"
    "/vibecheck &lt;number&gt; - Check the group's vibe on the last number of messages.\n"
    "<i>You can add a topic to any of the above, like /summarize 500 bitcoin</i>\n\n"
    "<b>📊 Stats &amp; Fun</b>\n"
    "/score - Get a detailed, AI-integrated contribution leaderboard (admin).\n"
    "/publicscore - Show a simple leaderboard in the chat (admin).\n"
    "/mystats - See your personal stats for this group.\n"
    "/stats - Show overall group statistics.\n"
    "/mybadges - View the badges you've earned.\n"
    "/allbadges - See all available badges.\n"
    "/copypasta - Create a copypasta based on your message history.\n\n"
    "<b>💰 Crypto</b>\n"
    "/price &lt;symbol&gt; - Look up a cryptocurrency price (including SUI ecosystem tokens like SUI, DEEP, WAL, NS).\n"
    "/airdrop &lt;count&gt; &lt;amount&gt; - Airdrop tokens to top scorers (admin, reply to /score).\n"
    "/raffle &lt;amount&gt; - Pick a weighted winner from the replied leaderboard and airdrop the prize (admin).\n"
    "/setairdropwallet - Set this group's encrypted airdrop wallet (admin).\n"
    "/settoken &lt;coin_type|off&gt; - Set or clear the airdrop token for this group (admin).\n"
    "/setbuybot on|off - Toggle selected-token DEX buy announcements (admin).\n"
    "/setbuyimage - Set custom buy media by replying to a photo, GIF, or video (admin).\n"
    "/setemoji &lt;emoji&gt; - Customize the buybot emoji for this group (admin).\n"
    "/setminbuy &lt;USD amount&gt; - Set the minimum announced buy value (admin).\n\n"
    "<b>🗓️ Group Management</b>\n"
    "/calendar - Manage the group's event calendar (admin).\n"
    "/events - List all upcoming events.\n"
    "/wallet - Submit or check your wallet address.\n"
    "/removewallet - Remove your registered wallet from this group.\n"
    "/setwelcome on|off - Toggle welcome messages (admin).\n"
    "/nameguard on|off - Toggle join impersonation protection (admin).\n"
    "/setachievements on|off - Toggle achievement tracking (admin).\n"
    "/settimezone - Set the timezone for event announcements (admin).\n\n"
    "<b>ℹ️ Other</b>\n"
    "/help - Show this help message.\n"
    "/cancel - Cancel any active operation (e.g., wallet submission).\n"
)

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote"}
_ALLOWED_SCHEMES = {"http", "https", "tg", "mailto"}
_SUI_ADDRESS_REGEX = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")


class TelegramHTMLSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.open_tags: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")
            return
        if tag not in _ALLOWED_TAGS:
            return
        if tag == "a":
            href = None
            for name, value in attrs:
                if name == "href" and value:
                    parsed = urlparse(value)
                    if parsed.scheme in _ALLOWED_SCHEMES:
                        href = value
                    break
            if href:
                self.parts.append(f'<a href="{html.escape(href, quote=True)}">')
                self.open_tags.append(tag)
            return
        self.parts.append(f"<{tag}>")
        self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if tag in _ALLOWED_TAGS and self.open_tags and self.open_tags[-1] == tag:
            self.parts.append(f"</{tag}>")
            self.open_tags.pop()

    def handle_data(self, data):
        self.parts.append(html.escape(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        text = "".join(self.parts)
        return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def sanitize_html_for_telegram(text: str) -> str:
    sanitizer = TelegramHTMLSanitizer()
    sanitizer.feed(text or "")
    sanitizer.close()
    return sanitizer.get_html()


async def user_is_admin(context, chat_id: int, user_id: int) -> bool:
    chat_member = await context.bot.get_chat_member(chat_id, user_id)
    return chat_member.status in ["administrator", "creator"]


async def require_admin(update: Update, context) -> bool:
    if await user_is_admin(context, update.effective_chat.id, update.effective_user.id):
        return True
    if update.message:
        await update.message.reply_text("❌ Only administrators can use this command.")
    return False


def normalize_wallet_address(wallet_address: str) -> str | None:
    """Validate a SUI wallet address and normalize it to lowercase 0x-prefixed form. Returns None if invalid."""
    candidate = (wallet_address or "").strip()
    if not _SUI_ADDRESS_REGEX.fullmatch(candidate):
        return None
    hex_portion = candidate[2:] if candidate.startswith("0x") else candidate
    return f"0x{hex_portion.lower()}"
