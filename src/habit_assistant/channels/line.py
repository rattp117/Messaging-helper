"""Future LINE channel — documented stub only, not implemented for MVP.

Why LINE is harder than Telegram:
- Outbound (`send`): use the LINE Messaging API "push message" endpoint
  (POST https://api.line.me/v2/bot/message/push) with a channel access
  token in the Authorization header. Reasonably close to Telegram's
  sendMessage.
- Inbound (`run`): LINE has no long-poll equivalent to Telegram's
  getUpdates. It requires a **webhook** — a publicly reachable HTTPS
  endpoint that LINE POSTs signed events to. That means a LineChannel.run()
  implementation needs:
    1. A small HTTP server (e.g. aiohttp or a single-route FastAPI app)
       bound to a public port.
    2. A public URL — either a real public IP + TLS cert, or a tunnel
       (ngrok / Cloudflare Tunnel) during development.
    3. Signature verification of each inbound request using the channel
       secret (`X-Line-Signature` header, HMAC-SHA256 over the raw body),
       to reject spoofed events before calling on_message.

None of this is wired up. When it is, LineChannel must still satisfy
habit_assistant.channels.base.Channel exactly, so core/reminders.py,
core/parser.py, core/review.py, and storage/db.py continue to work
unmodified — they only ever depend on the Channel ABC, never on Telegram
or LINE specifics.
"""

from __future__ import annotations

from typing import Awaitable, Callable

from habit_assistant.channels.base import Channel


class LineChannel(Channel):
    """Not implemented. See module docstring for the design this will need."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError("LineChannel is a documented stub; see channels/line.py docstring.")

    async def send(self, text: str) -> None:
        raise NotImplementedError

    async def run(self, on_message: Callable[[str], Awaitable[None]]) -> None:
        raise NotImplementedError
