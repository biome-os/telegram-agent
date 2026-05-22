"""
telegram_client.py

Thin async wrapper around the Telegram Bot API (httpx-based).

Supports:
  - send_message       — send plain/markdown/HTML text to a chat
  - send_photo         — send a photo (URL or raw bytes) with optional caption
  - get_updates        — poll for new updates (long-polling)
  - get_chat           — fetch chat/user metadata
  - set_webhook        — register a webhook URL
  - delete_webhook     — remove the webhook (switches to polling mode)
  - get_webhook_info   — inspect current webhook state
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}/{method}"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


@dataclass
class TelegramConfig:
    bot_token: str
    # Parsed on first successful getMe call
    bot_username: str = ""
    bot_id: int = 0


class TelegramError(Exception):
    """Raised when the Telegram API returns ok=false."""

    def __init__(self, description: str, error_code: int = 0) -> None:
        super().__init__(description)
        self.error_code = error_code
        self.description = description


class TelegramClient:
    """Async Telegram Bot API client."""

    def __init__(self, config: TelegramConfig) -> None:
        self._cfg = config
        self._http = httpx.AsyncClient(timeout=_TIMEOUT)

    # ── Low-level ──────────────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return _API_BASE.format(token=self._cfg.bot_token, method=method)

    async def _call(self, method: str, **params: Any) -> Any:
        """POST to Bot API, raise TelegramError on failure."""
        # Drop None values so we don't send nulls the API doesn't expect
        body = {k: v for k, v in params.items() if v is not None}
        resp = await self._http.post(self._url(method), json=body)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Unknown Telegram error"),
                data.get("error_code", 0),
            )
        return data.get("result")

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── Bot identity ───────────────────────────────────────────────────────────

    async def get_me(self) -> dict:
        """Return bot info and populate config fields."""
        result = await self._call("getMe")
        self._cfg.bot_id = result.get("id", 0)
        self._cfg.bot_username = result.get("username", "")
        logger.info("Bot identity: @%s (id=%d)", self._cfg.bot_username, self._cfg.bot_id)
        return result

    # ── Sending ────────────────────────────────────────────────────────────────

    async def send_message(
        self,
        chat_id: int | str,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
        disable_web_page_preview: bool = False,
    ) -> dict:
        """
        Send a text message.

        parse_mode: None | "Markdown" | "MarkdownV2" | "HTML"
        """
        if not text:
            raise ValueError("send_message requires non-empty text")

        result = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
            disable_web_page_preview=disable_web_page_preview or None,
        )
        return {
            "ok": True,
            "message_id": result.get("message_id"),
            "chat_id": chat_id,
        }

    async def send_photo(
        self,
        chat_id: int | str,
        photo: str,  # URL or file_id
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        """
        Send a photo by URL or Telegram file_id.
        For local file uploads call send_photo_bytes() instead.
        """
        result = await self._call(
            "sendPhoto",
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            parse_mode=parse_mode,
            reply_to_message_id=reply_to_message_id,
        )
        return {
            "ok": True,
            "message_id": result.get("message_id"),
            "chat_id": chat_id,
        }

    async def send_photo_bytes(
        self,
        chat_id: int | str,
        photo_bytes: bytes,
        filename: str = "photo.jpg",
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        """Upload raw photo bytes to a chat."""
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        if reply_to_message_id:
            data["reply_to_message_id"] = str(reply_to_message_id)

        files = {"photo": (filename, photo_bytes, "image/jpeg")}
        resp = await self._http.post(
            self._url("sendPhoto"),
            data=data,
            files=files,
        )
        resp.raise_for_status()
        result_json = resp.json()
        if not result_json.get("ok"):
            raise TelegramError(
                result_json.get("description", "Unknown error"),
                result_json.get("error_code", 0),
            )
        result = result_json.get("result", {})
        return {
            "ok": True,
            "message_id": result.get("message_id"),
            "chat_id": chat_id,
        }

    async def send_document(
        self,
        chat_id: int | str,
        document: str,  # URL or file_id
        caption: Optional[str] = None,
        reply_to_message_id: Optional[int] = None,
    ) -> dict:
        result = await self._call(
            "sendDocument",
            chat_id=chat_id,
            document=document,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )
        return {
            "ok": True,
            "message_id": result.get("message_id"),
            "chat_id": chat_id,
        }

    # ── Receiving (polling) ────────────────────────────────────────────────────

    async def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 100,
        timeout: int = 0,
        allowed_updates: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Fetch pending updates.  Set timeout > 0 for long-polling.
        Returns raw Update objects from the Bot API.
        """
        result = await self._call(
            "getUpdates",
            offset=offset,
            limit=limit,
            timeout=timeout if timeout > 0 else None,
            allowed_updates=allowed_updates,
        )
        return result or []

    # ── Chat info ──────────────────────────────────────────────────────────────

    async def get_chat(self, chat_id: int | str) -> dict:
        return await self._call("getChat", chat_id=chat_id)

    async def get_chat_member_count(self, chat_id: int | str) -> int:
        return await self._call("getChatMemberCount", chat_id=chat_id)

    # ── Webhook ────────────────────────────────────────────────────────────────

    async def set_webhook(
        self,
        url: str,
        max_connections: int = 40,
        allowed_updates: Optional[list[str]] = None,
        secret_token: Optional[str] = None,
    ) -> bool:
        await self._call(
            "setWebhook",
            url=url,
            max_connections=max_connections,
            allowed_updates=allowed_updates or ["message", "callback_query"],
            secret_token=secret_token,
        )
        logger.info("Webhook set to: %s", url)
        return True

    async def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        await self._call(
            "deleteWebhook",
            drop_pending_updates=drop_pending_updates or None,
        )
        logger.info("Webhook deleted (polling mode active)")
        return True

    async def get_webhook_info(self) -> dict:
        return await self._call("getWebhookInfo")

    # ── Answer callback queries ────────────────────────────────────────────────

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        await self._call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert or None,
        )
        return True
