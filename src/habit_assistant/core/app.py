"""SPEC-REFACTOR.md Stage 2 (rule 9): bootstrap/wiring, split out of
`main.py:async_main` -- config/secrets/db/llm/channel/provider wiring, the
CLI-adjacent branches (`--seed`/`--dry-run`/`--migrate`/`--backup`/
`--restore`/`--test-reminder`), startup announce/command-menu registration,
job registration (delegating each job's actual body to `core/jobs.py`), and
the Telegram long-poll loop (delegating to `core/routing.py`).

`load_config`/`load_secrets`/`setup_logging`/`AsyncIOScheduler`/
`TelegramChannel`/`OllamaClient`/`HealthMonitor`/`run_due_reminders`/
`render_weekly_review_charts`/`version` are explicit keyword parameters
(not module-level imports bound here) precisely so `main.py`'s own
re-export of `async_main` can forward its OWN current module-level names
for each -- see that module for why: several tests monkeypatch these names
ON `habit_assistant.main` and expect `async_main` to see the patched
value, which only works if the code that uses them reads them from
`main.py`'s namespace at call time rather than this module's.

`on_message`/`on_callback` and each scheduler job are registered here as
small zero-arg forwarding closures over `core/routing.py`/`core/jobs.py`'s
real, parameter-taking implementations -- the only closures left in this
file, carrying no logic of their own (OQ3).

SPEC-LINE.md §4 R-I1-R-I5 (Integration pass, branch `line-version`): this
is the ONE central file §11 reserves for wiring the LINE channel in --
every LINE module (A/B/C/D) built its own piece in isolation and left this
file untouched. `LineChannel` is now a fourth explicit, keyword-only
constructor parameter, exactly mirroring `TelegramChannel`'s own shape (so
a test can inject a fake/instrumented class the same way existing tests
already do for `TelegramChannel`/`OllamaClient`/`HealthMonitor`) --
`config.channel.type` picks which one `async_main` actually constructs
(R-I1); the Telegram branch is untouched, byte-for-byte, so `type=
"telegram"` (the default, and every pre-LINE `config.toml`) reaches the
exact same code it always did (AC28). `config.ollama.enabled == False`
(this branch's own only-supported mode, but checked generically, not
keyed off channel type, since the two are independent config knobs)
short-circuits `OllamaClient` construction and the startup schema probe
(R-B7/R-B9); `llm` then threads through as `None` to every downstream
call site, all of which already guard on `config.ollama.enabled`
themselves (module B) before ever touching it -- see `_owner_chat_id`'s
own docstring for the parallel `channel.type`-keyed owner-id resolution
this same pass threads through the three existing `load_secrets()` call
sites (R-I4)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import random
import sys
from datetime import datetime, timedelta

import httpx
from apscheduler.triggers.cron import CronTrigger

from habit_assistant.config import Config, ConfigError, Secrets
from habit_assistant.core import announce, digest, i18n, jobs, routing, undo_ui
from habit_assistant.core.backup import BackupError
from habit_assistant.core.backup import backup as backup_db
from habit_assistant.core.backup import restore as restore_db
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState, send_reminder
from habit_assistant.llm.ollama_client import build_extraction_schema
from habit_assistant.llm.prompts import build_extraction_system_prompt, build_extraction_user_prompt
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

logger = logging.getLogger(__name__)


def _owner_chat_id(config: Config, secrets: Secrets) -> str:
    """SPEC-LINE.md §4 R-I4: the id every owner-only surface (`/audit`,
    `/users`, admin commands, the digest quota warning) and startup's own
    `attribute_legacy_to_owner` key off -- `LINE_OWNER_USER_ID` when
    `config.channel.type=="line"`, else the pre-LINE `secrets.
    telegram_chat_id` (byte-identical for every existing Telegram install,
    AC28 -- `config.channel.type` defaults to `"telegram"`, so an
    unconfigured `config.toml` resolves exactly as it always did)."""
    return secrets.line_owner_user_id if config.channel.type == "line" else secrets.telegram_chat_id


def _load_secrets_for_channel(load_secrets, config: Config) -> Secrets:
    """SPEC-LINE.md §4 R-I1/AC1: validates the SELECTED channel's secrets
    (`config.py:load_secrets`'s own new `channel_type` param, shared
    surface). Calls `load_secrets()` completely bare -- no kwarg at all --
    for the Telegram default, byte-identical to every pre-LINE call site
    (AC28): roughly two dozen existing tests across this suite monkeypatch
    `main_module.load_secrets` with a zero-argument fake
    (`lambda: SimpleNamespace(...)`), and none of them need to know this
    branch exists. `channel_type="line"` is only ever passed when
    `config.channel.type=="line"`, a shape unreachable outside this
    branch's own tests."""
    if config.channel.type == "line":
        return load_secrets(channel_type="line")
    return load_secrets()


