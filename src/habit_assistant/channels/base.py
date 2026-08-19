"""Channel abstraction (SPEC.md §8). Every messaging platform integration
implements this ABC. No module in core/ or storage/ may import a concrete
channel — only this base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Awaitable, Callable


class Channel(ABC):
    @abstractmethod
    async def send(self, text: str) -> None:
        """Push a message to the user."""

    @abstractmethod
    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        """Run the inbound loop, awaiting on_message(text) for every
        inbound message. Runs until cancelled."""

    # ROADMAP.md v1.0.0 "Insights: Charts-as-Images": a concrete (not
    # abstract) default so a channel that can't/hasn't implemented image
    # upload -- channels/line.py's stub, test fakes, any future channel --
    # degrades to a plain text send of the caption instead of being forced
    # to implement this or raise NotImplementedError. A channel that CAN
    # post an image (channels/telegram.py, via sendPhoto) overrides it.
    async def send_image(self, image: bytes, caption: str) -> None:
        """Push an image with a caption. Default: send the caption as text."""
        await self.send(caption)
