# Contributing to CityLedger

Thanks for your interest in contributing!  This guide covers how to set up the project, run tests, and add new commands.

---

## Prerequisites

- **Python 3.12+**
- **Node.js 22+**
- **PostgreSQL** (or a managed instance, e.g. Railway)
- A **Telegram Bot Token** from [@BotFather](https://t.me/botfather)
- An **OpenAI API key**

---

## Local Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/drjnolen/GuildHero.git
   cd GuildHero
   ```

2. **Install dependencies**

   ```bash
   npm ci
   pip install -r requirements.txt
   ```

3. **Configure environment variables** — copy the template and fill in your values:

   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. **Run the bot**

   ```bash
   python main.py
   ```

---

## Running Tests

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s billing_tests -v
npm run check:sui
npm run test:sui
```

Tests live in the `tests/` directory. They mock all external services (database, OpenAI, Telegram API, and Sui) and run fully offline.

Run `billing_tests/` separately: it uses real Telegram SDK types with mocked I/O.
Set `TEST_DATABASE_URL` to a disposable PostgreSQL database for its transaction
tests, or leave it unset to skip those tests locally. GitHub Actions runs them
against PostgreSQL 16. See [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) for billing rollout
and Telegram test-environment checks.

---

## Project Structure

```
CityLedger/
├── main.py                 # Repository entrypoint
├── requirements.txt        # Python dependencies
├── package.json            # Official Sui SDK dependency and bridge scripts
├── package-lock.json       # Reproducible Node dependency lock
├── Dockerfile              # Container build (used by Railway)
├── railway.toml            # Railway deploy config
├── .env.example            # Environment variable template
├── CityLedger/             # Bot source code
│   ├── bot.py              # Telegram command handlers and bot wiring
│   ├── ai_services.py      # OpenAI-powered analysis functions
│   ├── db.py               # PostgreSQL-backed key-value + message storage
│   ├── telegram_utils.py   # Help text, admin checks, HTML sanitisation
│   ├── sui_utils.py        # SUI blockchain key handling and airdrop utilities
│   ├── sui_service.py      # Async Python/SDK bridge client
│   ├── sui_bridge.mjs      # Official SDK gRPC and PTB implementation
│   ├── buy_tracker.py      # Pure finalized-swap detection
│   ├── raffle_utils.py     # Weighted raffle winner selection
│   └── http_clients.py     # Shared async HTTP client
└── tests/                  # Unit tests
    ├── test_telegram_utils.py
    ├── test_sui_utils.py
    ├── test_sui_service.py
    ├── test_buy_tracker.py
    ├── sui_bridge.test.mjs
    ├── test_raffle_utils.py
    └── test_bot_utils.py
```

---

## Adding a New Command

1. **Write the handler** in `CityLedger/bot.py`, following the existing pattern:

   ```python
   async def mycommand_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
       """Brief description."""
       await update.message.reply_text("Hello!")
   ```

2. **Register the handler** in the `main()` function:

   ```python
   application.add_handler(CommandHandler("mycommand", mycommand_command))
   ```

3. **Add it to the command list** in `setup_bot_commands()`:

   ```python
   BotCommand("mycommand", "Brief description shown in the Telegram command menu"),
   ```

4. **Update the help text** in `CityLedger/telegram_utils.py` under the relevant section.

5. **Write tests** for any pure logic functions in `tests/`.

---

## Code Style

- Follow **PEP 8** for formatting.
- Use `ParseMode.HTML` for Telegram message formatting (avoid MarkdownV2).
- Escape all user-supplied content with `html.escape()` before embedding it in HTML messages.
- Use `asyncio.to_thread()` for blocking database or CPU-bound calls.
- Use `logging.error()` / `logging.warning()` (not `print()`) for all diagnostics.

---

## Security Notes

- **Never log private keys**, passwords, or full wallet addresses.
- Validate and normalise all wallet addresses with `normalize_wallet_address()` before storing.
- Encrypt all private keys with `encrypt_private_key()` before storing in the database.
- All admin-gated commands must call `require_admin()` from `telegram_utils.py` at the top of the handler.