# Bot command menu copy -- used only by this module's own `set_my_commands`
# registration below. See `main.py`'s pre-Stage-2 history for the per-entry
# spec citations (SPEC-v1.1.md through SPEC-v1.9.md integration steps);
# kept verbatim here, just relocated.
TARGET_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "View or set a habit's daily goal",
    "th": "ดูหรือตั้งเป้าหมายรายวันของกิจกรรม",
}
DISCOVERABILITY_COMMAND_DESCRIPTIONS: dict[i18n.Language, list[tuple[str, str]]] = {
    "en": [("help", "Show what I can do"), ("habits", "List your habits and today's progress")],
    "th": [("help", "ดูสิ่งที่ฉันทำได้"), ("habits", "ดูรายการกิจกรรมและความคืบหน้าวันนี้")],
}
START_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Get started",
    "th": "เริ่มต้นใช้งาน",
}
LANG_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Set your reply language (en/th/auto)",
    "th": "ตั้งภาษาที่ใช้ตอบ (en/th/auto)",
}
QUIET_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Set your quiet hours (no reminders sent)",
    "th": "ตั้งช่วงเวลางดแจ้งเตือนของคุณ",
}
CHECKIN_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Get hourly check-in nudges (off by default)",
    "th": "เปิดแจ้งเตือนเช็คอินรายชั่วโมง (ปิดโดยค่าเริ่มต้น)",
}
REMIND_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "View or set your reminder times for a habit",
    "th": "ดูหรือตั้งเวลาแจ้งเตือนของกิจกรรม",
}
HISTORY_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Show your recent entries (including undone ones)",
    "th": "ดูรายการล่าสุดของคุณ (รวมรายการที่ยกเลิกแล้ว)",
}
DASHBOARD_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Pin a live \"Today\" board that updates as you log",
    "th": "ปักหมุดบอร์ด \"วันนี้\" แบบสดที่อัปเดตเมื่อคุณบันทึก",
}
HEATMAP_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "See a consistency calendar picture of your habits",
    "th": "ดูภาพปฏิทินความสม่ำเสมอของกิจกรรมคุณ",
}
RECORDS_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "See your personal bests (day, week, streak)",
    "th": "ดูสถิติส่วนตัวของคุณ (วัน สัปดาห์ สตรีค)",
}
TRENDS_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "See this week vs last week, at a glance",
    "th": "ดูเปรียบเทียบสัปดาห์นี้กับสัปดาห์ที่แล้วแบบย่อ",
}
ADDHABIT_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Add your own custom habit",
    "th": "เพิ่มนิสัยของคุณเอง",
}
DELHABIT_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Remove a habit you created",
    "th": "ลบนิสัยที่คุณสร้างไว้",
}
LOG_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Tap to log a habit instantly",
    "th": "แตะเพื่อบันทึกกิจกรรมทันที",
}
ROUTINE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Create or run a bundle of habits at once",
    "th": "สร้างหรือรันชุดกิจกรรมพร้อมกัน",
}
INVITE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Invite someone to use this bot",
    "th": "เชิญคนอื่นมาใช้บอทนี้",
}
APPROVE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Approve a pending user",
    "th": "อนุมัติผู้ใช้ที่รอการอนุมัติ",
}
BLOCK_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Block a user",
    "th": "บล็อกผู้ใช้",
}
USERS_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "List all users and their status",
    "th": "ดูรายชื่อผู้ใช้ทั้งหมดและสถานะ",
}
AUDIT_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "View the audit log of account changes",
    "th": "ดูประวัติการเปลี่ยนแปลงบัญชี",
}
CADENCE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Set a weekly goal for a habit (e.g. gym 3x/week)",
    "th": "ตั้งเป้าหมายรายสัปดาห์ให้กิจกรรม (เช่น ยิม 3 ครั้ง/สัปดาห์)",
}
PAUSE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Pause a habit (or everything) for a planned break",
    "th": "พักกิจกรรม (หรือทุกอย่าง) ระหว่างที่คุณไม่สะดวก",
}
RESUME_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "End a pause early",
    "th": "กลับมาก่อนกำหนด",
}
WRAPPED_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "Get a shareable picture recap of your recent progress",
    "th": "รับการ์ดสรุปความคืบหน้าล่าสุดของคุณ",
}
# SPEC-v1.10.md §4 R17 (integration pass): public menu 22 -> 23, owner menu
# (a strict superset) 27 -> 28. Inserted after `wrapped` per R17's own
# placement rule.
GUIDE_COMMAND_DESCRIPTIONS: dict[i18n.Language, str] = {
    "en": "A 20-second orientation to get you started",
    "th": "คู่มือเริ่มต้นใช้งานฉบับ 20 วินาที",
}


