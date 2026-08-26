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
offset it would have asked for had the failures never happened.

SPEC-v1.2.md "Multi-user support" (R-C1): every send now takes an explicit
`chat_id` instead of a pinned `self._chat_id` -- the constructor's second
positional arg is renamed `owner_chat_id` and kept only as a public
attribute for callers that need "the owner's chat" (health alerts,
`--test-reminder`), never used internally to address a send. `run()`
extracts `chat_id` from each update's `message.chat.id` (or
`callback_query.message.chat.id` for a button tap) and passes it first to
the caller's handler."""

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
        owner_chat_id: str,
        poll_timeout: int = 30,
        client: httpx.AsyncClient | None = None,
        backoff_initial_seconds: float = 1.0,
        backoff_max_seconds: float = 60.0,
    ):
        self._token = bot_token
        # SPEC-v1.2.md R-C1: kept only for defaulting/health (main.py wires
        # this into HealthMonitor and --test-reminder) -- never read inside
        # send()/send_image()/send_actionable() below, which all take an
        # explicit chat_id now.
        self.owner_chat_id = owner_chat_id
        self._poll_timeout = poll_timeout
        self._base_url = f"{TELEGRAM_API_ROOT}/bot{bot_token}"
        self._client = client or httpx.AsyncClient(timeout=poll_timeout + 10)
        self._offset: int | None = None
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds

    def build_send_request(
        self, chat_id: str, text: str, *, disable_notification: bool = False
    ) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: returns (url, json_payload) without sending.
        SPEC-v1.8.md R-S1: `disable_notification` is only ADDED to the
        payload when `True` -- the default `False` produces the exact
        pre-v1.8 payload shape (AC-1), so `send_actionable`/`send_and_pin`
        below (which call this with no `disable_notification` argument)
        are unaffected."""
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if disable_notification:
            payload["disable_notification"] = True
        return f"{self._base_url}/sendMessage", payload

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        url, payload = self.build_send_request(chat_id, text, disable_notification=disable_notification)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    def build_send_image_request(
        self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False
    ) -> tuple[str, dict[str, Any], dict[str, Any]]:
        """Exposed for testing: returns (url, data, files) without sending.
        ROADMAP.md v1.0.0 AC1.0.2: `sendPhoto` is a multipart upload (not
        JSON like `sendMessage`), so `caption`/`chat_id` are form fields
        and the image bytes are a file part.

        SPEC-v1.9.md R26/AC28 (v1.9 integration pass): `disable_notification`
        is an additive, keyword-only, DEFAULTED param -- mirrors `send`'s
        own SPEC-v1.8.md R-S1 shape exactly -- `False` (the default)
        produces a payload byte-identical to pre-v1.9 (every existing
        `send_image` caller/fake is unaffected); the optional month-end
        `/wrapped` auto-send is the one caller that ever passes `True`
        (R26's "one SILENT card per active user")."""
        data: dict[str, Any] = {"chat_id": chat_id, "caption": caption}
        if disable_notification:
            data["disable_notification"] = True
        return (
            f"{self._base_url}/sendPhoto",
            data,
            {"photo": ("chart.png", image, "image/png")},
        )

    async def send_image(self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False) -> None:
        url, data, files = self.build_send_image_request(chat_id, image, caption, disable_notification=disable_notification)
        resp = await self._client.post(url, data=data, files=files)
        resp.raise_for_status()

    # SPEC-v1.8.md integration step (Archi-directed follow-up, flagged by
    # both IMPL-v1.8-quicklog.md and TEST-v1.8-quicklog.md's "Known
    # non-module limitation"): `/log`'s quick-log keyboard can carry many
    # buttons (every loggable habit's amount buttons, flattened) -- a
    # single-row layout renders as one long, awkward row. Chunked into rows
    # of at most `_ACTIONABLE_ROW_SIZE` buttons each, in registry/list
    # order. Backward-compatible by construction: <=3 buttons is exactly
    # one row, identical to the pre-v1.8 shape (same button objects in the
    # same order) -- so `undo_ui.undo_button`'s own one-button payload (and
    # every other pre-v1.8 caller, none of which ever passed more than a
    # couple of buttons) is byte-identical.
    _ACTIONABLE_ROW_SIZE = 3

    def build_send_actionable_request(
        self, chat_id: str, text: str, buttons: list[Button]
    ) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `sendMessage` + an inline keyboard
        (SPEC-v1.1.md §5, chunked per SPEC-v1.8.md's own integration note
        above) -- each `(label, callback_data)` pair becomes one button,
        at most `_ACTIONABLE_ROW_SIZE` per row."""
        url, payload = self.build_send_request(chat_id, text)
        payload = dict(payload)
        if buttons:
            rows = [
                buttons[i : i + self._ACTIONABLE_ROW_SIZE]
                for i in range(0, len(buttons), self._ACTIONABLE_ROW_SIZE)
            ]
        else:
            rows = [[]]  # SPEC-v1.1.md §5's own "no buttons -> one empty row" shape, unchanged.
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in row] for row in rows]
        }
        return url, payload

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        url, payload = self.build_send_actionable_request(chat_id, text, buttons)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    def build_set_my_commands_requests(
        self, commands: dict[str, list[tuple[str, str]]], *, scope_chat_id: str | None = None
    ) -> list[tuple[str, dict[str, Any]]]:
        """Exposed for testing: one `setMyCommands` request per language
        code in `commands` (SPEC-v1.1.md §5) -- `"en"` is the Bot API's
        default set (no `language_code` field); any other code (e.g.
        `"th"`) is sent with `language_code` so it only applies to clients
        in that language. Global, not per-chat (R-C1), UNLESS
        `scope_chat_id` is given (SPEC-v1.8.md R-S3): then every request
        also carries `"scope": {"type": "chat", "chat_id": scope_chat_id}`,
        registering a chat-scoped menu instead of the default one -- the
        default `None` leaves every payload byte-identical to v1.7 (AC-3)."""
        requests: list[tuple[str, dict[str, Any]]] = []
        url = f"{self._base_url}/setMyCommands"
        for lang_code, cmds in commands.items():
            payload: dict[str, Any] = {
                "commands": [{"command": command, "description": description} for command, description in cmds]
            }
            if lang_code != "en":
                payload["language_code"] = lang_code
            if scope_chat_id is not None:
                payload["scope"] = {"type": "chat", "chat_id": scope_chat_id}
            requests.append((url, payload))
        return requests

    async def set_my_commands(
        self, commands: dict[str, list[tuple[str, str]]], *, scope_chat_id: str | None = None
    ) -> None:
        for url, payload in self.build_set_my_commands_requests(commands, scope_chat_id=scope_chat_id):
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()

    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        url = f"{self._base_url}/answerCallbackQuery"
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text is not None:
            payload["text"] = text
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    def build_set_message_reaction_request(
        self, chat_id: str, message_id: str, emoji: str
    ) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: Bot API 7.0 `setMessageReaction` -- one
        emoji reaction, replacing any previous reaction this bot set on
        the message (SPEC-v1.8.md R-S2)."""
        return f"{self._base_url}/setMessageReaction", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}],
        }

    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """SPEC-v1.8.md R-S2/AC-2: NEVER raises -- a reaction is purely
        decorative (module `quicklog`'s own R-Q4 fail-open call), so a
        transport error here is logged and swallowed, exactly like
        `unpin`'s own best-effort posture above."""
        url, payload = self.build_set_message_reaction_request(chat_id, message_id, emoji)
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("setMessageReaction failed for %s/%s; continuing (fail-open)", chat_id, message_id)

    def build_pin_request(self, chat_id: str, message_id: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `pinChatMessage` -- no `disable_notification`
        field, so Telegram's default (notify) behavior applies. SPEC-v1.6.md
        R-D6/§9 OQ1: the one-time pin is deliberately user-initiated and
        MEANT to notify (only the many silent `edit_message` calls after it
        are not)."""
        return f"{self._base_url}/pinChatMessage", {"chat_id": chat_id, "message_id": message_id}

    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        """SPEC-v1.6.md §5: `sendMessage` then `pinChatMessage`, returning
        the sent message's id either way -- a pin failure (permissions,
        rate limit, chat type) is logged and swallowed, not raised: the
        message was still successfully SENT, so the caller (`core/
        dashboard.py:execute_dashboard`) has a real id to store even if
        the pin itself didn't take; the next `refresh` will just keep
        editing an unpinned message rather than losing the dashboard
        entirely."""
        url, payload = self.build_send_request(chat_id, text)
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        message_id = str(resp.json()["result"]["message_id"])
        pin_url, pin_payload = self.build_pin_request(chat_id, message_id)
        try:
            pin_resp = await self._client.post(pin_url, json=pin_payload)
            pin_resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("pinChatMessage failed for %s/%s; message sent but not pinned", chat_id, message_id)
        return message_id

    def build_edit_message_request(self, chat_id: str, message_id: str, text: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `editMessageText`."""
        return f"{self._base_url}/editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text}

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """SPEC-v1.6.md §5/R-D3/R-D4: never raises -- every failure mode
        (transport error, "not found", any other Bot API rejection) maps
        to `False`, the exact signal `dashboard.refresh`'s self-heal path
        (R-D4) needs to recreate the board rather than silently losing it
        or propagating an exception into the log/undo flow that triggered
        the refresh. "message is not modified" (a 400 whose `description`
        says so -- Telegram's own no-op-edit rejection) maps to `True`:
        the board's content is already correct, which is what R-D3's own
        unchanged-render skip is checking for in the first place."""
        url, payload = self.build_edit_message_request(chat_id, message_id, text)
        try:
            resp = await self._client.post(url, json=payload)
        except httpx.HTTPError:
            logger.exception("editMessageText transport error for %s/%s", chat_id, message_id)
            return False
        if resp.status_code == 200:
            return True
        description = ""
        try:
            description = (resp.json() or {}).get("description") or ""
        except Exception:
            pass
        if "not modified" in description.lower():
            return True
        logger.info("editMessageText failed for %s/%s: %s", chat_id, message_id, description or resp.status_code)
        return False

    def build_unpin_request(self, chat_id: str, message_id: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `unpinChatMessage`."""
        return f"{self._base_url}/unpinChatMessage", {"chat_id": chat_id, "message_id": message_id}

    def build_delete_message_request(self, chat_id: str, message_id: str) -> tuple[str, dict[str, Any]]:
        """Exposed for testing: `deleteMessage`, the best-effort follow-up
        `unpin` makes after unpinning (SPEC-v1.6.md §5's own "(+deleteMessage)"
        note) -- a `/dashboard off` should leave no stray message behind,
        not just an unpinned one."""
        return f"{self._base_url}/deleteMessage", {"chat_id": chat_id, "message_id": message_id}

    async def unpin(self, chat_id: str, message_id: str) -> None:
        """Never raises -- both calls are individually best-effort/fail-
        open, mirroring every other "never blocks the triggering action"
        posture in this codebase (`core/audit.py:record`, `core/
        announce.py:announce_release`'s per-user sends)."""
        url, payload = self.build_unpin_request(chat_id, message_id)
        try:
            resp = await self._client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("unpinChatMessage failed for %s/%s; continuing", chat_id, message_id)
        del_url, del_payload = self.build_delete_message_request(chat_id, message_id)
        try:
            del_resp = await self._client.post(del_url, json=del_payload)
            del_resp.raise_for_status()
        except httpx.HTTPError:
            logger.exception("deleteMessage failed for %s/%s (best-effort); continuing", chat_id, message_id)

    @staticmethod
    def _chat_id_of(message: dict[str, Any]) -> str:
        """SPEC-v1.2.md §2.1: `message.chat.id` as a string -- Telegram
        sends it as a JSON int; every scoped db call in this app expects a
        `str` user_id, so the conversion happens once, here, at the
        channel boundary."""
        chat = message.get("chat") or {}
        return str(chat.get("id", ""))

    @staticmethod
    def _display_name_of(message: dict[str, Any]) -> str | None:
        """Integration step (IMPL-v1.2-access.md's own documented "Known
        limitations" #1): SPEC-v1.2.md §2.1 lists `message.from.first_name`
        as available inbound data -- `access.handle_gate`'s own
        `display_name` param (R-A2) was always built to accept it, but
        nothing extracted it from the update until now. Absent/blank ->
        `None` (the ABC's own documented "safe fallback to the chat id"
        contract; `access.py` already handles `None` correctly)."""
        sender = message.get("from") or {}
        name = sender.get("first_name")
        return name if name else None

    @staticmethod
    def _message_id_of(message: dict[str, Any]) -> str | None:
        """SPEC-v1.8.md R-S4: `message.message_id` as a `str` (Telegram
        sends it as a JSON int, same conversion-at-the-boundary rationale
        as `_chat_id_of` above), or `None` if absent."""
        message_id = message.get("message_id")
        return str(message_id) if message_id is not None else None

    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],
        on_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,
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
                    # SPEC-v1.1.md R-U4 / SPEC-v1.2.md R-C2: a callback
                    # (inline-button tap) is routed to on_callback with the
                    # tapping chat id first, then the client's spinner is
                    # ALWAYS dismissed via answerCallbackQuery -- even when
                    # on_callback is absent, raises, or the data is
                    # malformed (that validation lives in on_callback
                    # itself; this loop never inspects `data`).
                    cb_id = callback_query.get("id", "")
                    data = callback_query.get("data") or ""
                    message = callback_query.get("message") or {}
                    source_text = message.get("text") or ""
                    chat_id = self._chat_id_of(message)
                    if on_callback is not None:
                        try:
                            await on_callback(chat_id, data, source_text, cb_id)
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
                chat_id = self._chat_id_of(message)
                display_name = self._display_name_of(message)
                # SPEC-v1.8.md R-S4: the trailing, defaulted 4th arg -- see
                # channels/base.py's module docstring for the full
                # additive-signature rationale (mirrors display_name above).
                message_id = self._message_id_of(message)
                try:
                    await on_message(chat_id, text, display_name, message_id)
                except Exception:
                    logger.exception("on_message handler raised; continuing inbound loop")

    async def aclose(self) -> None:
        await self._client.aclose()
