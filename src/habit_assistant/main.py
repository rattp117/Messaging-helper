"""Wiring: load config, start the scheduler + Telegram inbound loop.

Also the CLI entry point: --test-reminder, --seed, --dry-run (SPEC.md §10),
plus --migrate, --backup, --restore (ROADMAP.md v0.3.0), plus the health
monitor task (ROADMAP.md v0.4.0).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
from dataclasses import asdict
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.channels.base import Channel
from habit_assistant.channels.telegram import TelegramChannel
from habit_assistant.config import Config, ConfigError, load_config, load_secrets
from habit_assistant.core import commands
from habit_assistant.core.backup import BackupError
from habit_assistant.core.backup import backup as backup_db
from habit_assistant.core.backup import restore as restore_db
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.parser import parse_message
from habit_assistant.core.reminders import REMINDER_TEXTS, schedule_reminders, send_reminder
from habit_assistant.core.review import run_weekly_review
from habit_assistant.llm.ollama_client import OllamaClient
from habit_assistant.llm.prompts import DIARY_REFLECTION_SYSTEM_PROMPT, DIARY_REFLECTION_USER_TEMPLATE
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

logger = logging.getLogger("habit_assistant")

CLARIFYING_QUESTION = (
    "🤔 I couldn't quite tell what you meant — was that about water, a stretch "
    "break, or today's diary? Try something like '500ml water' or '10 min stretch'."
)

# ROADMAP.md v0.4.0 AC3.3: sent instead of the normal parse/confirm flow
# while the LLM is known DOWN (health_monitor.ollama_up is False) -- the
# message is persisted verbatim (category='unparsed') and re-parsed
# automatically once Ollama recovers (see reparse_pending_unparsed below).
DEFERRED_ACK_MESSAGE = (
    "⏳ Got it — I'll process this once the connection to the assistant is back."
)

# ROADMAP.md v0.5.0 AC5.2 / edit-with-nothing-to-edit: friendly, no-write
# responses when a command has nothing to act on. Bilingual-aware copy for
# every reply is v0.6.0's message catalog (ROADMAP.md §2) -- these stay
# English for now, per this version's explicit scope note.
NOTHING_TO_UNDO_MESSAGE = "🤷 Nothing to undo — you don't have any logged entries yet."
NOTHING_TO_EDIT_MESSAGE = "🤷 Nothing to edit — I couldn't find a matching entry to update."


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


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


def _describe_log(row) -> str:
    """Human-readable one-line summary of a log row, for undo's "confirm
    what was removed" (AC5.1)."""
    category = row["category"]
    if category == "water":
        return f"{row['value_num']:g} ml water"
    if category == "stretch":
        return f"{row['value_num']:g} min stretch"
    if category == "diary":
        text = row["value_text"] or ""
        snippet = text if len(text) <= 40 else text[:37] + "..."
        return f'diary entry: "{snippet}"'
    return f"{category} entry"


async def _execute_undo(db: Database, channel: Channel, config: Config, clock) -> None:
    """ROADMAP.md v0.5.0 AC5.1/AC5.2: soft-delete the most recent
    non-deleted log and confirm what was removed, with today's running
    total reflecting the removal. Nothing logged -> friendly message, no
    write (AC5.2)."""
    row = db.last_log()
    if row is None:
        await channel.send(NOTHING_TO_UNDO_MESSAGE)
        return

    db.soft_delete(row["id"])
    description = _describe_log(row)
    today_str = clock().date().isoformat()

    if row["category"] == "water":
        total = db.water_total_ml(today_str)
        goal = config.reminders.water.goal_ml
        pct = round(100 * total / goal) if goal else 0
        await channel.send(f"↩️ Undone — removed {description}. Today: {int(total)} / {goal} ml ({pct}%)")
    elif row["category"] == "stretch":
        count = db.stretch_count(today_str)
        await channel.send(f"↩️ Undone — removed {description}. {count} stretch session(s) today")
    else:
        await channel.send(f"↩️ Undone — removed {description}")


