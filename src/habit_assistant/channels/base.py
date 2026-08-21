"""Channel abstraction (SPEC.md §8). Every messaging platform integration
implements this ABC. No module in core/ or storage/ may import a concrete
channel — only this base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable

# SPEC-v1.1.md §5: an inline-keyboard button is a (label, callback_data) pair.
Button = tuple[str, str]


class Channel(ABC):
    @abstractmethod
    async def send(self, text: str) -> None:
        """Push a message to the user."""

    @abstractmethod
    async def run(
        self,
        on_message: Callable[[str], Awaitable[None]],
        on_callback: Callable[[str, str, str], Awaitable[None]] | None = None,
    ) -> None:
        """Run the inbound loop, awaiting on_message(text) for every
        inbound message. Runs until cancelled.

        SPEC-v1.1.md R-U4: `on_callback`, if given, is awaited for every
        inbound callback-query update (Telegram inline-button tap) as
        `on_callback(data, source_message_text, callback_id)`. Optional and
        defaulted to `None` for back-compat -- a channel/caller that
        doesn't care about callbacks (every pre-v1.1 caller) is unaffected.
        """

    # ROADMAP.md v1.0.0 "Insights: Charts-as-Images": a concrete (not
    # abstract) default so a channel that can't/hasn't implemented image
    # upload -- channels/line.py's stub, test fakes, any future channel --
    # degrades to a plain text send of the caption instead of being forced
    # to implement this or raise NotImplementedError. A channel that CAN
    # post an image (channels/telegram.py, via sendPhoto) overrides it.
    async def send_image(self, image: bytes, caption: str) -> None:
        """Push an image with a caption. Default: send the caption as text."""
        await self.send(caption)

    # SPEC-v1.1.md R-U7: concrete defaults, mirroring send_image's
    # degradation pattern above, so the ~15 existing test fakes and
    # channels/line.py's stub keep working unmodified -- no fake/subclass
    # must implement send_actionable/set_my_commands/answer_callback_query
    # to satisfy the Channel ABC.
    async def send_actionable(self, text: str, buttons: list[Button]) -> None:
        """Push a message with inline action buttons attached. Default:
        drop the buttons and send the text only (only Telegram can render
        an inline keyboard)."""
        await self.send(text)

    async def set_my_commands(self, commands: dict[str, list[tuple[str, str]]]) -> None:
        """Register the platform's command menu. `commands` =
        {lang_code: [(command, description), ...]}. Default: no-op (only
        Telegram's Bot API has a `setMyCommands` concept)."""
        return None

    async def answer_callback_query(self, callback_id: str, text: str | None = None) -> None:
        """Acknowledge an inline-button tap (dismisses the client's loading
        spinner). Default: no-op (only Telegram implements this)."""
        return None
