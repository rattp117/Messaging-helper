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
