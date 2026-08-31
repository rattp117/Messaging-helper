"""LINE Messaging API channel (SPEC-LINE.md §4 Module A).

Two economics drive every design choice here: LINE **Reply API** sends
(triggered by an inbound event's single-use `replyToken`) are free and
uncounted; **Push API** sends count against the account's monthly quota.
`send`/`send_actionable`/`send_image` therefore don't call the API
directly -- they append a LINE message object to a per-event reply buffer
(a `contextvars.ContextVar`, R-A4) that `channels/line_webhook.py`'s worker
sets before `await`-ing the inbound handler and flushes as ONE reply call
after. Outside any active reply context (only the daily digest, module C)
a send goes out via Push and increments `push_ledger` (R-A6/R-C6) -- the
channel is the single, authoritative place that quota spend is counted,
regardless of caller.

`LineChannel` still satisfies `channels.base.Channel` exactly, so
`core/`/`storage/` are unaffected (verified: neither package imports this
module, tests/test_channels.py's own AST scan enforces it)."""

from __future__ import annotations

import contextlib
import logging
import secrets as secrets_module
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import httpx

from habit_assistant.channels.base import Button, Channel
from habit_assistant.channels.line_webhook import LineWebhookServer

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.storage.db import Database

logger = logging.getLogger(__name__)

LINE_API_ROOT = "https://api.line.me"
# LINE splits its API surface across two hosts: JSON management calls
# (create/set-default rich menu, push/reply, ...) stay on api.line.me,
# but any call that moves BINARY content (upload/download of rich-menu
# images, message-content download) lives on api-data.line.me instead --
# hitting api.line.me for those 404s in production (hotfix v1.0.2, found
# live on the VPS: register_rich_menu's content upload was on the wrong
# host). Currently the only binary-content call this file makes.
LINE_API_DATA_ROOT = "https://api-data.line.me"

# LINE Messaging API hard limits (SPEC-LINE.md §7): at most 5 message
# objects per reply/push, at most 13 quickReply items, postback `data`
# capped at 300 chars.
_MAX_REPLY_MESSAGES = 5
_MAX_QUICK_REPLY_ITEMS = 13
_MAX_POSTBACK_DATA_CHARS = 300

# R-A4: {"replyToken": str, "buffer": list[message-object]} while an event
# is being handled; `None` outside any reply context (a proactive/scheduled
# send -- only the digest). Module-level (not an instance attribute) is
# deliberate: contextvars are the whole point -- they propagate correctly
# through the `await on_message(...)` call the worker makes, without
# threading the buffer through every core/ call site.
_REPLY_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("line_reply_context", default=None)


def _default_rich_menu_payload() -> dict[str, Any]:
    """SPEC-LINE.md §9 OQ3's own default: a plain 6-button layout
    (`/log /habits /heatmap /wrapped /help /guide`) as message actions --
    each arrives as an ordinary inbound text message and routes through
    the existing dispatch unchanged (R-A10). 2500x1686, a LINE-supported
    rich menu size; a 3x2 grid of message-action areas. The deployment PNG
    (module D, `[line].rich_menu_image`) must match these dimensions."""
    labels = ["/log", "/habits", "/heatmap", "/wrapped", "/help", "/guide"]
    width, height, cols, rows = 2500, 1686, 3, 2
    cell_w, cell_h = width // cols, height // rows
    areas = []
    for i, label in enumerate(labels):
        row, col = divmod(i, cols)
        areas.append(
            {
                "bounds": {"x": col * cell_w, "y": row * cell_h, "width": cell_w, "height": cell_h},
                "action": {"type": "message", "text": label},
            }
        )
    return {
        "size": {"width": width, "height": height},
        "selected": False,
        "name": "habit-assistant-default",
        "chatBarText": "Menu",
        "areas": areas,
    }


