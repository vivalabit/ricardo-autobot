# Ricardo Assistant

Your assistant for checking Ricardo.ch listings as resale opportunities in Switzerland.
The assistant communicates with users through Telegram: users send Ricardo listing links to the bot in a Telegram chat and receive the resale analysis back in the same chat.

## Bot Commands

- `/check <ricardo_lot_link> [min_profit=30] [max_price=180]` - check a specific listing.
- `/find <item> [price_or_range]` - parse live Ricardo.ch search results and return active listings. With a price or range, Ricardo price filters are applied.
- `/question <question>` - ask a follow-up about the recent checked lot or search results, or ask a standalone question.
- `/language` or `/lang` - choose the answer language with buttons.
- `/language <code>` or `/lang <code>` - set the answer language. Supported codes: `en`, `ru`, `de`, `fr`, `it`, `es`.
- `/settings` - show current chat settings.
- `/help` or `/start` - show help.

## Setup

```bash
cp .env.example .env
```

### Get a Telegram bot token

The bot needs a Telegram bot token in `TELEGRAM_BOT_TOKEN` to receive messages and send replies.

1. Open Telegram and start a chat with [@BotFather](https://t.me/BotFather).
2. Send the `/newbot` command.
3. Follow BotFather's prompts: choose a display name, then choose a username that ends with `bot`, for example `ricardo_resale_bot`.
4. Copy the token that BotFather returns.
5. Paste it into your `.env` file:

```env
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenFromBotFather
```

Keep this token private. Anyone with the token can control your bot.

Minimum required values:

```env
TELEGRAM_BOT_TOKEN=
OPENCLAW_AGENT_ID=ricardo-resale
OPENCLAW_AGENT_COMMAND=openclaw
OPENCLAW_RESPONSE_LANGUAGE=en
```

If `openclaw` is not in `PATH`, use the full path:

```env
OPENCLAW_AGENT_COMMAND=/Users/you/.openclaw/bin/openclaw
```

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Test the parser manually:

```bash
python3 scrape_page.py --url "https://www.ricardo.ch/..." --headless
```

Run the bot:

```bash
python3 bot.py
```

## Create an OpenClaw AI Agent

First, check that OpenClaw is installed and configured:

```bash
openclaw status
```

If OpenClaw is not configured yet:

```bash
openclaw onboard
```

Create a dedicated workspace and agent:

```bash
mkdir -p ~/.openclaw/workspace-ricardo-resale

openclaw agents add ricardo-resale \
  --workspace ~/.openclaw/workspace-ricardo-resale \
  --model openai/gpt-5.5 \ 
  --non-interactive \
  --json
```

Add the agent instructions to `~/.openclaw/workspace-ricardo-resale/AGENTS.md` and IDENTITY.md from files folder in repo:

After this, the bot will call this agent through `OPENCLAW_AGENT_ID=ricardo-resale`.

If `OPENCLAW_AGENT_DELIVER_REPLY=0`, the bot sends the reply to Telegram itself. If you set `OPENCLAW_AGENT_DELIVER_REPLY=1`, OpenClaw will try to deliver the reply directly through its Telegram channel.
