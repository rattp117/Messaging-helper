"""Runtime health monitoring (ROADMAP.md v0.4.0 "Runtime Resilience &
Self-Monitoring", scope item 3): a periodic reachability monitor for the
only two hosts this app is ever allowed to talk to -- Ollama and Telegram
(SPEC.md's local-first constraint). Read-only calls only (`/api/version`,
`getMe`) to those two already-allowed hosts, no new outbound destination
(AC3.5).

Tracks UP/DOWN transitions per service and alerts the channel exactly
once per UP->DOWN transition (AC3.2) -- never once per failed check, and
never again until a DOWN->UP->DOWN round trip happens. The alert is
always logged too, since for a Telegram outage the channel itself may be
the thing that's down; the log line is the fallback record in that case.

No channel imports here beyond the abstract Channel (SPEC.md §8 seam) --
same convention as core/reminders.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

import httpx

from habit_assistant.channels.base import Channel
from habit_assistant.core import i18n

logger = logging.getLogger("habit_assistant")

# ROADMAP.md v0.6.0: alert copy now lives in core/i18n.py's catalog.
# These two names stay as the resolved *English* text (== CATALOG[id]["en"])
# so existing imports/tests keep working unchanged; the localized send
# path is HealthMonitor.__init__'s `language` param below (production
# wiring in main.py passes `i18n.resolve_unprompted_language(config)`,
# defaulting to Thai -- health alerts are unprompted, same rule as
# reminders and the weekly review).
OLLAMA_DOWN_MESSAGE = i18n.t("ollama_down_alert", "en")
TELEGRAM_DOWN_MESSAGE = i18n.t("telegram_down_alert", "en")


class HealthMonitor:
    """Polls Ollama `/api/version` and Telegram `getMe` every
    `interval_seconds` (config.toml `[health].interval_seconds`).

    Exposes `.ollama_up` / `.telegram_up` for other components --
    `main.py:handle_inbound_message` reads `.ollama_up` to decide whether
    to defer an inbound message (AC3.3) instead of calling the LLM at
    all. `on_ollama_recovered` fires once per DOWN->UP transition so
    `main.py` can re-parse anything deferred while it was down."""

    def __init__(
        self,
        ollama_base_url: str,
        telegram_bot_token: str,
        *,
        interval_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        channel: Channel | None = None,
        on_ollama_recovered: Callable[[], Awaitable[None]] | None = None,
        language: i18n.Language = "en",
    ):
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._telegram_base_url = f"https://api.telegram.org/bot{telegram_bot_token}"
        self._interval = interval_seconds
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = client is None
        self._channel = channel
        self._on_ollama_recovered = on_ollama_recovered
        self._language = language

        # Optimistic initial state: a check must actually run and observe
        # a transition before anything is alerted -- a slow/failing first
        # check is not itself treated as "just went down" more than once,
        # and startup catch-up of any already-deferred backlog is handled
        # explicitly by main.py (reparse_pending_unparsed), not by this
        # flag's initial value.
        self.ollama_up: bool = True
        self.telegram_up: bool = True

    async def check_ollama(self) -> bool:
        try:
            resp = await self._client.get(f"{self._ollama_base_url}/api/version")
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("Ollama health check failed: %s", exc)
            return False

    async def check_telegram(self) -> bool:
        try:
            resp = await self._client.get(f"{self._telegram_base_url}/getMe")
            resp.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("Telegram health check failed: %s", exc)
            return False

    async def _alert(self, message: str) -> None:
        """Log is always the record of a DOWN transition; pushing it to
        the channel is best-effort. For a Telegram-down alert this is
        expected to sometimes fail to actually reach the user -- that's
        fine, the log line above it already fired (per ROADMAP.md v0.4.0:
        "log is the fallback when Telegram itself is down")."""
        logger.warning(message)
        if self._channel is not None:
            try:
                await self._channel.send(message)
            except Exception:
                logger.exception(
                    "Health alert channel send failed; log line above is the fallback record"
                )

    async def run_once(self) -> None:
        """One check cycle for both services. Updates `.ollama_up` /
        `.telegram_up`, alerts exactly once per UP->DOWN transition
        (AC3.2), and fires `on_ollama_recovered` exactly once per
        DOWN->UP transition."""
        ollama_now = await self.check_ollama()
        if self.ollama_up and not ollama_now:
            await self._alert(i18n.t("ollama_down_alert", self._language))
        elif not self.ollama_up and ollama_now:
            logger.info("Ollama reachability recovered")
            if self._on_ollama_recovered is not None:
                try:
                    await self._on_ollama_recovered()
                except Exception:
                    logger.exception("on_ollama_recovered callback failed")
        self.ollama_up = ollama_now

        telegram_now = await self.check_telegram()
        if self.telegram_up and not telegram_now:
            await self._alert(i18n.t("telegram_down_alert", self._language))
        elif not self.telegram_up and telegram_now:
            logger.info("Telegram reachability recovered")
        self.telegram_up = telegram_now

    async def run(self) -> None:
        """Loop forever at `interval_seconds`. A cycle that raises
        unexpectedly is logged and swallowed -- the monitor itself must
        never crash the process (mirrors the inbound loop's own
        never-raise contract, AC3.4)."""
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("Health monitor cycle failed unexpectedly; continuing")
            await asyncio.sleep(self._interval)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
