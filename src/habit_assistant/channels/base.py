"""Channel abstraction (SPEC.md §8). Every messaging platform integration
implements this ABC. No module in core/ or storage/ may import a concrete
channel — only this base class.

SPEC-v1.2.md "Multi-user support" (R-C1/R-C2): sends became per-recipient
-- every send takes an explicit `chat_id` (the address to deliver to) --
and `run` extracts a chat id from each inbound update and passes it first
to the caller's handler, so no core code may assume a single global user
(§2.1).

Integration step (IMPL-v1.2-access.md's own documented "Known limitations"
#1): `on_message` may optionally be called with a THIRD argument,
`display_name` -- the sender's Telegram `first_name` when the update
carries one, else `None`. It's a trailing, DEFAULTED parameter (not a
required positional), so this is additive, not a breaking signature
change: any existing `on_message(chat_id, text)`-shaped caller (every
pre-integration test fake, `channels/line.py`'s stub, a channel that
doesn't bother extracting a display name) keeps working unmodified --
only `TelegramChannel.run()` actually supplies it, and only
`main.py:on_message` actually reads it (threaded into
`access.handle_gate`'s own `display_name` param so `access_request`/
`/users` can show a friendly name instead of a bare chat id, R-A2).

SPEC-v1.8.md R-S4 (shared surface, module `quicklog`'s own dependency):
`on_message` may likewise optionally be called with a FOURTH argument,
`message_id` -- the inbound `message.message_id` (as `str`) of the
message that triggered it, or `None`. Same additive/trailing/defaulted
shape as `display_name` just above, so every 2- or 3-arg caller/fake is
unaffected (AC-4); only `TelegramChannel.run()` actually supplies it (for
a text message -- a callback-query update carries no loggable inbound
message and never reaches `on_message` at all), and only
`main.py:on_message` threads it into `handle_inbound_message`'s own
`inbound_message_id` param, where module `quicklog`'s reaction call
(R-Q4) consumes it at integration time."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

# SPEC-v1.1.md §5: an inline-keyboard button is a (label, callback_data) pair.
Button = tuple[str, str]


class Channel(ABC):
    @abstractmethod
    async def send(self, chat_id: str, text: str, *, disable_notification: bool = False) -> None:
        """Push a message to `chat_id`.

        SPEC-v1.8.md R-S1 (shared surface): `disable_notification` is an
        additive, keyword-only, DEFAULTED param -- `False` (the default)
        must produce a payload byte-identical to pre-v1.8 (AC-1), so every
        existing caller/fake that still calls `send(chat_id, text)` is
        unaffected. `True` is used by the three proactive-send sites
        (`reminders.send_reminder`, `checkins.run_due_checkins`,
        `nudge.run_due_nudges`, module `riders`, R-D1) when
        `[notifications] silent_proactive` is on -- user-initiated
        confirmations/replies and the one-time dashboard pin never pass
        `True`."""

    @abstractmethod
    async def run(
        self,
        on_message: Callable[[str, str], Awaitable[None]],
        on_callback: Callable[[str, str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        """Run the inbound loop, awaiting on_message(chat_id, text) for
        every inbound message. Runs until cancelled.

        SPEC-v1.2.md R-C2: `chat_id` is extracted from each update
        (`message.chat.id`, or `callback_query.message.chat.id` for a
        button tap) and passed FIRST to the handler -- `on_message(chat_id,
        text)` -- so every downstream handler knows who is acting without
        a second lookup.

        SPEC-v1.1.md R-U4 (still true in v1.2): `on_callback`, if given, is
        awaited for every inbound callback-query update (Telegram
        inline-button tap) as `on_callback(chat_id, data,
        source_message_text, callback_id)`. Optional and defaulted to
        `None` for back-compat -- a channel/caller that doesn't care about
        callbacks is unaffected.

        Integration step: a channel implementation MAY additionally call
        `on_message` with a third argument, `display_name: str | None`
        (the type hint above stays the conservative 2-arg shape so a
        caller that ignores this is still type-correct) -- see the module
        docstring above. `TelegramChannel.run()` does; this base ABC's
        contract doesn't require it of every implementation, matching the
        `send_image`/`send_actionable`/`set_my_commands` degradation
        pattern just below (an implementation that can't/doesn't supply
        one is unaffected, and any real caller's own `on_message` gives
        that trailing parameter a default so it never breaks either way).
        """

    # ROADMAP.md v1.0.0 "Insights: Charts-as-Images": a concrete (not
    # abstract) default so a channel that can't/hasn't implemented image
    # upload -- channels/line.py's stub, test fakes, any future channel --
    # degrades to a plain text send of the caption instead of being forced
    # to implement this or raise NotImplementedError. A channel that CAN
    # post an image (channels/telegram.py, via sendPhoto) overrides it.
    async def send_image(self, chat_id: str, image: bytes, caption: str) -> None:
        """Push an image with a caption to `chat_id`. Default: send the
        caption as text."""
        await self.send(chat_id, caption)

    # SPEC-v1.1.md R-U7: concrete defaults, mirroring send_image's
    # degradation pattern above, so the many existing test fakes and
    # channels/line.py's stub keep working unmodified -- no fake/subclass
    # must implement send_actionable/set_my_commands/answer_callback_query
    # to satisfy the Channel ABC.
    async def send_actionable(self, chat_id: str, text: str, buttons: list[Button]) -> None:
        """Push a message with inline action buttons to `chat_id`.
        Default: drop the buttons and send the text only (only Telegram
        can render an inline keyboard)."""
        await self.send(chat_id, text)

    async def set_my_commands(
        self, commands: dict[str, list[tuple[str, str]]], *, scope_chat_id: str | None = None
    ) -> None:
        """Register the platform's command menu. `commands` =
        {lang_code: [(command, description), ...]}. SPEC-v1.2.md R-C1:
        stays global (not per-chat) -- Telegram's `setMyCommands` sets the
        menu for every user of the bot in one call, not per recipient.

        SPEC-v1.8.md R-S3 (shared surface, module `riders`' own
        dependency): `scope_chat_id`, additive keyword-only and defaulted
        to `None` -- `None` registers the DEFAULT (global) menu,
        byte-identical to v1.7 (AC-3); a non-`None` chat id additionally
        scopes the call to just that chat (Telegram's `scope={type:chat,
        chat_id:...}`), letting `main.py`'s integration step register a
        second, owner-only menu at the owner's own chat id (R-D2) without
        this signature change affecting the existing global-menu call.
        Default: no-op (only Telegram's Bot API has a `setMyCommands`
        concept)."""
        return None

    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        """Acknowledge an inline-button tap (dismisses the client's loading
        spinner). Addressed by `callback_id`, not `chat_id` -- Telegram's
        `answerCallbackQuery` is keyed on the callback itself. Default:
        no-op (only Telegram implements this)."""
        return None

    # SPEC-v1.6.md §2.2/§5 (shared surface, module `dashboard`'s own
    # dependency): three more concrete defaults, same degradation pattern
    # as `send_image`/`send_actionable`/`set_my_commands` above -- a
    # channel that can't pin/edit/unpin (channels/line.py's stub, every
    # existing test fake) is unaffected and needs no changes to keep
    # satisfying this ABC. `TelegramChannel` overrides all three.
    async def send_and_pin(self, chat_id: str, text: str) -> str | None:
        """Send + pin `text` to `chat_id`, returning the new message's id
        (for a caller to later `edit_message`/`unpin` it). Default: just
        `send` the text and report no id (no pin capability -- there is
        nothing a caller could `edit_message`/`unpin` with `None`, so
        `core/dashboard.py` (module `dashboard`) treats a `None` result
        the same as a failed pin: the dashboard write is skipped, fail-
        open, never blocking the confirmation that triggered it)."""
        await self.send(chat_id, text)
        return None

    async def edit_message(self, chat_id: str, message_id: str, text: str) -> bool:
        """Edit a previously-sent message in place. `True` on success OR
        when the platform reports "not modified" (the content was
        already identical -- not a failure, R-D3's own "skip a redundant
        edit" case still counts as "the board is correct"); `False` on
        "not found" (the message was deleted -- R-D4's self-heal signal)
        or any other failure. Default: always `False` (no edit
        capability -- a channel without this can never report "already
        correct" or "found", so the honest, fail-safe answer is "not
        done", which is exactly the signal that tells a caller like
        `dashboard.refresh` to fall back to re-sending)."""
        return False

    async def unpin(self, chat_id: str, message_id: str) -> None:
        """Unpin (and best-effort delete) a previously pinned message.
        Default: no-op (no pin capability, so there is nothing to
        undo)."""
        return None

    # SPEC-v1.8.md §2.2/§5 (shared surface, module `quicklog`'s own
    # dependency, R-S2): same concrete-default degradation pattern as
    # `send_image`/`send_actionable`/`set_my_commands`/`send_and_pin`/
    # `edit_message`/`unpin` above -- a channel that can't react to a
    # message (channels/line.py's stub, every existing test fake) needs no
    # changes to keep satisfying this ABC. `TelegramChannel` overrides it
    # via Bot API 7.0's `setMessageReaction`, and (unlike `edit_message`,
    # which reports failure via its return value) NEVER RAISES -- a
    # reaction is purely decorative (AC-2/AC-A4), so even a transport error
    # must not propagate into the log-confirmation flow that triggers it.
    async def set_message_reaction(self, chat_id: str, message_id: str, emoji: str) -> None:
        """Set one emoji reaction on a previously-sent message. Default:
        no-op (no reaction capability)."""
        return None
