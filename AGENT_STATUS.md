# Agent Status: NOT READY — NEEDS ATTENTION

**Status:** ❌ NOT READY TO ACCEPT TRAFFIC
**Reason:** Mandatory Telegram bot token is not configured

## Required Parameters (Missing)

| Parameter | Key | Description |
|-----------|-----|-------------|
| Telegram Bot Token | `telegram_bot_token` | Token from @BotFather (format: `123456:ABC-…`) |

## How to Fix

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the **bot token** provided by BotFather
4. Set the value via the orchestrator dashboard or in `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-your-token-here
```

This agent will remain offline and refuse traffic until the bot token is supplied.

> **Note:** Webhook settings (`telegram_webhook_url`, `telegram_webhook_secret`) are optional.
> Without them the agent uses long-polling which works without a public URL.