async def _execute_edit(
    db: Database, channel: Channel, config: Config, clock, command: commands.Command
) -> None:
    """ROADMAP.md v0.5.0 AC5.3: update the last matching (same-category)
    entry's value and re-confirm the new daily total. No matching entry ->
    friendly message, no write (mirrors AC5.2's undo-with-nothing shape)."""
    row = db.last_log(category=command.category)
    if row is None:
        await channel.send(NOTHING_TO_EDIT_MESSAGE)
        return

    db.update_value(row["id"], value_num=command.value_num)
    today_str = clock().date().isoformat()

    if command.category == "water":
        total = db.water_total_ml(today_str)
        goal = config.reminders.water.goal_ml
        pct = round(100 * total / goal) if goal else 0
        await channel.send(f"✏️ Updated to {command.value_num:g} ml — today {int(total)} / {goal} ml ({pct}%)")
    elif command.category == "stretch":
        count = db.stretch_count(today_str)
        await channel.send(f"✏️ Updated to {command.value_num:g} min stretch — {ordinal(count)} today")


async def handle_inbound_message(
    text: str,
    *,
    db: Database,
    llm: OllamaClient,
    channel: Channel | None,
    config: Config,
    source: str = "reply",
    clock=datetime.now,
    dry_run: bool = False,
    health_monitor: HealthMonitor | None = None,
) -> None:
    """Command dispatch -> (act + confirm) OR Parse -> validate -> (write
    row + confirm) OR (clarifying question). Confirmation formats are
    verbatim per SPEC.md §6.

    ROADMAP.md v0.5.0: every inbound message is checked against
    `core/commands.dispatch()` first -- a conservative, LLM-free router for
    explicit undo/edit commands (AC5.1, AC5.3). It never needs the LLM and
    is unaffected by Ollama's up/down state, so it runs even while the
    deferral path below would otherwise kick in. A message that isn't a
    recognized command (AC5.5: normal habit messages like "500ml") falls
    through to the parser exactly as before.

    ROADMAP.md v0.4.0 AC3.3: if `health_monitor` says Ollama is currently
    DOWN, skip calling the LLM entirely -- acknowledge the message and
    persist it verbatim as category='unparsed' (raw text kept). It gets
    re-parsed and confirmed automatically once Ollama recovers (see
    `reparse_pending_unparsed`, wired as `health_monitor`'s
    `on_ollama_recovered` callback in `async_main`, and also run once at
    startup to catch up on anything deferred by a previous process run)."""
    command = commands.dispatch(text, config.units.glass_ml, config.units.bottle_ml)
    if command is not None:
        if dry_run:
            print({"kind": command.kind, "category": command.category, "value_num": command.value_num})
            return
        assert channel is not None, "channel is required outside dry-run"
        if command.kind == "undo":
            await _execute_undo(db, channel, config, clock)
        else:
            await _execute_edit(db, channel, config, clock, command)
        return

    if not dry_run and health_monitor is not None and not health_monitor.ollama_up:
        assert channel is not None, "channel is required outside dry-run"
        now = clock()
        ts = now.isoformat(timespec="seconds")
        db.insert_log(LogEntry(None, ts, "unparsed", None, None, text, source))
        await channel.send(DEFERRED_ACK_MESSAGE)
        return

    result = await parse_message(
        text, llm, config.units.glass_ml, config.units.bottle_ml, config.ollama.confidence_threshold
    )

    if dry_run:
        print(asdict(result))
        return

    assert channel is not None, "channel is required outside dry-run"

    now = clock()
    ts = now.isoformat(timespec="seconds")
    today_str = now.date().isoformat()

    if result.category == "unknown":
        await channel.send(CLARIFYING_QUESTION)
        return

    if result.category == "water":
        entry = LogEntry(None, ts, "water", float(result.water_ml), None, text, source)
        db.insert_log(entry)
        total = db.water_total_ml(today_str)
        goal = config.reminders.water.goal_ml
        pct = round(100 * total / goal) if goal else 0
        await channel.send(f"✅ {result.water_ml} ml logged — today {int(total)} / {goal} ml ({pct}%)")
        return

    if result.category == "stretch":
        entry = LogEntry(None, ts, "stretch", float(result.stretch_min), None, text, source)
        db.insert_log(entry)
        count = db.stretch_count(today_str)
        await channel.send(f"✅ {result.stretch_min} min stretch logged — {ordinal(count)} today")
        return

    if result.category == "diary":
        entry = LogEntry(None, ts, "diary", None, result.diary_text, text, source)
        db.insert_log(entry)
        reflection = await llm.chat_text(
            DIARY_REFLECTION_SYSTEM_PROMPT,
            DIARY_REFLECTION_USER_TEMPLATE.format(diary_text=result.diary_text),
        )
        if not reflection:
            reflection = "Thanks for sharing — noted."
        await channel.send(f"✅ Saved. {reflection}")
        return


