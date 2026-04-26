"""
orchestrator_client.py

WebSocket + HTTP client for orchestrator-connected agents.
Telegram-agent edition.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger(__name__)

AGENT_NAME = "telegram-agent"
AGENT_VERSION = "1.0.0"
AGENT_DESCRIPTION = (
    "Send and receive Telegram messages via the Bot API — "
    "supports text, photos, documents, and incoming message routing"
)

HEARTBEAT_INTERVAL = 15

_ID_FILE = Path(".agent_id")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_agent_id() -> str:
    if _ID_FILE.exists():
        return _ID_FILE.read_text().strip()
    agent_id = str(uuid.uuid4())
    _ID_FILE.write_text(agent_id)
    logger.info("Generated new stable agent ID: %s", agent_id)
    return agent_id


class OrchestratorClient:
    def __init__(self, base_url: str = "http://localhost:8000") -> None:
        self.base_url = base_url.rstrip("/")
        self.agent_id: Optional[str] = None
        self.ws_url: Optional[str] = None
        self.common_settings: dict = {}

        self._ws: Optional[Any] = None
        self._running = False
        self._stop_event = asyncio.Event()
        self._pending: dict[str, asyncio.Future] = {}
        self._task_handlers: list[Callable] = []

        self._active_tasks = 0
        self._tasks_completed = 0
        self._tasks_failed = 0
        self._start_time = time.monotonic()

    def on_task_request(self, handler: Callable) -> None:
        self._task_handlers.append(handler)

    async def register(self) -> str:
        payload = {
            "id": _stable_agent_id(),
            "name": AGENT_NAME,
            "description": AGENT_DESCRIPTION,
            "version": AGENT_VERSION,
            "capabilities": [
                {
                    "name": "send_message",
                    "description": "Send a text message to a Telegram chat or user.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": ["integer", "string"],
                                "description": "Telegram chat ID or @username",
                            },
                            "text": {"type": "string", "description": "Message text (supports Markdown/HTML)"},
                            "parse_mode": {
                                "type": "string",
                                "enum": ["Markdown", "MarkdownV2", "HTML"],
                                "description": "Optional text formatting mode",
                            },
                            "reply_to_message_id": {
                                "type": "integer",
                                "description": "Reply to a specific message by ID",
                            },
                        },
                        "required": ["chat_id", "text"],
                    },
                    "tags": [
                        "telegram", "message", "send", "text", "chat", "notify",
                        "bot", "messaging", "push", "notification",
                    ],
                    "cost": {"type": "free", "estimated_cost_usd": None},
                },
                {
                    "name": "send_photo",
                    "description": "Send a photo to a Telegram chat by URL or file_id.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": ["integer", "string"],
                                "description": "Telegram chat ID or @username",
                            },
                            "photo": {
                                "type": "string",
                                "description": "Public photo URL or Telegram file_id",
                            },
                            "caption": {"type": "string", "description": "Optional image caption"},
                            "parse_mode": {
                                "type": "string",
                                "enum": ["Markdown", "MarkdownV2", "HTML"],
                            },
                            "reply_to_message_id": {"type": "integer"},
                        },
                        "required": ["chat_id", "photo"],
                    },
                    "tags": ["telegram", "photo", "image", "picture", "send", "bot"],
                    "cost": {"type": "free", "estimated_cost_usd": None},
                },
                {
                    "name": "get_updates",
                    "description": "Fetch recent messages/updates from the Telegram bot (polling).",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "offset": {
                                "type": "integer",
                                "description": "Update ID offset — pass last_update_id + 1 to ack previous updates",
                            },
                            "limit": {"type": "integer", "default": 20},
                        },
                    },
                    "tags": [
                        "telegram", "updates", "messages", "inbox", "receive",
                        "poll", "fetch", "get", "read", "bot",
                    ],
                    "cost": {"type": "free", "estimated_cost_usd": None},
                },
                {
                    "name": "get_chat_info",
                    "description": "Get metadata about a Telegram chat, group, or user.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "chat_id": {
                                "type": ["integer", "string"],
                                "description": "Telegram chat ID or @username",
                            },
                        },
                        "required": ["chat_id"],
                    },
                    "tags": ["telegram", "chat", "info", "user", "group", "bot"],
                    "cost": {"type": "free", "estimated_cost_usd": None},
                },
            ],
            "tags": ["telegram", "messaging", "bot"],
            "required_settings": [
                {
                    "key": "telegram_bot_token",
                    "label": "Telegram Bot Token",
                    "type": "secret",
                    "required": True,
                    "description": "Bot token from @BotFather (format: 123456:ABC-...).",
                },
                {
                    "key": "telegram_webhook_url",
                    "label": "Webhook URL (optional)",
                    "type": "string",
                    "required": False,
                    "description": (
                        "Public HTTPS URL for receiving Telegram updates. "
                        "Leave empty to use long-polling instead."
                    ),
                },
                {
                    "key": "telegram_webhook_secret",
                    "label": "Webhook Secret Token (optional)",
                    "type": "secret",
                    "required": False,
                    "description": "Random secret sent in X-Telegram-Bot-Api-Secret-Token header for security.",
                },
                {
                    "key": "telegram_routing_target",
                    "label": "Incoming Message Target Agent",
                    "type": "string",
                    "required": False,
                    "description": (
                        "Name of the orchestrator agent that should handle incoming "
                        "Telegram user messages (e.g. 'task-planner-agent'). "
                        "Leave empty to disable inbound routing."
                    ),
                },
            ],
        }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/v1/agents/register",
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

        self.agent_id = data["agent_id"]
        self.ws_url = data["ws_url"]
        self.common_settings = data.get("common_settings", {})
        logger.info("Registered as %s", self.agent_id)
        return self.agent_id

    async def get_agent_setting(self, key: str) -> Any | None:
        if not self.agent_id:
            return None
        url = f"{self.base_url}/api/v1/agents/{self.agent_id}/settings/{key}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("value")

    async def send_task(
        self,
        target_agent: str,
        capability: str,
        input_data: dict,
        timeout_s: float = 60.0,
    ) -> dict:
        """
        Send a task_request to another agent via the orchestrator and await the response.
        Used for routing incoming Telegram messages to other agents.
        """
        if not self._ws:
            raise RuntimeError("Not connected to orchestrator")

        corr_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[corr_id] = fut

        envelope = self._make_envelope(
            "task_request",
            {
                "capability": capability,
                "input_data": input_data,
            },
            recipient_id=target_agent,
            correlation_id=corr_id,
        )
        await self._ws.send(json.dumps(envelope))

        try:
            result = await asyncio.wait_for(fut, timeout=timeout_s)
            return result
        except asyncio.TimeoutError:
            self._pending.pop(corr_id, None)
            raise TimeoutError(f"Task to {target_agent!r} timed out after {timeout_s}s")

    async def connect_and_run(self) -> None:
        self._running = True
        retry_delay = 1.0

        while self._running:
            try:
                logger.info("Connecting to %s", self.ws_url)
                async with websockets.connect(self.ws_url) as ws:
                    self._ws = ws
                    retry_delay = 1.0
                    logger.info("WebSocket connected")

                    sender_task = asyncio.create_task(self._heartbeat_loop())
                    receiver_task = asyncio.create_task(self._recv_loop())
                    stop_task = asyncio.create_task(self._stop_event.wait())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task, stop_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()

                    for t in done:
                        if t != stop_task and not t.cancelled():
                            exc = t.exception()
                            if exc:
                                raise exc

            except ConnectionClosed as exc:
                if exc.code == 4004:
                    logger.warning("Unknown agent_id (4004), re-registering")
                    await self.register()
                elif exc.code == 4003:
                    logger.info("Agent disabled by orchestrator (4003) — retrying")
                    retry_delay = max(retry_delay, 10.0)
                else:
                    logger.warning("WebSocket closed (code=%s): %s", exc.code, exc.reason)
            except Exception as exc:
                logger.warning("WebSocket error: %s", exc)
            finally:
                self._ws = None

            if not self._running:
                break

            logger.info("Reconnecting in %.1f s", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60.0)

    async def shutdown(self) -> None:
        self._running = False
        self._stop_event.set()

        if self.agent_id:
            async with httpx.AsyncClient() as client:
                try:
                    await client.delete(
                        f"{self.base_url}/api/v1/agents/{self.agent_id}",
                        timeout=5.0,
                    )
                except Exception as exc:
                    logger.warning("Deregister failed: %s", exc)

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    def _make_envelope(
        self,
        msg_type: str,
        payload: dict,
        recipient_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
    ) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "type": msg_type,
            "sender_id": self.agent_id,
            "recipient_id": recipient_id,
            "payload": payload,
            "timestamp": _now_iso(),
            "correlation_id": correlation_id,
        }

    async def _heartbeat_loop(self) -> None:
        while True:
            uptime = time.monotonic() - self._start_time
            msg = self._make_envelope(
                "heartbeat",
                {
                    "status": "busy" if self._active_tasks > 0 else "available",
                    "current_load": min(self._active_tasks / 10.0, 1.0),
                    "active_tasks": self._active_tasks,
                    "metrics": {
                        "tasks_completed": self._tasks_completed,
                        "tasks_failed": self._tasks_failed,
                        "uptime_seconds": round(uptime, 1),
                    },
                },
            )
            await self._ws.send(json.dumps(msg))
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _recv_loop(self) -> None:
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON frame ignored")
                continue

            mtype = msg.get("type")
            if mtype == "task_request":
                await self._handle_task_request(msg)
            elif mtype == "task_response":
                corr = msg.get("correlation_id")
                if corr and corr in self._pending:
                    fut = self._pending.pop(corr)
                    if not fut.done():
                        fut.set_result(msg.get("payload", {}))
            elif mtype == "settings_push":
                payload = msg.get("payload", {})
                if isinstance(payload, dict):
                    pushed = payload.get("settings")
                    if isinstance(pushed, dict):
                        self.common_settings.update(pushed)
                    else:
                        self.common_settings.update(payload)
            elif mtype == "error":
                logger.warning("Orchestrator error: %s", msg.get("payload"))

    async def _handle_task_request(self, msg: dict) -> None:
        req_id = msg.get("id")
        sender_id = msg.get("sender_id")
        payload = msg.get("payload", {})
        self._active_tasks += 1
        t0 = time.monotonic()
        try:
            output = None
            for handler in self._task_handlers:
                maybe = await handler(msg)
                if maybe is not None:
                    output = maybe
                    break
            if output is None:
                raise ValueError(f"Unknown capability: {payload.get('capability')!r}")

            self._tasks_completed += 1
            resp = self._make_envelope(
                "task_response",
                {
                    "success": True,
                    "output_data": output,
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
                recipient_id=sender_id,
                correlation_id=req_id,
            )
        except Exception as exc:
            self._tasks_failed += 1
            resp = self._make_envelope(
                "task_response",
                {
                    "success": False,
                    "error": str(exc),
                    "duration_ms": round((time.monotonic() - t0) * 1000, 1),
                },
                recipient_id=sender_id,
                correlation_id=req_id,
            )
        finally:
            self._active_tasks = max(0, self._active_tasks - 1)

        await self._ws.send(json.dumps(resp))
