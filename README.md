# telegram-agent

Orchestrator-connected agent that bridges Telegram and your multi-agent system.

## What it does

**Outbound** — other agents in the orchestrator can call:

| Capability | What it does |
|---|---|
| `send_message` | Send text to any Telegram chat (Markdown/HTML supported) |
| `send_photo` | Send a photo by URL or Telegram `file_id` |
| `get_updates` | Fetch recent bot messages (on-demand polling) |
| `get_chat_info` | Get metadata about a chat, group, or user |

**Inbound** — when a Telegram user messages the bot, the agent routes the message to a target agent in the orchestrator and sends the reply back to Telegram. Supports both **webhook** and **long-polling** modes.

---

## Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. `/newbot` → follow prompts → copy the **bot token**

### 2. Configure credentials

Either set environment variables or use the orchestrator dashboard agent settings:

| Setting key | Env var | Description |
|---|---|---|
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | **Required.** Token from BotFather |
| `telegram_webhook_url` | `TELEGRAM_WEBHOOK_URL` | Optional. Public HTTPS URL for webhook mode |
| `telegram_webhook_secret` | `TELEGRAM_WEBHOOK_SECRET` | Optional. Random string to validate webhook requests |
| `telegram_routing_target` | `TELEGRAM_ROUTING_TARGET` | Optional. Agent name to forward incoming messages to |

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run

```bash
python main.py
```

---

## Update modes

### Long-polling (default — no public URL needed)

Leave `TELEGRAM_WEBHOOK_URL` empty. The agent polls Telegram's servers every 30 seconds. Works behind firewalls and NAT.

### Webhook mode (recommended for production)

Set `TELEGRAM_WEBHOOK_URL` to a public HTTPS URL (e.g. from ngrok or your server). The agent will register the webhook with Telegram on startup and receive updates via HTTP POST.

```bash
# Example with ngrok
ngrok http 8080
# Then set:
TELEGRAM_WEBHOOK_URL=https://abc123.ngrok.io/webhook
WEBHOOK_PORT=8080
```

---

## Calling from another agent

```json
{
  "capability": "send_message",
  "input_data": {
    "chat_id": 123456789,
    "text": "Hello from the orchestrator! 👋",
    "parse_mode": "Markdown"
  }
}
```

```json
{
  "capability": "send_photo",
  "input_data": {
    "chat_id": 123456789,
    "photo": "https://example.com/image.jpg",
    "caption": "Here's your chart"
  }
}
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `ORCHESTRATOR_URL` | `http://localhost:8000` | Orchestrator base URL |
| `TELEGRAM_BOT_TOKEN` | — | Bot token (required) |
| `TELEGRAM_WEBHOOK_URL` | — | Webhook URL (polling if empty) |
| `TELEGRAM_WEBHOOK_SECRET` | — | Webhook validation secret |
| `TELEGRAM_ROUTING_TARGET` | — | Target agent for inbound messages |
| `WEBHOOK_HOST` | `0.0.0.0` | Webhook server bind address |
| `WEBHOOK_PORT` | `8080` | Webhook server port |
| `TASK_TIMEOUT_S` | `60` | Seconds to wait for agent reply |
| `LOG_LEVEL` | `INFO` | Logging level |
