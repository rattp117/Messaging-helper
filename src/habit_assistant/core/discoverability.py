"""Discoverability (SPEC-v1.1.md §4 Feature 3 "discoverability", R-D1-R-D5):
`/help` (a concise capability overview) and `/habits` (a per-habit listing
of effective goal + today's progress). Both are deterministic, read-only,
LLM-free -- routed by `core/commands.py`'s anchored `"help"`/`"habits"`
kinds, before `main.py`'s health-monitor deferral check, so both work with
Ollama DOWN (R-D1/AC35/AC37).

This module never writes to the DB and never imports a concrete channel
(mirrors `core/reminders.py`/`core/query.py`'s own "no channel imports"
seam) -- it only builds strings; `main.py` is the one that sends them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from habit_assistant.core import i18n, targets

if TYPE_CHECKING:
    from habit_assistant.config import Config
    from habit_assistant.core.habits import HabitRegistry
    from habit_assistant.storage.db import Database

# R-D3: `habit.type` -> a bilingual, human-friendly "kind" label -- raw
# type strings ("numeric"/"duration"/"boolean") would read fine in English
# but wouldn't localize, so each gets its own catalog entry (R-D5: every
# new string lives in core/i18n.py with both languages).
_HABIT_KIND_MSG_IDS: dict[str, str] = {
    "numeric": "habit_kind_numeric",
    "duration": "habit_kind_duration",
    "text": "habit_kind_text",
    "boolean": "habit_kind_boolean",
}


# ---------------------------------------------------------------------------
# R-D2 / AC35 / AC36: `/help`.
# ---------------------------------------------------------------------------


def build_help_text(config: "Config", lang: i18n.Language) -> str:
    """A concise, chat-friendly capability overview -- every time/number
    value below is read live from `config` (AC36: changing e.g.
    `config.weekly_review.time` changes the shown time), never
    hard-coded. Deterministic and LLM-free (works with Ollama down,
    AC35)."""
    lines = [
        i18n.t("help_header", lang),
        i18n.t("help_log", lang),
        i18n.t("help_undo", lang),
        i18n.t("help_target", lang),
        i18n.t("help_query", lang),
        i18n.t(
            "help_streaks",
            lang,
            milestones=", ".join(str(m) for m in config.gamification.milestones),
        ),
    ]

    if config.gamification.daily_summary:
        lines.append(i18n.t("help_daily_summary_on", lang, time=config.gamification.daily_summary_time))
    else:
        lines.append(i18n.t("help_daily_summary_off", lang))

    lines.append(
        i18n.t(
            "help_weekly_review",
            lang,
            day=config.weekly_review.day_of_week,
            time=config.weekly_review.time,
        )
    )
    lines.append(i18n.t("help_snooze", lang, minutes=config.snooze.default_minutes))

    if config.quiet_hours.windows:
        windows_str = ", ".join(f"{start}-{end}" for start, end in config.quiet_hours.windows)
        lines.append(i18n.t("help_quiet_hours_on", lang, windows=windows_str))
    else:
        lines.append(i18n.t("help_quiet_hours_off", lang))

    # Integration step (IMPL-v1.2-preferences.md's own documented "Known
    # limitations" #3): the three per-user settings commands, added after
    # this module itself landed. Unconditional (unlike the two branches
    # just above, which reflect a live config value) -- every user can
    # always run all three regardless of the global config's own defaults.
    lines.append(i18n.t("help_lang", lang))
    lines.append(i18n.t("help_quiet_cmd", lang))
    lines.append(i18n.t("help_remind_cmd", lang))
    # SPEC-v1.5.md §6/§11 integration step (IMPL-v1.5-checkins.md's own
    # documented "Known limitations" #1, same precedent as the three
    # lines above): `/checkin` + `/dnd` mentions, added after the
    # `checkins` module itself landed (the catalog keys are its own; this
    # module isn't among its owned files).
    lines.append(i18n.t("help_checkin_cmd", lang))
    lines.append(i18n.t("help_dnd_cmd", lang))
    # SPEC-v1.6.md §11 integration step (same "data only in each module's
    # own catalog block, wiring is a later append" posture as the
    # check-in/dnd lines just above): the four new v1.6.0 features. The
    # `dashboard` module's own IMPL flagged that it didn't add a
    # `help_dashboard_cmd` key (out of its file ownership) -- added here
    # as part of this integration pass, alongside the three keys
    # `heatmap`/`insights` already shipped ready-to-use.
    lines.append(i18n.t("help_dashboard_cmd", lang))
    lines.append(i18n.t("help_heatmap_cmd", lang))
    lines.append(i18n.t("help_records_cmd", lang))
    lines.append(i18n.t("help_trends_cmd", lang))
    # SPEC-v1.7.md §11 integration step (same "data only in each module's
    # own catalog block, wiring is a later append" posture as the lines
    # above): `/addhabit`/`/delhabit` (module `habitdef`), added after that
    # track landed.
    lines.append(i18n.t("help_addhabit_cmd", lang))
    lines.append(i18n.t("help_delhabit_cmd", lang))
    # v1.8.1 gap-fix (small patch scope, no SPEC-v1.8.md-numbered §11
    # integration step of its own -- the v1.8.0 integration pass updated
    # the Telegram command menu but never appended these /help lines):
    # same "data only in each module's own catalog block, wiring is a
    # later append" posture as every block above. `/log` (module
    # `quicklog`), `/routine` (module `routines`), then the backfill
    # message-syntax capability line (module `backfill` -- not a command,
    # so no menu entry of its own, mirroring `help_query`'s own
    # syntax-not-command shape).
    lines.append(i18n.t("help_log_cmd", lang))
    lines.append(i18n.t("help_routine_cmd", lang))
    lines.append(i18n.t("help_backfill", lang, max_days=config.backfill.max_days_back))

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# R-D3 / AC37 / AC38 / AC39: `/habits`.
# ---------------------------------------------------------------------------


def _goal_phrase(db: "Database", config: "Config", habit, lang: i18n.Language, user_id: str) -> str:
    """AC38: the goal shown comes from `targets.effective_goal`; the mark
    ("your target" vs. "default" vs. "no goal") comes from whether
    `db.get_target(user_id, habit.id)` is set -- two independent reads,
    exactly as R-D3 specifies, not inferred from one another. SPEC-v1.2.md
    R-D3: both scoped to `user_id`."""
    goal = targets.effective_goal(db, habit, config, user_id)
    if goal is None:
        return i18n.t("habits_overview_goal_none", lang)
    unit = habit.unit(lang) or ""
    msg_id = (
        "habits_overview_goal_target" if db.get_target(user_id, habit.id) is not None else "habits_overview_goal_default"
    )
    return i18n.t(msg_id, lang, goal=goal, unit=unit)


def _today_phrase(db: "Database", habit, today_str: str, lang: i18n.Language, user_id: str) -> str:
    """AC39: today's total for `user_id` -- `db.sum_value` for
    numeric/duration (a quantity, "today 500 ml"), `db.count_true` for
    boolean, `db.count` for text (both a plain count, "today 2 time(s)"),
    per R-D3."""
    if habit.type in ("numeric", "duration"):
        total = db.sum_value(user_id, habit.id, today_str)
        unit = habit.unit(lang) or ""
        return i18n.t("habits_overview_today_quantity", lang, total=total, unit=unit)
    if habit.type == "boolean":
        total = db.count_true(user_id, habit.id, today_str)
    else:  # text
        total = db.count(user_id, habit.id, today_str)
    return i18n.t("habits_overview_today_count", lang, total=total)


def build_habits_overview(
    db: "Database", config: "Config", registry: "HabitRegistry", clock, lang: i18n.Language, user_id: str
) -> str:
    """One line per registered habit, in registry order (AC37), for
    `user_id`: bilingual label, kind + unit, effective goal (marked
    user-target/default/no-goal, AC38), and today's total (AC39). `clock`
    (not a bare `datetime.now()` call) matches every other testable "what
    day is it" call site in this codebase (`main.py`'s own
    `_execute_undo`/`_execute_edit`/`handle_inbound_message`,
    `core/undo_ui.py`'s `send_undo_confirmation`) -- `config.app.timezone`
    correctness is the caller's responsibility via what it passes as
    `clock`, exactly as for those existing call sites. SPEC-v1.2.md R-D3:
    every read scoped to `user_id` (AC-U-ISO)."""
    today_str = clock().date().isoformat()
    lines = [i18n.t("habits_overview_header", lang)]
    for habit in registry:
        kind_label = i18n.t(_HABIT_KIND_MSG_IDS[habit.type], lang)
        goal_phrase = _goal_phrase(db, config, habit, lang, user_id)
        today_phrase = _today_phrase(db, habit, today_str, lang, user_id)
        lines.append(
            i18n.t(
                "habits_overview_line",
                lang,
                label=habit.label(lang),
                kind=kind_label,
                goal_phrase=goal_phrase,
                today_phrase=today_phrase,
            )
        )
    return "\n".join(lines)
