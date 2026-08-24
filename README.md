# CityLedger Bot

An all-in-one Telegram community management and engagement bot built for crypto-related group chats. Powered by AI analysis, gamification, real-time crypto data, and SUI blockchain airdrops.

## Features

### 🤖 AI Analysis
- **/summarize** `<#>` `[topic]` — AI-powered summary of recent chat messages, optionally filtered by topic
- **/bestof** `<#>` — Curated digest of the best messages (Most Humorous, Most Degen, Best Alpha, Most Helpful)
- **/vibecheck** `<#>` `[topic]` — Sentiment analysis with bullish/bearish classification

### 💰 Crypto Tools
- **/price** `<symbol>` — Live cryptocurrency price lookup with 24h change, market cap, and volume, including SUI ecosystem tickers like SUI, DEEP, WAL, and NS
- **/airdrop** `<count>` `<amount>` — Airdrop SUI tokens to top scorers by replying to a `/score` leaderboard (admin only)
- **/raffle** `<amount>` — Pick a weighted winner from the top 20 ranked wallets in a replied `/score` leaderboard and airdrop the prize (admin only)
- **/setairdropwallet** — Configure an encrypted, per-group airdrop wallet in DM (admin only)
- **/settoken** `<coin_type|off>` — Set or clear the group's airdrop token (admin only; airdrops fall back to `0x2::sui::SUI`)
- **/setbuybot** `on|off` — Toggle finalized DEX-buy announcements for the explicitly selected token (admin only)
- **/setbuyimage** — Set custom buy announcement media by replying to a photo, GIF, video, or image/video file; use `off` to remove it (admin only)
- **/setminbuy** `<USD amount>` — Suppress buy announcements below a per-group USD minimum; use `off` or `0` to remove it (admin only)

### 📊 Leaderboards & Stats
- **/score** — Detailed AI-integrated contribution leaderboard with quality, tone, helpfulness, and humor scoring (admin only)
- **/publicscore** — Simple public leaderboard (admin only)
- **/mystats** — Personal stats including message count, rank, and badges
- **/stats** — Group-wide statistics and top contributors

### 🏅 Badges & Gamification
- **/mybadges** — View your earned badges
- **/allbadges** — See all available badges

| Badge | Name | Requirement |
|-------|------|-------------|
| ✍️ | Contributor | 100+ messages |
| 🦸 | Hero | 500+ messages |
| ⚡️ | God-like | 1,000+ messages |
| 👑 | Weekly Champion | #1 in weekly leaderboard |
| ✨ | High Quality | Quality score 18+ |
| 🙏 | Helping Hand | Helpfulness score 18+ |
| 💎 | Diamond Hands | Active for 30+ days |

### 🗓️ Group Management
- **/calendar** — Interactive event calendar (admin only)
- **/events** — List upcoming events
- **/settimezone** `<timezone>` — Set timezone for event announcements (admin only)
- **/setwelcome** `on|off` — Toggle new member welcome messages (admin only, default: off)
- **/nameguard** `on|off` — Toggle join-time impersonation protection (admin only, default: off). When enabled, new members are silenced if their display name or username contains the complete word `dev`, `admin`, or `support`, or matches a current human admin's normalized display name or username. The bot must have Telegram's **Ban users** permission.
- **/wallet** — Submit or check wallet address (private via DM)
- **/copypasta** — Generate a copypasta from your message history

### 🤝 Community
- Optional new member welcome messages (admin-configurable)
- Event announcements at 8 AM in the group's timezone

## Setup

