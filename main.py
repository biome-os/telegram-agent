"""
main.py — Entry point for telegram-agent.

Capabilities exposed to the orchestrator:
  - send_message    — send text to any Telegram chat
  - send_photo      — send a photo (URL or file_id)
  - get_updates     — poll for recent messages (manual / on-demand)
  - get_chat_info   — fetch chat/user metadata

Inbound routing:
  When a Telegram user messages the bot, the agent can forward the message
  to another orchestrator agent (configured via telegram_routing_target)
  and reply with its response.  Supports both webhook and long-polling modes.

Update modes:
  TELEGRAM_WEBHOOK_URL set  → webhook mode (needs a public HTTPS server)
  TELEGRAM_WEBHOOK_URL unset → long-polling mode (no public URL needed)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import sys
from typing import Any, Optional

from dotenv import load_dotenv

try:
    from aiohttp import web as aiohttp_web
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

from telegram_client import TelegramClient, TelegramConfig, TelegramError
from orchestrator_client import OrchestratorClient

load_dotenv()

logging.basicConfig(
    level=logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO").upper()),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telegram-agent")


# ── Config helpers ─────────────────────────────────────────────────────────────

def _optional(key: str, default: str = "") -> str:
    return os.getenv(key, default)


# ── Setting resolution ─────────────────────────────────────────────────────────

async def _resolve(orchestrator: OrchestratorClient, key: str, env_key: str = "") -> str:
    """Env var → agent setting → common settings."""
    if env_key:
        env_val = os.getenv(env_key, "").strip()
        if env_val:
            return env_val
    try:
        agent_val = await orchestrator.get_agent_setting(key)
        if str(agent_val or "").strip():
            return str(agent_val).strip()
    except Exception:
        pass
    common = orchestrator.common_settings
    if isinstance(common.get("settings"), dict):
        common = {**common, **common["settings"]}
    return str(common.get(key, "")).strip()


# ── Incoming message handler ───────────────────────────────────────────────────

class InboundRouter:
    """
    Receives raw Telegram Update dicts, extracts the user message, and
    forwards it to the configured routing target agent in the orchestrator.
    Sends the agent's reply back to the Telegram chat.
    """

    def __init__(
        self,
        tg: TelegramClient,
        orchestrator: OrchestratorClient,
        routing_target: str,
        task_timeout_s: float = 60.0,
    ) -> None:
        self._tg = tg
        self._orch = orchestrator
        self._target = routing_target
        self._timeout = task_timeout_s

    async def handle_update(self, update: dict) -> None:
        message = update.get("message") or update.get("edited_message")
        if not message:
            return  # ignore non-message updates (callback_query etc.)

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text", "").strip()
        user = message.get("from", {})
        username = user.get("username") or user.get("first_name") or str(user.get("id", "unknown"))

        if not text or not chat_id:
            return

        logger.info("Inbound from @%s (chat=%s): %s", username, chat_id, text[:80])

        try:
            result = await self._orch.send_task(
                target_agent=self._target,
                capability="execute_task",
                input_data={
                    "message": text,
                    "from_user": username,
                    "chat_id": chat_id,
                    "platform": "telegram",
                },
                timeout_s=self._timeout,
            )
            reply = self._extract_reply(result)
        except TimeoutError:
            reply = "Sorry, the request timed out. Please try again."
        except Exception as exc:
            logger.warning("Routing error: %s", exc)
            reply = "Sorry, something went wrong processing your request."

        if reply:
            try:
                await self._tg.send_message(
                    chat_id=chat_id,
                    text=reply,
                    reply_to_message_id=message.get("message_id"),
                )
            except TelegramError as exc:
                logger.warning("Failed to send reply to %s: %s", chat_id, exc)

    @staticmethod
    def _extract_reply(result: dict) -> str:
        """Pull a text reply out of a task_response payload."""
        if not result.get("success"):
            return result.get("error", "An error occurred.")
        output = result.get("output_data", {})
        if isinstance(output, str):
            return output
        if isinstance(output, dict):
            for key in ("reply", "text", "message", "result", "output", "content"):
                val = output.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            # Fallback: JSON dump
            return json.dumps(output, ensure_ascii=False)
        return str(output)


# ── Webhook server (aiohttp) ───────────────────────────────────────────────────

class WebhookServer:
    """Tiny aiohttp server that receives Telegram webhook POSTs."""

    def __init__(
        self,
        router: InboundRouter,
        secret_token: Optional[str] = None,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        self._router = router
        self._secret_token = secret_token
        self._host = host
        self._port = port
        self._runner: Optional[Any] = None

    async def start(self) -> None:
        app = aiohttp_web.Application()
        app.router.add_post("/webhook", self._handle)
        app.router.add_get("/health", self._health)
        self._runner = aiohttp_web.AppRunner(app)
        await self._runner.setup()
        site = aiohttp_web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("Webhook server listening on %s:%d/webhook", self._host, self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def _health(self, request: Any) -> Any:
        return aiohttp_web.Response(text="ok")

    async def _handle(self, request: Any) -> Any:
        # Validate secret token if configured
        if self._secret_token:
            received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(received, self._secret_token):
                logger.warning("Webhook: invalid secret token from %s", request.remote)
                return aiohttp_web.Response(status=403, text="Forbidden")

        try:
            update = await request.json()
        except Exception:
            return aiohttp_web.Response(status=400, text="Bad JSON")

        asyncio.create_task(self._router.handle_update(update))
        return aiohttp_web.Response(text="ok")


# ── Long-polling loop ──────────────────────────────────────────────────────────

class LongPoller:
    """Polls getUpdates in a loop and feeds updates to InboundRouter."""

    _POLL_TIMEOUT = 30   # seconds of long-poll connection
    _RETRY_DELAY = 5     # wait on consecutive errors

    def __init__(self, tg: TelegramClient, router: InboundRouter) -> None:
        self._tg = tg
        self._router = router
        self._offset: Optional[int] = None
        self._running = False

    async def run(self) -> None:
        self._running = True
        error_streak = 0
        logger.info("Long-polling started")

        while self._running:
            try:
                updates = await self._tg.get_updates(
                    offset=self._offset,
                    limit=100,
                    timeout=self._POLL_TIMEOUT,
                )
                error_streak = 0
                for update in updates:
                    uid = update.get("update_id")
                    if uid is not None:
                        self._offset = uid + 1
                    asyncio.create_task(self._router.handle_update(update))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                error_streak += 1
                delay = min(self._RETRY_DELAY * error_streak, 60)
                logger.warning("Polling error (%d in a row): %s — retrying in %ds", error_streak, exc, delay)
                await asyncio.sleep(delay)

        logger.info("Long-polling stopped")

    def stop(self) -> None:
        self._running = False


# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    orchestrator_url = _optional("ORCHESTRATOR_URL", "http://localhost:8000")
    orchestrator = OrchestratorClient(base_url=orchestrator_url)

    # ── Register with orchestrator ─────────────────────────────────────────────
    logger.info("Registering with orchestrator at %s …", orchestrator_url)
    for attempt in range(1, 6):
        try:
            await orchestrator.register()
            break
        except Exception as exc:
            if attempt == 5:
                logger.error("Could not register after 5 attempts: %s", exc)
                sys.exit(1)
            wait = 2 ** attempt
            logger.warning("Registration failed (%s) — retrying in %ds", exc, wait)
            await asyncio.sleep(wait)

    # ── Resolve bot credentials ────────────────────────────────────────────────
    bot_token = await _resolve(orchestrator, "telegram_bot_token", "TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error(
            "telegram_bot_token is required. Set it via the dashboard agent settings "
            "or TELEGRAM_BOT_TOKEN env var."
        )
        sys.exit(1)

    webhook_url     = await _resolve(orchestrator, "telegram_webhook_url",    "TELEGRAM_WEBHOOK_URL")
    webhook_secret  = await _resolve(orchestrator, "telegram_webhook_secret",  "TELEGRAM_WEBHOOK_SECRET")
    routing_target  = await _resolve(orchestrator, "telegram_routing_target",  "TELEGRAM_ROUTING_TARGET")
    webhook_host    = _optional("WEBHOOK_HOST", "0.0.0.0")
    webhook_port    = int(_optional("WEBHOOK_PORT", "8080"))
    task_timeout_s  = float(_optional("TASK_TIMEOUT_S", "60"))

    # ── Build Telegram client ─────────────────────────────────────────────────
    tg_cfg = TelegramConfig(bot_token=bot_token)
    tg = TelegramClient(tg_cfg)
    try:
        me = await tg.get_me()
        logger.info("Telegram bot: @%s", me.get("username", "?"))
    except Exception as exc:
        logger.error("Failed to verify bot token: %s", exc)
        sys.exit(1)

    # ── Outbound capability handlers ───────────────────────────────────────────

    async def handle_task(msg: dict) -> dict | None:
        payload = msg.get("payload", {})
        capability = payload.get("capability")
        if capability not in ("send_message", "send_photo", "get_updates", "get_chat_info"):
            return None

        inp = payload.get("input_data", {}) or {}

        if capability == "send_message":
            chat_id = inp.get("chat_id")
            text = str(inp.get("text", ""))
            if not chat_id or not text:
                raise ValueError("send_message requires chat_id and text")
            return await tg.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=inp.get("parse_mode"),
                reply_to_message_id=inp.get("reply_to_message_id"),
            )

        if capability == "send_photo":
            chat_id = inp.get("chat_id")
            photo = inp.get("photo")
            if not chat_id or not photo:
                raise ValueError("send_photo requires chat_id and photo")
            return await tg.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=inp.get("caption"),
                parse_mode=inp.get("parse_mode"),
                reply_to_message_id=inp.get("reply_to_message_id"),
            )

        if capability == "get_updates":
            offset = inp.get("offset")
            limit = int(inp.get("limit", 20))
            updates = await tg.get_updates(offset=offset, limit=limit)
            return {"updates": updates, "count": len(updates)}

        if capability == "get_chat_info":
            chat_id = inp.get("chat_id")
            if not chat_id:
                raise ValueError("get_chat_info requires chat_id")
            info = await tg.get_chat(chat_id)
            return info

        return None

    orchestrator.on_task_request(handle_task)

    # ── Set up inbound routing ─────────────────────────────────────────────────
    inbound_router: Optional[InboundRouter] = None
    webhook_server: Optional[WebhookServer] = None
    long_poller: Optional[LongPoller] = None

    if routing_target:
        inbound_router = InboundRouter(
            tg=tg,
            orchestrator=orchestrator,
            routing_target=routing_target,
            task_timeout_s=task_timeout_s,
        )

        if webhook_url:
            # Webhook mode
            if not _AIOHTTP_AVAILABLE:
                logger.error(
                    "aiohttp is required for webhook mode. "
                    "Install it with: pip install aiohttp"
                )
                sys.exit(1)
            await tg.set_webhook(
                url=webhook_url,
                secret_token=webhook_secret or None,
            )
            webhook_server = WebhookServer(
                router=inbound_router,
                secret_token=webhook_secret or None,
                host=webhook_host,
                port=webhook_port,
            )
            logger.info("Inbound routing → %r via webhook at %s", routing_target, webhook_url)
        else:
            # Long-polling mode
            await tg.delete_webhook(drop_pending_updates=False)
            long_poller = LongPoller(tg=tg, router=inbound_router)
            logger.info("Inbound routing → %r via long-polling", routing_target)
    else:
        logger.info(
            "No telegram_routing_target configured — inbound messages will be ignored. "
            "Set the setting in the dashboard to enable routing."
        )
        # Make sure we're in polling mode (no leftover webhook)
        try:
            await tg.delete_webhook()
        except Exception:
            pass

    # ── Signal handling ────────────────────────────────────────────────────────
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _on_signal(*_) -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _on_signal)

    # ── Start all tasks ────────────────────────────────────────────────────────
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(orchestrator.connect_and_run()))
    tasks.append(asyncio.create_task(shutdown_event.wait()))

    if webhook_server:
        await webhook_server.start()

    if long_poller:
        tasks.append(asyncio.create_task(long_poller.run()))

    logger.info(
        "telegram-agent is running (@%s). Press Ctrl+C to stop.",
        tg_cfg.bot_username or "?",
    )

    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # ── Cleanup ────────────────────────────────────────────────────────────────
    logger.info("Shutting down …")
    if long_poller:
        long_poller.stop()
    if webhook_server:
        await webhook_server.stop()
    for t in tasks:
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    await orchestrator.shutdown()
    await tg.aclose()
    logger.info("telegram-agent stopped cleanly.")


if __name__ == "__main__":
    asyncio.run(main())