async def reparse_pending_unparsed(
    db: Database, llm: OllamaClient, channel: Channel, config: Config
) -> None:
    """ROADMAP.md v0.4.0 AC3.3 recovery path: re-parse every row deferred
    while Ollama was DOWN (category='unparsed'), convert it to its real
    category, and confirm. Rows come straight from `db.pending_unparsed()`
    -- a plain query against persisted state, not an in-memory queue --
    so this also picks up rows deferred by a *previous* process run. Two
    call sites in `async_main`: once at startup (catches up any backlog
    left over from before a restart) and once per DOWN->UP transition via
    `health_monitor`'s `on_ollama_recovered` callback.

    A row that's still unparseable after Ollama is back (genuinely bad
    input, not an outage) is left as 'unparsed' and logged -- it is not
    retried again until the next DOWN->UP transition."""
    pending = db.pending_unparsed()
    if not pending:
        return

    logger.info("Re-parsing %d deferred message(s)", len(pending))
    for row in pending:
        text = row["raw_message"]
        result = await parse_message(
            text, llm, config.units.glass_ml, config.units.bottle_ml, config.ollama.confidence_threshold
        )

        if result.category == "water":
            db.reclassify_log(row["id"], "water", float(result.water_ml), None)
            await channel.send(f"🔁 Recovered: {result.water_ml} ml logged from your earlier message.")
        elif result.category == "stretch":
            db.reclassify_log(row["id"], "stretch", float(result.stretch_min), None)
            await channel.send(
                f"🔁 Recovered: {result.stretch_min} min stretch logged from your earlier message."
            )
        elif result.category == "diary":
            db.reclassify_log(row["id"], "diary", None, result.diary_text)
            await channel.send("🔁 Recovered: saved your earlier diary message.")
        else:
            logger.warning(
                "Deferred message id=%s still unparseable after Ollama recovery; left as 'unparsed': %r",
                row["id"],
                text,
            )