### Requirements
- Python 3.12+
- Node.js 22+ (for Mysten's official Sui SDK)
- A [Telegram Bot Token](https://core.telegram.org/bots#botfather)
- An [OpenAI API Key](https://platform.openai.com/api-keys)
- A PostgreSQL database (provided automatically by Railway)
- (Optional) A SUI wallet private key for airdrop functionality

### Installation

```bash
npm ci
pip install -r requirements.txt
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string (set automatically by Railway when you add a Postgres plugin) |
| `TELEGRAM_BOT_TOKEN` | Yes | Your Telegram bot token from BotFather |
| `OPENAI_API_KEY` | Yes | Your OpenAI API key |
| `SUI_PRIVATE_KEY` | No | Legacy global fallback airdrop key shared by all groups |
| `AIRDROP_ENCRYPTION_KEY` | No | 32-byte hex key used to encrypt per-group airdrop private keys at rest (required for `/setairdropwallet`) |
| `SUI_GRPC_URL` | No | Sui gRPC v2 endpoint (defaults to `https://fullnode.mainnet.sui.io:443`) |
| `SUI_GRPC_HEADERS_JSON` | No | JSON object containing provider headers such as an API key |
| `SUI_GAS_BUDGET` | No | Maximum gas budget per airdrop transfer in MIST (default: `50000000`) |
| `SUI_EXPLORER_TX_URL` | No | Explorer transaction URL prefix used in buy announcements |
| `SUI_EXPLORER_ADDRESS_URL` | No | Explorer account URL prefix used for buyer and sender links |
| `BUYBOT_WHALE_USD_THRESHOLD` | No | USD purchase value that earns the Whale Buy badge (default: `100`) |
| `SUI_DEX_PACKAGES_JSON` | No | JSON map of additional DEX package IDs to display names |
| `SUI_NODE_BINARY` | No | Node.js executable override (default: `node`) |

### Running Locally

```bash
export DATABASE_URL="postgresql://user:password@localhost:5432/cityledger"
export TELEGRAM_BOT_TOKEN="your-token"
export OPENAI_API_KEY="your-key"
python main.py
```

### Deploying on Railway

1. **Create a new project** on [Railway](https://railway.app/) and connect this repository.
2. **Add a PostgreSQL plugin** — Railway will automatically set the `DATABASE_URL` variable. The bot creates its database table on first start.
3. **Set environment variables** — In the Railway service settings, add `TELEGRAM_BOT_TOKEN` and `OPENAI_API_KEY`. Add `AIRDROP_ENCRYPTION_KEY` if you want each group to manage its own encrypted airdrop wallet, and optionally `SUI_PRIVATE_KEY` as a legacy global fallback.
4. **Deploy** — Railway can start the bot through the repository root `main.py`, and the included `Dockerfile` uses the same entrypoint for container-based deploys.

### SUI Airdrop Setup

The `/airdrop` command uses Mysten's official TypeScript SDK, Sui gRPC v2, and programmable transaction blocks. The SDK's `tx.coin()` intent draws from the sender's address balance and owned coin objects, then transfers the selected token to each recipient. **No custom smart contract or Move package needs to be deployed.**

To enable airdrops:

1. **Generate an Ed25519 keypair** — you can use the [SUI CLI](https://docs.sui.io/build/install) (`sui keytool generate ed25519`) or any Ed25519 key generator. The bot accepts the standard SUI wallet export format (`suiprivkey1...`) and also supports the legacy raw 32-byte private key as 64 hex characters.
2. **Set `AIRDROP_ENCRYPTION_KEY`** — generate a random 32-byte secret encoded as 64 hex characters (for example `python -c "import secrets; print(secrets.token_hex(32))"`) and add it to your deployment environment. This key encrypts per-group airdrop private keys before they are stored in PostgreSQL.
3. **Fund each group's wallet** — after an admin configures a group wallet with `/setairdropwallet`, fund that wallet's derived SUI address with enough tokens and gas.
4. **(Optional) Set `SUI_PRIVATE_KEY`** — if you still want one global fallback wallet for groups that have not configured their own sender, add it as an environment variable.
5. **(Optional) Set a custom token** — use `/settoken <coin_type>` in your group to airdrop a token other than native SUI.

**Airdrop workflow:**
```
1. Admin runs:  /score 30 days
2. Admin clicks: "📢 Broadcast in Group"
3. Admin configures the group's sender with: /setairdropwallet
4. Admin replies to the leaderboard message with:  /airdrop 10 1
   → Sends 1 SUI to each of the top 10 users who have registered wallets.
   → Runs a preflight balance/gas check before sending.
   → Users without wallets are gracefully skipped.
5. Admin can also reply with: /raffle 1
   → Selects one winner from the top 20 ranked users with registered wallets.
   → Applies a slight weighting toward higher leaderboard ranks.
   → Sends the configured token prize to the winner's wallet after preflight checks.
```

### Sui Buy Bot Setup

The buy bot reads finalized Sui checkpoints over gRPC. A transaction is announced when:

1. The selected token has a positive balance change for a wallet; and
2. The successful transaction contains swap evidence, or the recipient has a net outflow of another coin after SUI gas is removed.

This deliberately excludes plain transfers, airdrops, failed transactions, claims, rewards, and most liquidity operations. The announcement includes the token amount, SUI and USD purchase values, a transaction-derived post-buy market-cap estimate, a size-based flame scale, smart buyer badges, an abbreviated explorer link for the purchasing wallet, token-wide 24-hour and 1-hour DEX volume, an abbreviated sender link when different, and a transaction explorer link.

The market-cap estimate uses the finalized purchase's effective USD price per received token multiplied by Sui's on-chain total supply. This makes the triggering buy part of the displayed estimate instead of relying on a potentially stale pre-buy market snapshot. If the transaction cannot be valued in USD or Sui does not expose the coin's supply, the alert displays `N/A` without suppressing the buy.

Rolling USD volume comes from [DEX Screener](https://docs.dexscreener.com/api/reference) and is summed across the selected token's unique Sui pools. Results are cached for 60 seconds. Because the provider can lag a newly finalized transaction, GuildHero adds the triggering buy's USD value to both displayed windows. If provider volume is unavailable but the buy can be valued, the current buy becomes the minimum displayed 1-hour and 24-hour volume instead of showing `N/A`.

Buyer badges are tracked separately for each group, selected token, and wallet:

- 🐋 **Whale Buy** — purchase value meets `BUYBOT_WHALE_USD_THRESHOLD` (default: $100)
- 🆕 **First-Time Buyer** — the first buy GuildHero observes after this feature is deployed
- 💎 **Returning Holder** — GuildHero has previously observed the wallet buy that token
- 🔥 **Three-Day Streak** — the wallet buys on three consecutive UTC calendar days

Buyer history is saved only after Telegram accepts the announcement, so delivery retries do not create false returning-buyer or streak badges.

To enable it in a group:

```text
/settoken 0xPACKAGE::module::TOKEN
/setbuybot on
```

Optionally, reply to a group photo, GIF, or video with `/setbuyimage` to attach that media to future announcements. Short MP4s sent as Telegram videos are supported, as are image/video files sent as documents (recognized by MIME type or a common extension). Telegram's reusable file ID is stored rather than the media itself. Use `/setbuyimage off` to return to text-only announcements.

Use `/setminbuy 5` to announce only buys valued at $5 USD or more in that group. Decimal values such as `/setminbuy .5` are supported. Use `/setminbuy off` or `/setminbuy 0` to return to announcing buys of any size. When a positive minimum is configured and a buy cannot be valued in USD, it is not announced.

There is no implicit tracked token: if `/settoken off` is used, buy announcements are disabled even though airdrops continue to fall back to SUI. Enabling or changing the token starts at the current finalized checkpoint, so historical buys are not replayed.

The bot registers `/settoken`, `/setbuybot`, `/setbuyimage`, `/setminbuy`, and the other supported commands with Telegram at startup so they appear in group command suggestions.

Exchange labels are inferred from known package/module names. Operators can add or update package labels without a release:

```bash
export SUI_DEX_PACKAGES_JSON='{"0xPACKAGE_ID":"Exchange Name"}'
```

The public Sui Foundation endpoint is the default. A dedicated gRPC provider endpoint is recommended for production streaming; provider credentials can be passed through `SUI_GRPC_HEADERS_JSON`. The tracker keeps one finalized-checkpoint subscription while active and sleeps without scanning the database when no groups have buy announcements enabled.

## Architecture

The bot uses a modular Python layout with a narrow Node.js chain boundary:
- `main.py` — repository-root entrypoint
- `CityLedger/bot.py` — Telegram command handlers and bot wiring
- `CityLedger/ai_services.py` — OpenAI-powered scoring, summaries, vibe checks, and copypasta generation
- `CityLedger/telegram_utils.py` — shared help text, admin checks, HTML sanitization, and wallet validation
- `CityLedger/db.py` — PostgreSQL-backed key-value storage plus normalized message storage
- `CityLedger/buy_tracker.py` — pure finalized-transaction buy classification
- `CityLedger/sui_service.py` — async Python client for the persistent SDK bridge
- `CityLedger/sui_bridge.mjs` — official Sui SDK gRPC reads and PTB signing/execution

Core integrations:
- **python-telegram-bot** — Telegram Bot API framework
- **OpenAI GPT-5 nano** — AI-powered chat analysis and content generation, with exact-request result caching and bounded prompt inputs
- **PostgreSQL** — Persistent bot state plus normalized message history storage
- **CoinGecko API** — Real-time cryptocurrency price data
- **Mysten Sui SDK + gRPC v2** — Finalized checkpoint reads and programmable token transfers
- **PyNaCl** — Encryption and local validation of per-group Ed25519 airdrop keys
- **httpx** — Async HTTP client for external API calls

## License

This project is provided as-is for community use.
