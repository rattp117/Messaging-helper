"""Entry point + CLI (SPEC.md §10, ROADMAP.md v0.3.0/v0.4.0).

SPEC-REFACTOR.md Stage 2 (rule 9): this module is now a thin shim --
`setup_logging`, `build_arg_parser`, and `main()` are the only real code
left here; `async_main`'s real body lives in `core/app.py`, the scheduler
job bodies in `core/jobs.py`, inbound routing in `core/routing.py`, and the
confirmation-formatting leaf in `core/confirmation.py`.

The functions below that AREN'T bare re-exports (`async_main`,
`handle_inbound_message`, `reparse_pending_unparsed`) are back-compat
wrapper shims, not straight `from core.X import Y` aliases: many existing
tests do `monkeypatch.setattr(main_module, "load_config", fake)` (and the
same for `load_secrets`/`setup_logging`/`AsyncIOScheduler`/`TelegramChannel`/
`OllamaClient`/`HealthMonitor`/`__version__`/`run_due_reminders`/
`render_weekly_review_charts`/`parse_message`), then call
`main_module.async_main(...)` (or `handle_inbound_message`/
`reparse_pending_unparsed`) and expect the patched value to take effect. A
bare re-export would bind the real implementation's OWN module globals at
definition time, in `core/app.py`/`core/routing.py`, where the patch on
THIS module's name would never be seen. Each wrapper below instead reads
this module's current globals at call time and threads them through
explicitly -- exactly what a direct call used to do before the split.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from habit_assistant import __version__
from habit_assistant.channels.line import LineChannel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config, load_config, load_secrets
from habit_assistant.core import i18n, routing
from habit_assistant.core.app import async_main as _async_main
from habit_assistant.core.confirmation import ordinal  # noqa: F401 -- back-compat re-export
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.parser import parse_message
from habit_assistant.core.review import render_weekly_review_charts
from habit_assistant.core.reminders import run_due_reminders
from habit_assistant.core.routing import _execute_snooze  # noqa: F401 -- back-compat re-export
from habit_assistant.llm.ollama_client import OllamaClient
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ROADMAP.md v0.7.0 "Multi-Habit Extensibility": the default habit registry,
# used only by `build_arg_parser()`'s `--test-reminder` choices (argparse
# runs before `async_main` loads config).
_DEFAULT_REGISTRY = HabitRegistry.from_config(Config())

# Kept as the resolved *English* text purely for backward-compat imports
# (existing tests) -- the actual reply language is resolved per-message.
CLARIFYING_QUESTION = i18n.t("clarifying_question", "en")
DEFERRED_ACK_MESSAGE = i18n.t("deferred_ack", "en")
NOTHING_TO_UNDO_MESSAGE = i18n.t("nothing_to_undo", "en")
NOTHING_TO_EDIT_MESSAGE = i18n.t("nothing_to_edit", "en")


def setup_logging(level: str) -> None:
    # Windows consoles / redirected files default to cp1252, which can't
    # encode the emoji used in reminders/confirmations. Force UTF-8 so
    # logging (and print()) never crashes on them, on any platform.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,
    )


async def handle_inbound_message(text, **kwargs):
    kwargs.setdefault("parse_message", parse_message)
    return await routing.handle_inbound_message(text, **kwargs)


async def reparse_pending_unparsed(db, llm, channel, config, registry=None, provider=None):
    return await routing.reparse_pending_unparsed(db, llm, channel, config, registry, provider, parse_message=parse_message)


async def async_main(args: argparse.Namespace) -> None:
    await _async_main(
        args,
        load_config=load_config,
        load_secrets=load_secrets,
        setup_logging=setup_logging,
        AsyncIOScheduler=AsyncIOScheduler,
        TelegramChannel=TelegramChannel,
        LineChannel=LineChannel,
        OllamaClient=OllamaClient,
        HealthMonitor=HealthMonitor,
        run_due_reminders=run_due_reminders,
        render_weekly_review_charts=render_weekly_review_charts,
        version=__version__,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="habit_assistant", description="Local habit-tracking assistant")
    parser.add_argument(
        "--test-reminder",
        metavar="CATEGORY",
        choices=sorted(_DEFAULT_REGISTRY.ids()),
        help="Fire one reminder immediately and exit",
    )
    parser.add_argument("--seed", action="store_true", help="Insert a few days of fake logs for weekly-review testing")
    parser.add_argument(
        "--dry-run",
        metavar="MESSAGE",
        default=None,
        help="Parse MESSAGE and print structured output without writing DB or sending a confirmation",
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Apply pending schema migrations and exit (prints from -> to version)",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Back up the DB to [backup].dir and exit",
    )
    parser.add_argument(
        "--restore",
        metavar="FILE",
        default=None,
        help="Restore the DB from FILE (destructive; requires --yes)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm a destructive operation (required with --restore)",
    )
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