def seed_fake_data(db: Database, config, user_id: str) -> None:
    """Insert a few days of plausible fake logs so --seed lets the weekly
    review be exercised fully offline."""
    now = datetime.now()
    rng = random.Random(42)
    for offset in range(6, -1, -1):
        day = now - timedelta(days=offset)
        for _ in range(rng.randint(2, 6)):
            ml = rng.choice([250, 300, 500, 600])
            ts = day.replace(hour=rng.randint(8, 20), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(
                LogEntry(None, user_id, ts.isoformat(timespec="seconds"), "water", float(ml), None, f"seed {ml}ml water", "reply")
            )
        if rng.random() > 0.3:
            minutes = rng.choice([5, 10, 15])
            ts = day.replace(hour=rng.randint(11, 17), minute=rng.randint(0, 59), second=0, microsecond=0)
            db.insert_log(
                LogEntry(
                    None, user_id, ts.isoformat(timespec="seconds"), "stretch", float(minutes), None,
                    f"seed {minutes} min stretch", "reply",
                )
            )
        if rng.random() > 0.4:
            ts = day.replace(hour=21, minute=30, second=0, microsecond=0)
            db.insert_log(
                LogEntry(
                    None, user_id, ts.isoformat(timespec="seconds"), "diary", None, "seed diary entry",
                    "seed diary entry", "reply",
                )
            )
    logger.info("Seeded fake data for the last 7 days into %s", db.db_path)


async def async_main(
    args: argparse.Namespace,
    *,
    load_config,
    load_secrets,
    setup_logging,
    AsyncIOScheduler,
    TelegramChannel,
    LineChannel=None,
    OllamaClient,
    HealthMonitor,
    run_due_reminders,
    render_weekly_review_charts,
    version: str,
) -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(config.app.log_level)

    registry = HabitRegistry.from_config(config)

    # --seed and --dry-run attribute to the OWNER (never leave a NULL-user_id
    # row) -- the owner id only exists in `.env`, so both load secrets too.
    if args.seed:
        try:
            secrets = _load_secrets_for_channel(load_secrets, config)
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        db = Database(config.app.db_path)
        owner_id = _owner_chat_id(config, secrets)
        db.attribute_legacy_to_owner(owner_id)
        seed_fake_data(db, config, owner_id)
        db.close()
        return

    if args.dry_run is not None:
        try:
            secrets = _load_secrets_for_channel(load_secrets, config)
        except ConfigError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
        db = Database(config.app.db_path)
        owner_id = _owner_chat_id(config, secrets)
        db.attribute_legacy_to_owner(owner_id)
        llm = OllamaClient(config.ollama.base_url, config.ollama.model_chain, config.ollama.timeout_seconds)
        await routing.handle_inbound_message(
            args.dry_run,
            db=db,
            llm=llm,
            channel=None,
            config=config,
            dry_run=True,
            registry=registry,
            user_id=owner_id,
        )
        await llm.aclose()
        db.close()
        return

    # --migrate / --backup / --restore are dev/ops affordances -- like
    # --seed and --dry-run, they never touch Telegram. getattr(...)
    # defaults keep this compatible with hand-built argparse.Namespace-
    # alikes that predate these flags.
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
        secrets = _load_secrets_for_channel(load_secrets, config)
    except ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    db = Database(config.app.db_path)
    # The ONE process-global per-user registry cache, threaded through
    # every consumer below in place of the single global `registry` above
    # (kept as-is -- still used for the startup schema-conformance probe
    # and as every fan-out function's own fallback-only positional).
    provider = RegistryProvider(config, db)
    owner_id = _owner_chat_id(config, secrets)
    # Startup attribution, called ONCE right after `load_secrets` -- the
    # owner id only lives in `.env`. Idempotent: safe to call on every
    # startup unconditionally. SPEC-LINE.md §4 R-I4: `owner_id` resolves to
    # `LINE_OWNER_USER_ID` on this branch when `config.channel.type=="line"`.
    db.attribute_legacy_to_owner(owner_id)
    # Retention, run ONCE at startup (cheap housekeeping, not per-insert) --
    # `retention_days = 0` means "keep forever".
    if config.audit.retention_days > 0:
        cutoff = (datetime.now() - timedelta(days=config.audit.retention_days)).isoformat(timespec="seconds")
        pruned = db.prune_audit(cutoff)
        if pruned:
            logger.info(
                "Pruned %d audit_log row(s) older than %s (retention_days=%d)", pruned, cutoff, config.audit.retention_days
            )
    # SPEC-LINE.md §4 R-B9/§5.2 row 8 (branch `line-version`): no
    # `OllamaClient` at all when `ollama.enabled` is False -- not even a
    # stub -- so there is structurally nothing to accidentally call.
    # `llm` threads through as `None` to every downstream site below (the
    # long-poll loop, the digest job, the recovery sweep); each one already
    # gates on `config.ollama.enabled` itself (module B) before ever
    # touching it, so `None` is safe by construction, not by luck.
    llm = (
        OllamaClient(
            config.ollama.base_url,
            config.ollama.model_chain,
            config.ollama.timeout_seconds,
            retry_attempts=config.ollama.retry_attempts,
            retry_backoff_seconds=config.ollama.retry_backoff_seconds,
        )
        if config.ollama.enabled
        else None
    )
    # SPEC-LINE.md §4 R-I1: channel selection by `config.channel.type` --
    # the Telegram branch is untouched, byte-for-byte (AC28). `LineChannel`
    # is a required constructor for the "line" branch (a caller that
    # configures `type="line"` without supplying one is a wiring bug, not a
    # runtime case to silently degrade); the default `None` on the
    # parameter itself exists purely so a Telegram-only caller/test isn't
    # forced to pass an unused class.
    if config.channel.type == "line":
        channel = LineChannel(
            secrets.line_channel_access_token,
            secrets.line_channel_secret,
            secrets.line_owner_user_id,
            config,
            db,
        )
    else:
        channel = TelegramChannel(
            secrets.telegram_bot_token,
            secrets.telegram_chat_id,
            config.telegram.poll_timeout,
            backoff_initial_seconds=config.telegram.backoff_initial_seconds,
            backoff_max_seconds=config.telegram.backoff_max_seconds,
        )

    # One `ReminderState` for the process lifetime, updated by every
    # `send_reminder` that actually fires and read by `_execute_snooze` to
    # resolve a bare "snooze"/"เลื่อน" command to a habit.
    reminder_state = ReminderState()

    if args.test_reminder:
        habit = registry.get(args.test_reminder)
        if habit is None:
            print(f"ERROR: {args.test_reminder!r} is not a configured habit", file=sys.stderr)
            await channel.aclose()
            if llm is not None:
                await llm.aclose()
            db.close()
            sys.exit(1)
        try:
            await send_reminder(
                channel, owner_id, habit, i18n.resolve_unprompted_language(config), db, config, reminder_state
            )
        except httpx.HTTPError as exc:
            print(f"ERROR: Failed to send test reminder: {exc}", file=sys.stderr)
            await channel.aclose()
            if llm is not None:
                await llm.aclose()
            db.close()
            sys.exit(1)
        await channel.aclose()
        if llm is not None:
            await llm.aclose()
        db.close()
        return

    # Build the generic extraction schema and the registry-driven
    # system/user prompt, then probe each configured model's schema
    # conformance once at startup, purely for operator visibility. Never
    # allowed to crash startup.
    # SPEC-LINE.md §4 R-B7/§5.2 row 7: the probe never runs at all when
    # `ollama.enabled` is False -- there is no client to probe with.
    if config.ollama.enabled and config.ollama.probe_on_startup:
        extraction_schema = build_extraction_schema(registry.category_enum())
        probe_system_prompt = build_extraction_system_prompt(registry)
        probe_user_prompt = build_extraction_user_prompt("500ml")
        try:
            await llm.probe_schema_support(probe_system_prompt, probe_user_prompt, extraction_schema)
        except Exception:
            logger.exception("Ollama schema conformance probe failed unexpectedly; continuing startup anyway")

    # The running version's release note, sent once per user per version,
    # fanned out to every active user. Deliberately NOT reached by the
    # --seed/--dry-run/--test-reminder CLI branches above (all of which
    # already returned by this point).
    #
    # SPEC-LINE.md §4 R-C2 (branch `line-version`): this is a proactive
    # PUSH send (no reply context ever active at startup), and it is fired
    # directly from here rather than through `core/jobs.py` -- the ONE
    # proactive-send call site R-C2's own suppression list doesn't cover,
    # since module C only gates functions living in `jobs.py`. Skipped
    # entirely on LINE: the release note is instead folded into the daily
    # digest (R-C1 item (e), `core/digest.py`'s own `_pending_announcement_
    # version` read of this SAME `RELEASE_NOTES`/`last_announced_version`
    # state), so nothing here needs a channel-type branch beyond this one
    # early skip -- the Telegram path is unaffected (AC28).
    if config.channel.type != "line":
        await announce.announce_release(db, channel, config, version)

    # Register the bot command menu once at startup -- 23 public commands
    # (SPEC-v1.10.md §4 R17: 22 -> 23, `/guide` appended after `/wrapped`).
    undo_command_menu = undo_ui.command_menu_entries()
    command_menu = {
        lang: (
            [("start", START_COMMAND_DESCRIPTIONS[lang])]
            + undo_command_menu[lang]
            + [("target", desc)]
            + DISCOVERABILITY_COMMAND_DESCRIPTIONS[lang]
            + [("remind", REMIND_COMMAND_DESCRIPTIONS[lang])]
            + [("lang", LANG_COMMAND_DESCRIPTIONS[lang])]
            + [("quiet", QUIET_COMMAND_DESCRIPTIONS[lang])]
            + [("history", HISTORY_COMMAND_DESCRIPTIONS[lang])]
            + [("checkin", CHECKIN_COMMAND_DESCRIPTIONS[lang])]
            + [("dashboard", DASHBOARD_COMMAND_DESCRIPTIONS[lang])]
            + [("heatmap", HEATMAP_COMMAND_DESCRIPTIONS[lang])]
            + [("records", RECORDS_COMMAND_DESCRIPTIONS[lang])]
            + [("trends", TRENDS_COMMAND_DESCRIPTIONS[lang])]
            + [("addhabit", ADDHABIT_COMMAND_DESCRIPTIONS[lang])]
            + [("delhabit", DELHABIT_COMMAND_DESCRIPTIONS[lang])]
            + [("log", LOG_COMMAND_DESCRIPTIONS[lang])]
            + [("routine", ROUTINE_COMMAND_DESCRIPTIONS[lang])]
            + [("cadence", CADENCE_COMMAND_DESCRIPTIONS[lang])]
            + [("pause", PAUSE_COMMAND_DESCRIPTIONS[lang])]
            + [("resume", RESUME_COMMAND_DESCRIPTIONS[lang])]
            + [("wrapped", WRAPPED_COMMAND_DESCRIPTIONS[lang])]
            + [("guide", GUIDE_COMMAND_DESCRIPTIONS[lang])]
        )
        for lang, desc in TARGET_COMMAND_DESCRIPTIONS.items()
    }
    try:
        await channel.set_my_commands(command_menu)
    except Exception:
        logger.exception("set_my_commands failed at startup; continuing")

    # A SECOND menu, scoped to just the owner's own chat, additionally
    # listing the five true admin commands -- built by extending the SAME
    # public list, so it's always a strict superset of the public one.
    owner_command_menu = {
        lang: (
            entries
            + [("invite", INVITE_COMMAND_DESCRIPTIONS[lang])]
            + [("approve", APPROVE_COMMAND_DESCRIPTIONS[lang])]
            + [("block", BLOCK_COMMAND_DESCRIPTIONS[lang])]
            + [("users", USERS_COMMAND_DESCRIPTIONS[lang])]
            + [("audit", AUDIT_COMMAND_DESCRIPTIONS[lang])]
        )
        for lang, entries in command_menu.items()
    }
    try:
        await channel.set_my_commands(owner_command_menu, scope_chat_id=owner_id)
    except Exception:
        logger.exception("set_my_commands (owner-scoped) failed at startup; continuing")

    # Catch up on anything deferred by a *previous* process run before
    # entering the main loop. Already self-guarded on `config.ollama.
    # enabled` inside `reparse_pending_unparsed` (module B) -- safe to call
    # unconditionally with `llm=None`.
    try:
        await routing.reparse_pending_unparsed(db, llm, channel, config, registry, provider=provider)
    except Exception:
        logger.exception("Startup re-parse of deferred messages failed unexpectedly; continuing")

    async def on_ollama_recovered() -> None:
        await routing.reparse_pending_unparsed(db, llm, channel, config, registry, provider=provider)

    # SPEC-LINE.md §4 R-B8/§5.2 row 8 (branch `line-version`): the monitor
    # is not wired at all on LINE -- not just its Ollama half. Its
    # Telegram half (`check_telegram`, a `getMe` liveness ping) has no
    # LINE equivalent and needs `secrets.telegram_bot_token`, which is
    # `None` on a LINE deployment; connectivity is implicit in webhook
    # delivery instead (AC28's own "does not construct ... the health
    # monitor" for `type=="line"`). `ollama_enabled=config.ollama.enabled`
    # covers the OTHER, independent axis (a hypothetical no-LLM Telegram
    # install) -- module B's own `HealthMonitor.check_ollama` already
    # no-ops when this is False.
    health_monitor = None
    if config.channel.type == "telegram":
        health_monitor = HealthMonitor(
            config.ollama.base_url,
            secrets.telegram_bot_token,
            secrets.telegram_chat_id,
            interval_seconds=config.health.interval_seconds,
            channel=channel,
            on_ollama_recovered=on_ollama_recovered,
            language=i18n.resolve_unprompted_language(config),
            ollama_enabled=config.ollama.enabled,
        )

    # SPEC-REFACTOR.md Stage 1 rule 2/AC4: a single minutely tick
    # (`core/jobs.py:minutely_tick`) replaces the three independent
    # `reminder_tick`/`checkin_tick`/`nudge_tick` jobs.
    async def _minutely_tick() -> None:
        await jobs.minutely_tick(
            channel, config, registry, db, reminder_state, provider, run_due_reminders=run_due_reminders
        )

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _minutely_tick,
        trigger=CronTrigger(second=0, timezone=config.app.timezone),
        id="minutely_tick",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    async def _dashboard_day_rollover_job() -> None:
        await jobs.dashboard_day_rollover_job(db, channel, config, provider)

    scheduler.add_job(
        _dashboard_day_rollover_job,
        trigger=CronTrigger(hour=0, minute=0, timezone=config.app.timezone),
        id="dashboard_day_rollover",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    review_hour, review_minute = (int(x) for x in config.weekly_review.time.split(":"))

    async def _weekly_review_job() -> None:
        await jobs.weekly_review_job(
            db, channel, config, provider, llm, render_weekly_review_charts=render_weekly_review_charts
        )

    scheduler.add_job(
        _weekly_review_job,
        trigger=CronTrigger(
            day_of_week=config.weekly_review.day_of_week,
            hour=review_hour,
            minute=review_minute,
            timezone=config.app.timezone,
        ),
        id="weekly_review",
        replace_existing=True,
    )

    summary_hour, summary_minute = (int(x) for x in config.gamification.daily_summary_time.split(":"))

    async def _daily_summary_job() -> None:
        await jobs.daily_summary_job(db, channel, config, provider)

    scheduler.add_job(
        _daily_summary_job,
        trigger=CronTrigger(hour=summary_hour, minute=summary_minute, timezone=config.app.timezone),
        id="daily_summary",
        replace_existing=True,
    )

    async def _grace_tick() -> None:
        await jobs.grace_tick(db, channel, config, provider)

    scheduler.add_job(
        _grace_tick,
        trigger=CronTrigger(hour=0, minute=5, timezone=config.app.timezone),
        id="grace_tick",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=30,
    )

    async def _wrapped_auto_job() -> None:
        await jobs.wrapped_auto_job(db, channel, config, provider)

    scheduler.add_job(
        _wrapped_auto_job,
        trigger=CronTrigger(day="last", hour=21, minute=30, timezone=config.app.timezone),
        id="wrapped_auto",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )

    # SPEC-LINE.md §4 R-C1/R-I2 (branch `line-version`): the one daily
    # digest push, registered ONLY on LINE -- on Telegram this would be a
    # brand-new proactive send with no pre-LINE equivalent, which AC28's
    # "the Telegram path is byte-unchanged" forbids adding. The other six
    # jobs just above stay registered unconditionally on BOTH channels
    # (R-I2's own "or run in their suppressed form" alternative) -- each
    # already self-gates on `config.channel.type == "line"` inside
    # `core/jobs.py` (module C), so leaving their registration untouched
    # here is the smallest-diff way to satisfy R-I2 without a second,
    # parallel wiring path. `scheduler=scheduler` is what lets `run_daily_
    # digest` defer a send past a user's own quiet-hours window (see that
    # function's own docstring for the ARCHI RULING this implements, and
    # `deploy/habit-assistant-line.service`'s own comment for the single-
    # instance assumption the in-memory once-per-day guard relies on).
    if config.channel.type == "line":
        digest_hour, digest_minute = (int(x) for x in config.digest.time.split(":"))

        async def _digest_job() -> None:
            await digest.run_daily_digest(db, channel, config, provider, scheduler=scheduler)

        scheduler.add_job(
            _digest_job,
            trigger=CronTrigger(hour=digest_hour, minute=digest_minute, timezone=config.app.timezone),
            id="daily_digest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=30,
        )

    scheduler.start()

    # SPEC-LINE.md §4 R-A10/R-I3 (branch `line-version`): the one static
    # rich menu, registered once at startup, fail-open (`register_rich_
    # menu` itself never raises -- module A's own contract, AC14) --
    # called here, right before the app enters its serve-forever loop
    # (`channel.run(...)` below never returns control until shutdown, so
    # "after the app is up" means "after all one-time startup wiring is
    # done", the same point every other startup-only step above already
    # runs at).
    if config.channel.type == "line":
        try:
            await channel.register_rich_menu()
        except Exception:
            logger.exception("LINE rich menu registration failed at startup; continuing")

    logger.info(
        "Scheduler started; entering %s", "the LINE webhook server" if config.channel.type == "line" else "the Telegram long-poll loop"
    )

    async def _on_message(
        chat_id: str,
        text: str,
        display_name: str | None = None,
        message_id: str | None = None,
        reply_to_message_id: str | None = None,
    ) -> None:
        await routing.on_message(
            chat_id,
            text,
            display_name,
            message_id,
            reply_to_message_id,
            db=db,
            llm=llm,
            channel=channel,
            config=config,
            owner_chat_id=owner_id,
            provider=provider,
            scheduler=scheduler,
            reminder_state=reminder_state,
            health_monitor=health_monitor,
        )

    async def _on_callback(chat_id: str, data: str, source_text: str, callback_id: str) -> None:
        await routing.on_callback(chat_id, data, source_text, callback_id, db=db, channel=channel, config=config, provider=provider)

    # The health monitor (Telegram only, see its construction above) runs
    # as its own asyncio task alongside the scheduler and the inbound loop.
    health_task = asyncio.create_task(health_monitor.run()) if health_monitor is not None else None
    try:
        await channel.run(_on_message, on_callback=_on_callback)
    finally:
        if health_task is not None:
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await health_task
        if health_monitor is not None:
            await health_monitor.aclose()
        scheduler.shutdown(wait=False)
        await channel.aclose()
        if llm is not None:
            await llm.aclose()
        db.close()