def seed_fake_data(db: Database, config: Config) -> None:
    """Insert a few days of plausible fake logs so --seed lets the weekly
    review be exercised fully offline."""
    now = datetime.now()
    rng = random.Random(42)
    for offset in range(6, -1, -1):
        day = now - timedelta(days=offset)
        for _ in range(rng.randint(2, 6)):
            ml = rng.choice([250, 300, 500, 600])
            ts = day.replace(hour=rng.randint(8, 20), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(LogEntry(None, ts.isoformat(timespec="seconds"), "water", float(ml), None, f"seed {ml}ml water", "reply"))
        if rng.random() > 0.3:
            minutes = rng.choice([5, 10, 15])
            ts = day.replace(hour=rng.randint(11, 17), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(LogEntry(None, ts.isoformat(timespec="seconds"), "stretch", float(minutes), None, f"seed {minutes} min stretch", "reply"))
        if rng.random() > 0.4:
            ts = day.replace(hour=21, minute=30, second=0, microsecond=0)
            db.insert_log(LogEntry(None, ts.isoformat(timespec="seconds"), "diary", None, "seed diary entry", "seed diary entry", "reply"))
    logger.info("Seeded fake data for the last 7 days into %s", db.db_path)


async def async_main(args: argparse.Namespace) -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.app.log_level)

    # --seed and --dry-run don't need Telegram credentials.
    if args.seed:
        db = Database(config.app.db_path)
        seed_fake_data(db, config)
        db.close()
        return

    if args.dry_run is not None:
        db = Database(config.app.db_path)
        llm = OllamaClient(config.ollama.base_url, config.ollama.model_chain, config.ollama.timeout_seconds)
        await handle_inbound_message(args.dry_run, db=db, llm=llm, channel=None, config=config, dry_run=True)
        await llm.aclose()
        db.close()
        return

    # --migrate / --backup / --restore are dev/ops affordances -- like
    # --seed and --dry-run, they never touch Telegram (ROADMAP v0.3.0).
    # getattr(...) defaults keep this compatible with hand-built
    # argparse.Namespace-alikes (pre-existing test fixtures) that predate
    # these flags and don't set them.
    if getattr(args, "migrate", False):
        db = Database(config.app.db_path)  # Database.__init__ runs the migration runner
        print(f"Migrated schema {db.schema_version_before} -> {db.schema_version}")
        db.close()
        return

    if getattr(args, "backup", False):
        try:
            dest = backup_db(config.app.db_path, config.backup.dir, retain=config.backup.retain)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Backup written: {dest}")
        return

    restore_arg = getattr(args, "restore", None)
    if restore_arg:
        if not getattr(args, "yes", False):
            print(
                "ERROR: --restore is destructive (it replaces the live DB). "
                "Re-run with --yes to confirm.",
                file=sys.stderr,
            )
            sys.exit(1)
        try:
            restored = restore_db(restore_arg, config.app.db_path, config.backup.dir, retain=config.backup.retain)
        except BackupError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        print(f"Restored {restore_arg} -> {restored}")
        return

    try:
        secrets = load_secrets()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    db = Database(config.app.db_path)
    llm = OllamaClient(
        config.ollama.base_url,
        config.ollama.model_chain,
        config.ollama.timeout_seconds,
        retry_attempts=config.ollama.retry_attempts,
        retry_backoff_seconds=config.ollama.retry_backoff_seconds,
    )
    channel = TelegramChannel(
        secrets.telegram_bot_token,
        secrets.telegram_chat_id,
        config.telegram.poll_timeout,
        backoff_initial_seconds=config.telegram.backoff_initial_seconds,
        backoff_max_seconds=config.telegram.backoff_max_seconds,
    )

    if args.test_reminder:
        try:
            await send_reminder(channel, args.test_reminder)
        except httpx.HTTPError as exc:
            print(f"ERROR: Failed to send test reminder: {exc}", file=sys.stderr)
            await channel.aclose()
            await llm.aclose()
            db.close()
            sys.exit(1)
        await channel.aclose()
        await llm.aclose()
        db.close()
        return

    # AC2.1: probe each configured model's schema conformance once at
    # startup, purely for operator visibility (logged inside the client).
    # Never allowed to crash startup -- probe_schema_support() itself never
    # raises, but this is belt-and-suspenders against a future change to it.
    try:
        await llm.probe_schema_support()
    except Exception:
        logger.exception("Ollama schema conformance probe failed unexpectedly; continuing startup anyway")

    # ROADMAP.md v0.4.0 AC3.3: catch up on anything deferred by a
    # *previous* process run before entering the main loop. If Ollama is
    # still down right now, parse_message just fails closed per row (no
    # change, no confirmation) and they stay 'unparsed' for the health
    # monitor's own DOWN->UP callback to pick up later -- never raises.
    try:
        await reparse_pending_unparsed(db, llm, channel, config)
    except Exception:
        logger.exception("Startup re-parse of deferred messages failed unexpectedly; continuing")

    async def on_ollama_recovered() -> None:
        await reparse_pending_unparsed(db, llm, channel, config)

    health_monitor = HealthMonitor(
        config.ollama.base_url,
        secrets.telegram_bot_token,
        interval_seconds=config.health.interval_seconds,
        channel=channel,
        on_ollama_recovered=on_ollama_recovered,
    )

    scheduler = AsyncIOScheduler()
    schedule_reminders(scheduler, channel, config)

    review_hour, review_minute = (int(x) for x in config.weekly_review.time.split(":"))

    async def weekly_review_job() -> None:
        text = await run_weekly_review(db, config, llm)
        await channel.send(text)

    scheduler.add_job(
        weekly_review_job,
        trigger=CronTrigger(
            day_of_week=config.weekly_review.day_of_week,
            hour=review_hour,
            minute=review_minute,
            timezone=config.app.timezone,
        ),
        id="weekly_review",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started; entering Telegram long-poll loop")

    async def on_message(text: str) -> None:
        await handle_inbound_message(
            text, db=db, llm=llm, channel=channel, config=config, health_monitor=health_monitor
        )

    # ROADMAP.md v0.4.0 scope item 5: the health monitor runs as its own
    # asyncio task alongside the scheduler and the inbound loop.
    health_task = asyncio.create_task(health_monitor.run())
    try:
        await channel.run(on_message)
    finally:
        health_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await health_task
        await health_monitor.aclose()
        scheduler.shutdown(wait=False)
        await channel.aclose()
        await llm.aclose()
        db.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="habit_assistant", description="Local habit-tracking assistant")
    parser.add_argument(
        "--test-reminder",
        metavar="CATEGORY",
        choices=sorted(REMINDER_TEXTS),
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
