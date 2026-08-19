"""Telegram Bot API channel via long polling (getUpdates) — no public
webhook/tunnel needed. Raw httpx client (see IMPL.md for why over
python-telegram-bot)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from habit_assistant.channels.base import Channel

logger = logging.getLogger(__name__)

TELEGRAM_API_ROOT = "https://api.telegram.org"


class TelegramChannel(Channel):
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        poll_timeout: int = 30,
        client: httpx.AsyncClient | None = None,
    ):
        self._token = bot_token
        self._chat_id = chat_id
        self._poll_timeout = poll_timeout
        self._base_url = f"{TELEGRAM_API_ROOT}/bot{bot_token}"
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10)
        self._offset: int | None = None

    def build_send_request(self, text: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: returns (url, json_payload) without sending."""
        return f"{self._base_url}/sendMessage", {"chat_id": self._chat_id, "text": text}

    async def send(self, text: str) -> None:
        url, payload = self.build_send_request(text)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        while True:
            try:
                params: dict[str, Any] = {"timeout": self._poll_timeout}
                if self._offset is not None:
                    params["offset"] = self._offset
                resp = await self._client.get(f"{self._base_url}/getUpdates", params=params)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                logger.warning("Telegram getUpdates failed: %s", exc)
                await asyncio.sleep(5)
                continue

            for update in payload.get("result", []):
                self._offset = update["update_id"] + 1
                message = update.get("message") or {}
                text = message.get("text")
                if not text:
                    continue
                try:
                    await on_message(text)
                except Exception:
                    logger.exception("on_message handler raised; continuing inbound loop")

    async def aclose(self) -> None:
        await self._client.aclose()
