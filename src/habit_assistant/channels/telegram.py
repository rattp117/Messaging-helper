"""Telegram Bot API channel via long polling (getUpdates) — no public
webhook/tunnel needed. Raw httpx client (see IMPL.md for why over
python-telegram-bot).

ROADMAP.md v0.4.0 "Runtime Resilience" (AC3.1, AC3.4): a transport error
from getUpdates no longer sleeps a fixed 5s and retries forever at that
same pace -- it backs off exponentially (1s -> 2s -> 4s -> ... capped at
`backoff_max_seconds`), resetting to `backoff_initial_seconds` the moment
a poll succeeds again. `self._offset` is only ever advanced inside the
per-update loop below, which only runs after a *successful* poll -- so a
run of consecutive failures, however long, never drops or duplicates an
update: the next successful `getUpdates` call still asks for the same
offset it would have asked for had the failures never happened."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

import httpx

from habit_assistant.channels.base import Button, Channel

logger = logging.getLogger(__name__)

TELEGRAM_API_ROOT = "https://api.telegram.org"


class TelegramChannel(Channel):
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        poll_timeout: int = 30,
        client: httpx.AsyncClient | None = None,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
    ):
        self._token = bot_token
        self._chat_id = chat_id
        self._poll_timeout = poll_timeout
        self._base_url = f"{TELEGRAM_API_ROOT}/bot{bot_token}"
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10)
        self._offset: int | None = None
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds

    def build_send_request(self, text: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: returns (url, json_payload) without sending."""
        return f"{self._base_url}/sendMessage", {"chat_id": self._chat_id, "text": text}

    async def send(self, text: str) -> None:
        url, payload = self.build_send_request(text)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    def build_send_image_request(
        self, image: bytes, caption: str
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Exposed for testing: returns (url, data, files) without sending.
        ROADMAP.md v1.0.0 AC1.0.2: `sendPhoto` is a multipart upload (not
        JSON like `sendMessage`), so `caption`/`chat_id` are form fields
        and the image bytes are a file part."""
        return (
            f"{self._base_url}/sendPhoto",
            {"chat_id": self._chat_id, "caption": caption},
            {"photo": ("chart.png", image, "image/png")},
        )

    async def send_image(self, image: bytes, caption: str) -> None:
        url, data, files = self.build_send_image_request(image, caption)
        resp = await self._client.post(url, data=data, files=files)
        resp.raise_for_status()

    def build_send_actionable_request(self, text: str, buttons: list[Button]) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `sendMessage` + a one-row inline keyboard
        (SPEC-v1.1.md §5) -- each `(label, callback_data)` pair becomes one
        button in the row."""
        url, payload = self.build_send_request(text)
        payload = dict(payload)
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
        }
        return url, payload

    async def send_actionable(self, text: str, buttons: list[Button]) -> None:
        url, payload = self.build_send_actionable_request(text, buttons)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    def build_set_my_commands_requests(
        self, commands: dict[str, list[tuple[str, str]]]
    ) -> list[tuple[str, dict[str, Any]]]:
        """Exposed for testing: one `setMyCommands` request per language
        code in `commands` (SPEC-v1.1.md §5) -- `"en"` is the Bot API's
        default set (no `language_code` field); any other code (e.g.
        `"th"`) is sent with `language_code` so it only applies to clients
        in that language."""
        requests: list[tuple[str, dict[str, Any]]] = []
        url = f"{self._base_url}/setMyCommands"
        for lang_code, cmds in commands.items():
            payload: dict[str, Any] = {
                "commands": [{"command": command, "description": description} for command, description in cmds]
            }
            if lang_code != "en":
                payload["language_code"] = lang_code
            requests.append((url, payload))
        return requests

    async def set_my_commands(self, commands: dict[str, list[tuple[str, str]]]) -> None:
        for url, payload in self.build_set_my_commands_requests(commands):
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()

    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        url = f"{self._base_url}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text is not None:
            payload["text"] = text
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    async def run(
        self,
        on_message: Callable[[str], Awaitable[None]],
        on_callback: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        backoff = self._backoff_initial
        while True:
            try:
                params: dict[str, Any] = {"timeout": self._poll_timeout}
                if self._offset is not None:
                    params["offset"] = self._offset
                resp = await self._client.get(f"{self._base_url}/getUpdates", params=params)
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                logger.warning(
                    "Telegram getUpdates failed (retrying in %.1fs): %s", backoff, exc
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._backoff_max)
                continue

            backoff = self._backoff_initial  # reset after a successful poll

            for update in payload.get("result", []):
                self._offset = update["update_id"] + 1

                callback_query = update.get("callback_query")
                if callback_query is not None:
                    # SPEC-v1.1.md R-U4: a callback (inline-button tap) is
                    # routed to on_callback, then the client's spinner is
                    # ALWAYS dismissed via answerCallbackQuery -- even when
                    # on_callback is absent, raises, or the data is
                    # malformed (that validation lives in on_callback
                    # itself; this loop never inspects `data`).
                    cb_id = callback_query.get("id", "")
                    data = callback_query.get("data") or ""
                    source_text = (callback_query.get("message") or {}).get("text") or ""
                    if on_callback is not None:
                        try:
                            await on_callback(data, source_text, cb_id)
                        except Exception:
                            logger.exception("on_callback handler raised; continuing inbound loop")
                    try:
                        await self.answer_callback_query(cb_id)
                    except Exception:
                        logger.exception("answerCallbackQuery failed; continuing inbound loop")
                    continue

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