class LineChannel(Channel):
    def __init__(
        self,
        channel_access_token: str,
        channel_secret: str,
        owner_user_id: str,
        config: "Config",
        db: "Database",
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = channel_access_token
        self._channel_secret = channel_secret
        self.owner_user_id = owner_user_id
        self._config = config
        self.db = db
        self._client = client or httpx.AsyncClient(timeout=30.0)
        self._media_dir = Path(config.line.media_dir)
        self._media_dir.mkdir(parents=True, exist_ok=True)
        # Readable-approval feature (branch line-version): per-process
        # "already tried a profile fetch for this user" set -- see
        # `_display_name_for`'s own docstring below for why this cap
        # exists (LINE gives no inline display name the way Telegram's
        # `message.from.first_name` does, so fetching it is a real API
        # call that must not fire on every single inbound message).
        self._profile_fetch_attempted: set[str] = set()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    # -- profile lookup (readable-approval feature, branch line-version) ----

    async def get_profile(self, user_id: str) -> str | None:
        """LINE's Get Profile API -- a JSON management call, so it stays on
        `api.line.me` (never `api-data.line.me`, reserved for binary
        content per this module's own host-split rationale above).
        Returns the account's `displayName`, or `None` on ANY failure
        (network error, non-2xx, missing/blank field) -- fail-open by
        design, never raises: a profile lookup is a readability
        nice-to-have (making the owner's approval flow show a name
        instead of an opaque `U...` id), never allowed to block or crash
        onboarding, mirroring `register_rich_menu`'s own fail-open
        posture."""
        try:
            resp = await self._client.get(
                f"{LINE_API_ROOT}/v2/bot/profile/{user_id}",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            name = resp.json().get("displayName")
            return name or None
        except Exception:
            logger.warning(
                "LINE get_profile failed for user_id=%r; continuing without a display name", user_id, exc_info=True
            )
            return None

    async def _display_name_for(self, user_id: str) -> str | None:
        """Resolves `user_id`'s display name for `run`'s own wrapped
        `on_message` below, fetching `get_profile` AT MOST ONCE per
        process lifetime per user -- the wrapper calls this for every
        inbound message, but the actual network call must be capped, not
        made on every one.

        A `users` row that already carries a `display_name` (persisted
        by a previous call this process, or from an earlier process run)
        short-circuits without even touching `_profile_fetch_attempted`
        -- covers a process restart mid-onboarding, and every
        already-onboarded user, neither of which should ever re-fetch.
        Otherwise `user_id` is fetched (and marked attempted, success OR
        fail) only the FIRST time this is called for it within this
        process's lifetime -- a still-pending/still-unknown user's every
        subsequent message reuses that one outcome (including a `None`
        from a failed fetch -- not retried per-message, only on the next
        process start)."""
        try:
            existing = self.db.get_user(user_id)
        except Exception:
            existing = None
        if existing is not None and existing["display_name"]:
            return existing["display_name"]
        if user_id in self._profile_fetch_attempted:
            return None
        self._profile_fetch_attempted.add(user_id)
        return await self.get_profile(user_id)

    # -- reply-buffer plumbing (R-A4) ---------------------------------------

    @contextlib.contextmanager
    def _reply_scope(self, reply_token: str, owner_chat_id: str | None = None):
        """Release-gate Finding 2 (branch `line-version`): `owner_chat_id`
        is the chat_id the INBOUND EVENT this reply context was opened for
        actually belongs to -- additive, keyword-defaulted `None` for
        back-compat with a bare call site that has no owning chat_id to
        compare against (see `_emit`'s own docstring for what `None` means
        there). The real production caller, `channels/line_webhook.py:
        LineWebhookServer._dispatch`, always knows the event's own
        `user_id` and passes it here."""
        ctx: dict[str, Any] = {"replyToken": reply_token, "buffer": [], "ownerChatId": owner_chat_id}
        token = _REPLY_CONTEXT.set(ctx)
        try:
            yield ctx
        finally:
            _REPLY_CONTEXT.reset(token)

    async def _emit(self, chat_id: str, message_objs: list[dict[str, Any]]) -> None:
        """Release-gate Finding 2: buffer into the active reply context
        (R-A4) only when `chat_id` matches the context's own owner --
        otherwise push immediately (R-A6), exactly as if there were no
        active reply context at all. This covers the genuine cross-user
        sends this app makes mid-event (`core/access.py:handle_gate`'s
        owner-pending-approval alert, `execute_admin`'s access-granted
        notice to a newly-approved user, `core/health.py`'s owner alert
        if ever wired here) -- before this fix, ANY send made while
        handling one user's event was folded into THAT user's own reply
        buffer regardless of who it was actually addressed to, so an
        owner notification triggered by a stranger's message silently
        never reached the owner and instead leaked owner-facing text
        (including the raw `/approve <id>` admin command) into the
        stranger's own reply.

        `ctx["ownerChatId"] is None` (the bare/default form of
        `_reply_scope`, used directly at the channel level with no
        owning event in scope -- e.g. a hand-rolled test) buffers
        regardless of `chat_id`, preserving the original "buffer
        everything sent during this scope" shape for a caller that has
        no owning chat_id to compare against; every real production
        call site (the webhook worker) always supplies one.

        Compounding note: owner pending-approval / approval notifications
        now cost push quota (a real Push API call + one `push_ledger`
        increment, R-C6) instead of being free-riding on the triggering
        event's own reply -- correct (SPEC-LINE.md's own U-ISO/no-cross-
        user-leakage discipline requires the send actually reach its real
        target) and rare (only fires on a new-user approval request or an
        admin action, not on ordinary logging traffic)."""
        ctx = _REPLY_CONTEXT.get()
        if ctx is not None and (ctx["ownerChatId"] is None or ctx["ownerChatId"] == chat_id):
            ctx["buffer"].extend(message_objs)
            return
        await self._push(chat_id, message_objs)

    async def _push(self, chat_id: str, messages: list[dict[str, Any]]) -> None:
        """R-A6/R-C6: the Push API call, and the ONLY place `push_ledger`
        is incremented -- authoritative regardless of caller. Only counted
        on success; a failed push (raise_for_status) never inflates the
        ledger."""
        resp = await self._client.post(
            f"{LINE_API_ROOT}/v2/bot/message/push",
            headers=self._auth_headers(),
            json={"to": chat_id, "messages": messages},
        )
        resp.raise_for_status()
        yyyymm = datetime.now().strftime("%Y-%m")
        self.db.increment_push(chat_id, yyyymm)

    async def _flush_reply(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        """R-A5: the reply uses `reply_token` exactly once. A rejected
        token (expired/already used) is logged and dropped -- NEVER falls
        back to a push, which would spend quota for output the user didn't
        get anyway (they can just re-send)."""
        if not messages:
            return
        if len(messages) > _MAX_REPLY_MESSAGES:
            logger.warning(
                "LINE reply for token %s carries %d message objects; dropping the overflow (limit %d)",
                reply_token,
                len(messages),
                _MAX_REPLY_MESSAGES,
            )
            messages = messages[:_MAX_REPLY_MESSAGES]
        try:
            resp = await self._client.post(
                f"{LINE_API_ROOT}/v2/bot/message/reply",
                headers=self._auth_headers(),
                json={"replyToken": reply_token, "messages": messages},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            logger.warning(
                "LINE reply failed for token %s (expired/already used?); dropping, never falling back to push",
                reply_token,
                exc_info=True,
            )

    # -- Channel ABC ----------------------------------------------------------

    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> str | None:
        # disable_notification: no LINE equivalent (§ degradation table) --
        # accepted for ABC conformance, ignored.
        await self._emit(chat_id, [{"type": "text", "text": text}])
        return None

    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        if len(buttons) > _MAX_QUICK_REPLY_ITEMS:
            logger.warning(
                "send_actionable for %s carries %d buttons; truncating to LINE's %d-item quickReply limit",
                chat_id,
                len(buttons),
                _MAX_QUICK_REPLY_ITEMS,
            )
        items = []
        for label, data in buttons[:_MAX_QUICK_REPLY_ITEMS]:
            if len(data) > _MAX_POSTBACK_DATA_CHARS:
                logger.warning(
                    "postback data for %s is %d chars, over LINE's %d-char limit; sending verbatim",
                    chat_id,
                    len(data),
                    _MAX_POSTBACK_DATA_CHARS,
                )
            items.append({"type": "action", "action": {"type": "postback", "label": label, "data": data}})
        message_obj: dict[str, Any] = {"type": "text", "text": text}
        if items:
            message_obj["quickReply"] = {"items": items}
        await self._emit(chat_id, [message_obj])

    async def send_image(self, chat_id: str, image: bytes, caption: str, *, disable_notification: bool = False) -> None:
        # R-A11: write the PNG under a random unguessable token, build the
        # public media URL, and emit a text (caption) + image message pair
        # as one unit. Any failure here (disk write, ...) propagates -- the
        # existing caller's own try/except degrades to a text summary
        # (R-3.5); this method must not swallow it.
        token = secrets_module.token_urlsafe(16)
        path = self._media_dir / f"{token}.png"
        path.write_bytes(image)
        url = f"{self._config.line.public_base_url}/media/{token}.png"
        await self._emit(
            chat_id,
            [
                {"type": "text", "text": caption},
                {"type": "image", "originalContentUrl": url, "previewImageUrl": url},
            ],
        )

    async def register_rich_menu(self) -> None:
        """R-A10/AC14: create + upload + set-as-default the one static rich
        menu, fail-open throughout -- a missing image asset or any API
        failure is logged and startup continues."""
        image_path = Path(self._config.line.rich_menu_image)
        if not image_path.is_file():
            logger.warning("Rich menu image %s not found; skipping rich menu registration", image_path)
            return
        try:
            create_resp = await self._client.post(
                f"{LINE_API_ROOT}/v2/bot/richmenu",
                headers=self._auth_headers(),
                json=_default_rich_menu_payload(),
            )
            create_resp.raise_for_status()
            rich_menu_id = create_resp.json()["richMenuId"]

            content_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
            upload_resp = await self._client.post(
                f"{LINE_API_DATA_ROOT}/v2/bot/richmenu/{rich_menu_id}/content",
                headers={**self._auth_headers(), "Content-Type": content_type},
                content=image_path.read_bytes(),
            )
            upload_resp.raise_for_status()

            default_resp = await self._client.post(
                f"{LINE_API_ROOT}/v2/bot/user/all/richmenu/{rich_menu_id}",
                headers=self._auth_headers(),
            )
            default_resp.raise_for_status()
            logger.info("LINE rich menu %s registered as the default", rich_menu_id)
        except Exception:
            logger.exception("LINE rich menu registration failed; continuing startup (fail-open)")

    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],
        on_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        # As with TelegramChannel.run (channels/telegram.py), this actually
        # calls on_message with 5 positional args (userId, text, None,
        # message_id, None) -- see channels/base.py's module docstring for
        # why the ABC's own type hint stays the conservative 2-arg shape.
        #
        # Readable-approval feature (branch line-version): wraps the
        # caller's `on_message` so the 3rd positional arg (`display_name`,
        # always `None` from `LineWebhookServer.process_event` -- LINE
        # never includes it inline) is resolved via `_display_name_for`
        # right here, at the channel boundary -- exactly where Telegram's
        # own `_display_name_of` (channels/telegram.py) fills the same
        # slot from its update payload instead. `LineWebhookServer` itself
        # stays DB/profile-fetch-free (its own docstring's "independently
        # testable with fake callables" contract, unchanged).
        async def _on_message_with_profile(
            user_id: str,
            text: str,
            _display_name: str | None,
            message_id: str | None,
            reply_to_message_id: str | None,
        ) -> None:
            name = await self._display_name_for(user_id)
            await on_message(user_id, text, name, message_id, reply_to_message_id)

        server = LineWebhookServer(
            channel_secret=self._channel_secret,
            bind_host=self._config.line.bind_host,
            bind_port=self._config.line.bind_port,
            media_dir=self._media_dir,
            media_ttl_seconds=self._config.line.media_ttl_seconds,
            reply_scope=self._reply_scope,
            flush_reply=self._flush_reply,
        )
        await server.serve(_on_message_with_profile, on_callback)

    async def aclose(self) -> None:
        await self._client.aclose()

    # send_and_pin / edit_message / unpin / set_message_reaction /
    # answer_callback_query / set_my_commands: inherited base no-op/degrade
    # defaults (R-A14) -- LINE has no pin/edit/reaction/spinner/command-menu
    # concept; the base Channel ABC's defaults are exactly the documented
    # degradation (§ degradation table).
