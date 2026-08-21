"""Bilingual message catalog + language detection (ROADMAP.md v0.6.0
"Bilingual Output & Message Catalog").

Every user-facing string sent through a `Channel` (SPEC.md §8) lives here,
keyed by a short id, with an `en` and `th` variant. Callers resolve via
`t(msg_id, lang, **kwargs)`, which looks the id up and applies
`str.format(**kwargs)` -- the same substitution mechanism the old inline
f-strings used, just centralized so there's exactly one place bilingual
copy can drift out of sync (AC6.2).

Language resolution has two distinct shapes, because "auto" means
something different depending on whether there's an inbound message to
read:

- **Reply to an inbound message** ("auto" mode): matches the detected
  language of the message that triggered the reply -- `resolve_reply_language`
  (AC6.1, AC6.3). Used for confirmations, the clarifying question, the
  deferred-ack, and undo/edit confirmations (all are responses to
  something the user just sent).
- **Unprompted sends** (reminders, health alerts, the weekly review): no
  inbound message to detect from, so "auto" falls back to
  `config.i18n.primary_language` (default Thai, per ROADMAP.md's own
  resolution for this ambiguity) -- `resolve_unprompted_language`.

In both cases, `config.i18n.language` forced to `"th"`/`"en"` overrides
the input/primary-language default entirely (AC6.3).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from habit_assistant.config import Config

Language = Literal["en", "th"]

# Thai Unicode block (U+0E00-U+0E7F). Any character in this range anywhere
# in the message -> Thai; pure non-Thai (including mixed digits/Latin/
# punctuation with zero Thai characters) -> English. Deterministic, no
# external calls, no locale dependence (AC6.5).
_THAI_CHAR_RE = re.compile(r"[฀-๿]")


def detect_language(text: str) -> Language:
    """Any Thai Unicode character present -> 'th'; otherwise -> 'en'
    (AC6.5). Empty/whitespace-only text has no Thai characters, so it
    classifies as 'en' -- a harmless default since callers never detect
    language from empty text in practice (commands.dispatch already
    treats blank input as "not a command" before this is reached)."""
    return "th" if _THAI_CHAR_RE.search(text) else "en"


def resolve_reply_language(inbound_text: str, config: "Config", user_pref: str = "auto") -> Language:
    """AC6.3: `language` = "th"/"en" forces that language regardless of
    the inbound text; "auto" matches the detected language of the
    message being replied to (AC6.1).

    SPEC-v1.2.md R-P1 (the READ side of per-user language; the `/lang`
    WRITE is module `preferences`): `user_pref` is the acting user's
    stored `users.language_pref` (`"auto"`/`"th"`/`"en"`), consulted
    AFTER the global `config.i18n.language` force (which still wins
    outright -- an operator-level override beats any individual
    preference) but BEFORE auto-detecting from the inbound text. Defaults
    to `"auto"`, which is a complete no-op here (falls through to
    detection exactly as every pre-v1.2 call site already does) --
    every call site in this codebase that doesn't yet look up a stored
    per-user preference is therefore byte-identical to v1.1 (AC-M3): the
    owner's own default preference is `"auto"` too (R-P1), so even a call
    site that DOES thread a real value through is a no-op for the owner
    until they run `/lang`."""
    forced = config.i18n.language
    if forced in ("th", "en"):
        return forced
    if user_pref in ("th", "en"):
        return user_pref
    return detect_language(inbound_text)


def resolve_unprompted_language(config: "Config", user_pref: str = "auto") -> Language:
    """Same forced-language override as `resolve_reply_language`, but for
    sends with no triggering inbound message (reminders, health alerts,
    the weekly review): "auto" uses the configured primary language
    (default Thai) instead of detecting from anything.

    SPEC-v1.2.md R-P1: `user_pref` follows the same precedence as
    `resolve_reply_language`'s own (see its docstring) -- global force,
    then this user's stored preference, then the primary-language
    fallback."""
    forced = config.i18n.language
    if forced in ("th", "en"):
        return forced
    if user_pref in ("th", "en"):
        return user_pref
    return config.i18n.primary_language


def language_instruction(lang: Language) -> str:
    """A short directive appended to an LLM system prompt so a
    free-form generation (diary reflection, weekly-review narrative)
    comes back in the target language too, not just the surrounding
    catalog copy (AC6.4)."""
    if lang == "th":
        return "เขียนคำตอบเป็นภาษาไทย ใช้น้ำเสียงสุภาพเป็นกันเอง เหมือนเพื่อนที่คอยให้กำลังใจ"
    return "Write the response in English."


def t(msg_id: str, lang: Language, **kwargs: object) -> str:
    """Look up `msg_id` in CATALOG and format it for `lang`."""
    try:
        variants = CATALOG[msg_id]
    except KeyError as exc:
        raise KeyError(f"Unknown i18n message id: {msg_id!r}") from exc
    try:
        template = variants[lang]
    except KeyError as exc:
        raise KeyError(f"i18n message {msg_id!r} has no {lang!r} variant") from exc
    return template.format(**kwargs)


# ---------------------------------------------------------------------------
# The catalog. Every entry MUST have both "en" and "th" keys (enforced by
# tests/test_i18n.py) and both variants MUST accept the same `.format()`
# placeholders, even if one language's phrasing doesn't need every one of
# them syntactically -- callers always pass the full kwarg set for an id.
# ---------------------------------------------------------------------------

CATALOG: dict[str, dict[Language, str]] = {
    # --- confirmations (main.py: handle_inbound_message) -----------------
    "clarifying_question": {
        "en": (
            "🤔 I couldn't quite tell what you meant — was that about water, a stretch "
            "break, or today's diary? Try something like '500ml water' or '10 min stretch'."
        ),
        "th": (
            "🤔 เอ๊ะ ยังไม่แน่ใจว่าหมายถึงอะไรนะ เกี่ยวกับน้ำ ยืดเส้น หรือไดอารี่วันนี้หรือเปล่า "
            "ลองพิมพ์แบบนี้ดูนะ เช่น 'น้ำ 500 มล.' หรือ 'ยืดเส้น 10 นาที'"
        ),
    },
    "deferred_ack": {
        "en": "⏳ Got it — I'll process this once the connection to the assistant is back.",
        "th": "⏳ รับทราบแล้วนะ เดี๋ยวประมวลผลให้ทันทีที่ระบบกลับมาใช้งานได้",
    },
    "nothing_to_undo": {
        "en": "🤷 Nothing to undo — you don't have any logged entries yet.",
        "th": "🤷 ยังไม่มีอะไรให้ยกเลิกนะ ยังไม่มีรายการที่บันทึกไว้เลย",
    },
    "nothing_to_edit": {
        "en": "🤷 Nothing to edit — I couldn't find a matching entry to update.",
        "th": "🤷 หารายการที่ตรงกันให้แก้ไม่เจอนะ",
    },
    "water_confirmation": {
        # SPEC-v1.1.md R-T5/AC23: {goal} -> {goal:g} so a DB-stored override
        # (a float, e.g. 2000.0) renders as "2000" not "2000.0" -- `:g` is a
        # no-op for the plain int config default (byte-identical, AC24).
        "en": "✅ {water_ml} ml logged — today {total} / {goal:g} ml ({pct}%)",
        "th": "✅ บันทึกน้ำ {water_ml} มล. แล้ว — วันนี้ดื่มไป {total} / {goal:g} มล. ({pct}%)",
    },
    "stretch_confirmation": {
        "en": "✅ {stretch_min} min stretch logged — {ordinal} today",
        "th": "✅ บันทึกยืดเส้น {stretch_min} นาที แล้ว — ครั้งที่ {count} ของวันนี้",
    },
    "diary_confirmation": {
        "en": "✅ Saved. {reflection}",
        "th": "✅ บันทึกแล้วนะ {reflection}",
    },
    "diary_reflection_fallback": {
        "en": "Thanks for sharing — noted.",
        "th": "ขอบคุณที่เล่าให้ฟังนะ บันทึกไว้แล้ว",
    },
    # --- undo / edit (main.py: _execute_undo / _execute_edit) ------------
    "undo_removed_water": {
        # SPEC-v1.1.md R-T5/AC23: see water_confirmation's own note on
        # {goal} -> {goal:g}.
        "en": "↩️ Undone — removed {description}. Today: {total} / {goal:g} ml ({pct}%)",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description} วันนี้เหลือ {total} / {goal:g} มล. ({pct}%)",
    },
    "undo_removed_stretch": {
        "en": "↩️ Undone — removed {description}. {count} stretch session(s) today",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description} วันนี้ยืดไปแล้ว {count} ครั้ง",
    },
    "undo_removed_generic": {
        "en": "↩️ Undone — removed {description}",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description}",
    },
    "edit_updated_water": {
        # SPEC-v1.1.md R-T5/AC23: see water_confirmation's own note on
        # {goal} -> {goal:g}.
        "en": "✏️ Updated to {value_num:g} ml — today {total} / {goal:g} ml ({pct}%)",
        "th": "✏️ แก้เป็น {value_num:g} มล. แล้ว — วันนี้ดื่มไป {total} / {goal:g} มล. ({pct}%)",
    },
    "edit_updated_stretch": {
        "en": "✏️ Updated to {value_num:g} min stretch — {ordinal} today",
        "th": "✏️ แก้เป็น {value_num:g} นาที แล้ว — ครั้งที่ {count} ของวันนี้",
    },
    # --- undo's "what did I just remove" descriptions ---------------------
    "describe_log_water": {
        "en": "{value_num:g} ml water",
        "th": "น้ำ {value_num:g} มล.",
    },
    "describe_log_stretch": {
        "en": "{value_num:g} min stretch",
        "th": "ยืดเส้น {value_num:g} นาที",
    },
    "describe_log_diary": {
        "en": 'diary entry: "{snippet}"',
        "th": 'ไดอารี่: "{snippet}"',
    },
    "describe_log_generic": {
        "en": "{category} entry",
        "th": "รายการ {category}",
    },
    # --- v0.4.0 recovery confirmations (main.py: reparse_pending_unparsed)
    "recovered_water": {
        "en": "🔁 Recovered: {water_ml} ml logged from your earlier message.",
        "th": "🔁 กู้คืนแล้ว: บันทึกน้ำ {water_ml} มล. จากข้อความก่อนหน้า",
    },
    "recovered_stretch": {
        "en": "🔁 Recovered: {stretch_min} min stretch logged from your earlier message.",
        "th": "🔁 กู้คืนแล้ว: บันทึกยืดเส้น {stretch_min} นาที จากข้อความก่อนหน้า",
    },
    "recovered_diary": {
        "en": "🔁 Recovered: saved your earlier diary message.",
        "th": "🔁 กู้คืนแล้ว: บันทึกไดอารี่จากข้อความก่อนหน้าเรียบร้อย",
    },
    # --- reminders (core/reminders.py) ------------------------------------
    "reminder_water": {
        "en": "💧 Time for water. How much did you drink?",
        "th": "💧 ถึงเวลาดื่มน้ำแล้วนะ วันนี้ดื่มไปเท่าไหร่แล้ว?",
    },
    "reminder_stretch": {
        "en": "🧘 Stretch break — do a few minutes and tell me how long.",
        "th": "🧘 พักยืดเส้นกันหน่อย ยืดสักไม่กี่นาทีแล้วบอกด้วยนะว่ากี่นาที",
    },
    "reminder_diary": {
        "en": "📓 How was today? A few lines is enough.",
        "th": "📓 วันนี้เป็นยังไงบ้าง เขียนสักนิดก็พอนะ",
    },
    # --- health alerts (core/health.py) -- unprompted, primary-language ---
    "ollama_down_alert": {
        "en": (
            "⚠️ Ollama is unreachable — new messages will be saved and "
            "processed automatically once it's back."
        ),
        "th": (
            "⚠️ ตอนนี้ติดต่อ Ollama ไม่ได้ ข้อความใหม่จะถูกเก็บไว้แล้วประมวลผลอัตโนมัติ"
            "ทันทีที่กลับมาใช้งานได้"
        ),
    },
    "telegram_down_alert": {
        "en": (
            "⚠️ Telegram connectivity check failed (this alert may not "
            "reach you until it recovers)."
        ),
        "th": "⚠️ ตรวจสอบการเชื่อมต่อ Telegram ไม่สำเร็จ (แจ้งเตือนนี้อาจไปไม่ถึงจนกว่าระบบจะกลับมา)",
    },
    # --- weekly review (core/review.py) -- unprompted, primary-language ---
    "weekly_review_header": {
        "en": "📊 Weekly Review",
        "th": "📊 สรุปประจำสัปดาห์",
    },
    "weekly_review_fallback_narrative": {
        "en": "Here is your weekly summary.",
        "th": "นี่คือสรุปประจำสัปดาห์ของคุณ",
    },
    "stats_water_header": {
        "en": "Water (ml / goal / %):",
        "th": "น้ำ (มล. / เป้าหมาย / %):",
    },
    "stats_water_line": {
        "en": "  {day}: {water_ml} / {water_goal_ml} ({water_pct}%)",
        "th": "  {day}: {water_ml} / {water_goal_ml} ({water_pct}%)",
    },
    "stats_water_total": {
        "en": "Water total: {water_total_ml} ml, average/day: {water_avg_ml} ml",
        "th": "น้ำรวม: {water_total_ml} มล. เฉลี่ยต่อวัน: {water_avg_ml} มล.",
    },
    "stats_stretch_summary": {
        "en": "Stretch sessions this week: {stretch_total}, current streak: {stretch_streak} day(s)",
        "th": "ยืดเส้นสัปดาห์นี้: {stretch_total} ครั้ง ต่อเนื่อง {stretch_streak} วัน",
    },
    "stats_diary_summary": {
        "en": "Diary entries this week: {diary_count}",
        "th": "บันทึกไดอารี่สัปดาห์นี้: {diary_count} ครั้ง",
    },
    # -----------------------------------------------------------------
    # ROADMAP.md v0.7.0 "Multi-Habit Extensibility" (SPEC-v0.7.md §5): the
    # type-generic templates. Built-in habits (water/stretch/diary) never
    # use these -- they keep reusing the entries above verbatim, which is
    # what makes AC7.1/AC9 byte-identical-by-construction rather than by
    # re-derivation (SPEC-v0.7.md §9 risk 2). These render any *other*
    # configured habit, parameterized by its `label`/`unit`/type.
    # -----------------------------------------------------------------
    "confirm_numeric_goal": {
        "en": "✅ {value:g} {unit} logged — today {total:g} / {goal:g} {unit} ({pct}%)",
        "th": "✅ บันทึก{label} {value:g} {unit} แล้ว — วันนี้ {total:g} / {goal:g} {unit} ({pct}%)",
    },
    "confirm_numeric_nogoal": {
        "en": "✅ {value:g} {unit} logged today",
        "th": "✅ บันทึก{label} {value:g} {unit} แล้ว วันนี้",
    },
    "confirm_duration": {
        "en": "✅ {value:g} {unit} {label} logged — {ordinal} today",
        "th": "✅ บันทึก{label} {value:g} {unit} แล้ว — ครั้งที่ {count} ของวันนี้",
    },
    "confirm_text": {
        "en": "✅ {label} saved. {reflection}",
        "th": "✅ บันทึก{label}แล้วนะ {reflection}",
    },
    "confirm_boolean": {
        "en": "✅ {label} — {status} today",
        "th": "✅ {label} — {status} วันนี้",
    },
    "bool_status_done": {
        "en": "done",
        "th": "ทำแล้ว",
    },
    "bool_status_not_done": {
        "en": "not done",
        "th": "ยังไม่ทำ",
    },
    "undo_removed_numeric": {
        "en": "↩️ Undone — removed {description}. Today: {total:g} / {goal:g} {unit} ({pct}%)",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description} วันนี้เหลือ {total:g} / {goal:g} {unit} ({pct}%)",
    },
    "undo_removed_duration": {
        "en": "↩️ Undone — removed {description}. {count} {label} session(s) today",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description} วันนี้{label}ไปแล้ว {count} ครั้ง",
    },
    "undo_removed_boolean": {
        "en": "↩️ Undone — removed {description}",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description}",
    },
    "edit_updated_numeric": {
        "en": "✏️ Updated to {value:g} {unit} — today {total:g} / {goal:g} {unit} ({pct}%)",
        "th": "✏️ แก้เป็น {value:g} {unit} แล้ว — วันนี้ {total:g} / {goal:g} {unit} ({pct}%)",
    },
    "edit_updated_duration": {
        "en": "✏️ Updated to {value:g} {unit} {label} — {ordinal} today",
        "th": "✏️ แก้เป็น {value:g} {unit} {label} แล้ว — ครั้งที่ {count} ของวันนี้",
    },
    "describe_log_numeric": {
        "en": "{value_num:g} {unit} {label}",
        "th": "{label} {value_num:g} {unit}",
    },
    "describe_log_duration": {
        "en": "{value_num:g} {unit} {label}",
        "th": "{label} {value_num:g} {unit}",
    },
    "describe_log_boolean": {
        "en": "{label}",
        "th": "{label}",
    },
    "recovered_numeric": {
        "en": "🔁 Recovered: {value:g} {unit} {label} logged from your earlier message.",
        "th": "🔁 กู้คืนแล้ว: บันทึก{label} {value:g} {unit} จากข้อความก่อนหน้า",
    },
    "recovered_duration": {
        "en": "🔁 Recovered: {value:g} {unit} {label} logged from your earlier message.",
        "th": "🔁 กู้คืนแล้ว: บันทึก{label} {value:g} {unit} จากข้อความก่อนหน้า",
    },
    "recovered_boolean": {
        "en": "🔁 Recovered: {label} logged from your earlier message.",
        "th": "🔁 กู้คืนแล้ว: บันทึก{label}จากข้อความก่อนหน้า",
    },
    # Not in SPEC-v0.7.md §5's illustrative catalog list (which names only
    # recovered_numeric/_duration/_boolean); added so a non-built-in TEXT
    # habit's deferred-recovery path (main.py._send_recovered_generic) has
    # somewhere to resolve to, mirroring recovered_diary's shape.
    "recovered_text": {
        "en": "🔁 Recovered: saved your earlier {label} message.",
        "th": "🔁 กู้คืนแล้ว: บันทึก{label}จากข้อความก่อนหน้าเรียบร้อย",
    },
    # --- reminders (core/reminders.py, module M2) -------------------------
    "reminder_generic": {
        "en": "⏰ Time for {label}. How did it go?",
        "th": "⏰ ถึงเวลา{label}แล้วนะ วันนี้เป็นยังไงบ้าง?",
    },
    # --- weekly review (core/review.py, module M3) -------------------------
    "stats_generic_numeric_header": {
        "en": "{label} ({unit} / goal / %):",
        "th": "{label} ({unit} / เป้าหมาย / %):",
    },
    "stats_generic_numeric_line": {
        "en": "  {day}: {value:g} / {goal:g} ({pct}%)",
        "th": "  {day}: {value:g} / {goal:g} ({pct}%)",
    },
    "stats_generic_numeric_total": {
        "en": "{label} total: {total:g} {unit}, average/day: {avg:g} {unit}",
        "th": "{label}รวม: {total:g} {unit} เฉลี่ยต่อวัน: {avg:g} {unit}",
    },
    "stats_generic_duration_summary": {
        "en": "{label} sessions this week: {total}, current streak: {streak} day(s)",
        "th": "{label}สัปดาห์นี้: {total} ครั้ง ต่อเนื่อง {streak} วัน",
    },
    "stats_generic_count_summary": {
        "en": "{label} entries this week: {count}",
        "th": "บันทึก{label}สัปดาห์นี้: {count} ครั้ง",
    },
    # -----------------------------------------------------------------
    # ROADMAP.md v0.8.0 "Natural-Language Queries" (core/query.py):
    # "how much water this week?" / "อาทิตย์นี้ยืดกี่ครั้ง" answers, plus
    # the fail-closed "can't answer" fallback (AC8.4) and the four
    # timeframe labels these answers are parameterized with (AC8.3).
    # -----------------------------------------------------------------
    "query_cant_answer": {
        "en": (
            "🤔 I can't answer that yet — try asking about a habit I track, "
            "like 'how much water this week?'"
        ),
        "th": (
            "🤔 ตอบคำถามนี้ไม่ได้นะ ลองถามเกี่ยวกับสิ่งที่บันทึกไว้ เช่น "
            "'อาทิตย์นี้ดื่มน้ำไปเท่าไหร่?'"
        ),
    },
    "query_timeframe_today": {"en": "today", "th": "วันนี้"},
    "query_timeframe_yesterday": {"en": "yesterday", "th": "เมื่อวาน"},
    "query_timeframe_this_week": {"en": "this week", "th": "สัปดาห์นี้"},
    "query_timeframe_last_7_days": {"en": "in the last 7 days", "th": "ใน 7 วันที่ผ่านมา"},
    "query_answer_numeric_sum": {
        "en": "📊 {label}: {total:g} {unit} {timeframe}",
        "th": "📊 {label}: {total:g} {unit} {timeframe}",
    },
    "query_answer_numeric_count": {
        "en": "📊 {label}: logged {count} time(s) {timeframe}",
        "th": "📊 บันทึก{label}ไปแล้ว {count} ครั้ง {timeframe}",
    },
    "query_answer_duration": {
        "en": "📊 {label}: {count} time(s), {total:g} {unit} total {timeframe}",
        "th": "📊 {label}: {count} ครั้ง รวม {total:g} {unit} {timeframe}",
    },
    "query_answer_boolean": {
        "en": "📊 {label}: done on {count} day(s) {timeframe}",
        "th": "📊 {label}: ทำแล้ว {count} วัน {timeframe}",
    },
    "query_answer_text": {
        "en": "📊 {label}: {count} entry(ies) {timeframe}",
        "th": "📊 บันทึก{label} {count} ครั้ง {timeframe}",
    },
    # -----------------------------------------------------------------
    # ROADMAP.md v0.9.0 "Adaptive Reminders, Snooze & Quiet Hours"
    # (core/commands.py's "snooze" kind, main.py's handler): AC9.3.
    # -----------------------------------------------------------------
    "snooze_confirmed": {
        "en": "⏰ Okay — I'll remind you about {label} again in {minutes} min.",
        "th": "⏰ ได้เลย เดี๋ยวเตือนเรื่อง{label}อีกครั้งใน {minutes} นาที",
    },
    "snooze_no_recent_reminder": {
        "en": "🤷 There's no recent reminder to snooze yet.",
        "th": "🤷 ยังไม่มีการเตือนล่าสุดให้เลื่อนนะ",
    },
    # -----------------------------------------------------------------
    # ROADMAP.md v0.10.0 "Streaks, Gentle Gamification & Daily Summary"
    # (core/streaks.py, main.py). Milestone lines are warm, one-off, and
    # never guilt-tripping (AC10.2); the daily summary is a plain factual
    # recap (AC10.3).
    # -----------------------------------------------------------------
    "milestone_reached": {
        "en": "🔥 {streak}-day {label} streak — nice work, keep it going!",
        "th": "🔥 ต่อเนื่อง {streak} วันแล้วสำหรับ{label} — เก่งมากเลยนะ!",
    },
    "daily_summary_header": {
        "en": "🌙 Today's Summary",
        "th": "🌙 สรุปวันนี้",
    },
    "daily_summary_numeric_goal": {
        "en": "  {label}: {total:g} / {goal:g} {unit} ({pct}%) · streak {streak}d",
        "th": "  {label}: {total:g} / {goal:g} {unit} ({pct}%) · ต่อเนื่อง {streak} วัน",
    },
    "daily_summary_numeric_nogoal": {
        "en": "  {label}: {total:g} {unit} today · streak {streak}d",
        "th": "  {label}: {total:g} {unit} วันนี้ · ต่อเนื่อง {streak} วัน",
    },
    "daily_summary_duration_nogoal": {
        "en": "  {label}: {total} session(s) today · streak {streak}d",
        "th": "  {label}: {total} ครั้งวันนี้ · ต่อเนื่อง {streak} วัน",
    },
    "daily_summary_boolean": {
        "en": "  {label}: {status} · streak {streak}d",
        "th": "  {label}: {status} · ต่อเนื่อง {streak} วัน",
    },
    "daily_summary_text": {
        "en": "  {label}: {total} entry(ies) today · streak {streak}d",
        "th": "  {label}: บันทึกแล้ว {total} ครั้งวันนี้ · ต่อเนื่อง {streak} วัน",
    },
    # -----------------------------------------------------------------
    # ROADMAP.md v1.0.0 "Insights: Charts-as-Images + Garmin Import"
    # (core/charts.py, core/garmin.py, core/review.py).
    # -----------------------------------------------------------------
    "chart_ylabel_count": {
        "en": "count",
        "th": "ครั้ง",
    },
    "chart_caption_numeric": {
        "en": "{label}: {total:g} {unit} this week (avg {avg:g} {unit}/day)",
        "th": "{label}: {total:g} {unit} สัปดาห์นี้ (เฉลี่ย {avg:g} {unit}/วัน)",
    },
    "chart_caption_duration": {
        "en": "{label}: {total:g} sessions this week — {streak}-day streak",
        "th": "{label}: {total:g} ครั้งสัปดาห์นี้ — ต่อเนื่อง {streak} วัน",
    },
    "chart_caption_boolean": {
        "en": "{label}: done {total:g} day(s) this week",
        "th": "{label}: ทำแล้ว {total:g} วันสัปดาห์นี้",
    },
    "garmin_section_header": {
        "en": "💧 Garmin Hydration Cross-Check",
        "th": "💧 เปรียบเทียบข้อมูลน้ำจาก Garmin",
    },
    "garmin_unavailable": {
        "en": (
            "⚠️ Garmin data unavailable this week (file missing or unreadable) — "
            "showing self-reported totals only."
        ),
        "th": (
            "⚠️ ไม่มีข้อมูลจาก Garmin ในสัปดาห์นี้ (ไฟล์หายหรืออ่านไม่ได้) — "
            "แสดงเฉพาะข้อมูลที่บันทึกเองเท่านั้น"
        ),
    },
    "garmin_day_line": {
        "en": "  {day}: you {self_reported} ml vs. Garmin {garmin} ml",
        "th": "  {day}: บันทึกเอง {self_reported} มล. เทียบกับ Garmin {garmin} มล.",
    },
    "garmin_day_line_flagged": {
        "en": "  {day}: you {self_reported} ml vs. Garmin {garmin} ml ⚠️ diff {diff} ml",
        "th": "  {day}: บันทึกเอง {self_reported} มล. เทียบกับ Garmin {garmin} มล. ⚠️ ต่างกัน {diff} มล.",
    },
    # -----------------------------------------------------------------
    # SPEC-v1.1.md "Undo menu (Telegram discoverability) + per-habit
    # targets" -- Feature 1 (undo-ui, core/undo_ui.py) and Feature 2
    # (targets, core/targets_command.py). Added here as one shared-surface
    # edit covering both features' disjoint keys (SPEC-v1.1.md §6), so the
    # two parallel modules never need to touch this file themselves.
    # -----------------------------------------------------------------
    "undo_button_label": {
        "en": "↩️ Undo",
        "th": "↩️ ยกเลิก",
    },
    "already_undone": {
        "en": "🤷 That one's already been removed.",
        "th": "🤷 รายการนี้ถูกลบไปแล้วนะ",
    },
    "target_set": {
        "en": "✅ Set {label}'s daily goal to {goal:g} {unit}. (was {previous})",
        "th": "✅ ตั้งเป้าหมาย{label}รายวันเป็น {goal:g} {unit} แล้ว (เดิม {previous})",
    },
    "target_cleared": {
        "en": "↩️ Reset {label}'s daily goal to the default {default:g} {unit}.",
        "th": "↩️ รีเซ็ตเป้าหมาย{label}รายวันกลับเป็นค่าเริ่มต้น {default:g} {unit} แล้ว",
    },
    "target_cleared_nogoal": {
        "en": "↩️ Cleared {label}'s target — back to no daily goal.",
        "th": "↩️ ล้างเป้าหมาย{label}แล้ว — กลับไปไม่มีเป้าหมายรายวัน",
    },
    "target_show": {
        "en": "🎯 {label}: {goal:g} {unit}/day{default_note}",
        "th": "🎯 {label}: {goal:g} {unit}/วัน{default_note}",
    },
    "target_show_default_note": {
        "en": " (default {default:g} {unit})",
        "th": " (ค่าเริ่มต้น {default:g} {unit})",
    },
    "target_show_all_header": {
        "en": "🎯 Daily goals:",
        "th": "🎯 เป้าหมายรายวัน:",
    },
    "target_show_all_line": {
        "en": "• {label}: {goal:g} {unit}/day{default_note}",
        "th": "• {label}: {goal:g} {unit}/วัน{default_note}",
    },
    "target_show_all_line_nogoal": {
        "en": "• {label}: — (no goal)",
        "th": "• {label}: — (ไม่มีเป้าหมาย)",
    },
    "target_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "target_not_goalable": {
        "en": "🤔 {label} doesn't have a daily goal to set.",
        "th": "🤔 {label}ไม่มีเป้าหมายรายวันให้ตั้งนะ",
    },
    "target_invalid_value": {
        "en": '🤔 A daily goal has to be a positive number, e.g. "/target {habit_id} {example}".',
        "th": '🤔 เป้าหมายรายวันต้องเป็นตัวเลขบวกนะ เช่น "/target {habit_id} {example}"',
    },
    "target_usage": {
        "en": (
            "🤔 Usage: \"/target <habit> <value>\" to set, \"/target <habit>\" to view, "
            "\"/target <habit> default\" to reset, or \"/target\" to see every goal."
        ),
        "th": (
            "🤔 วิธีใช้: \"/target <กิจกรรม> <ค่า>\" เพื่อตั้งเป้า, \"/target <กิจกรรม>\" เพื่อดูเป้าหมาย, "
            "\"/target <กิจกรรม> default\" เพื่อรีเซ็ต หรือ \"/target\" เพื่อดูเป้าหมายทั้งหมด"
        ),
    },
    "target_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    # -----------------------------------------------------------------
    # SPEC-v1.1.md "Undo menu + per-habit targets" -- Feature 3
    # (discoverability, core/discoverability.py): `/help` and `/habits`
    # (R-D5: every new string lives here, both languages). `help_log`
    # deliberately shows BOTH an English-style and a Thai-style logging
    # example in EVERY language variant (not just the reply language) --
    # the bot understands both scripts for logging regardless of which
    # language `/help` itself replies in (AC36's "EN/TH examples").
    # -----------------------------------------------------------------
    "help_header": {
        "en": "🤖 Here's what I can do:",
        "th": "🤖 นี่คือสิ่งที่ฉันทำได้:",
    },
    "help_log": {
        "en": (
            "📝 Log anything in plain text, English or Thai — '500ml' or 'น้ำ 500 มล.', "
            "'10 min stretch' or 'ยืดเส้น 10 นาที' — or just tell me about your day."
        ),
        "th": (
            "📝 พิมพ์บันทึกได้เลยแบบธรรมชาติ ทั้งไทยและอังกฤษ — 'น้ำ 500 มล.' หรือ '500ml', "
            "'ยืดเส้น 10 นาที' หรือ '10 min stretch' — หรือเล่าให้ฟังว่าวันนี้เป็นยังไง"
        ),
    },
    "help_undo": {
        "en": "↩️ Undo your last entry: tap the ↩️ Undo button under any confirmation, or type /undo (ยกเลิก).",
        "th": "↩️ ยกเลิกรายการล่าสุด: แตะปุ่ม ↩️ ยกเลิก ใต้ข้อความยืนยัน หรือพิมพ์ /undo (ยกเลิก)",
    },
    "help_target": {
        "en": (
            "🎯 Set a daily goal: /target water 2000, or just say 'from now on I want to drink "
            "2.5L a day'. Check one: /target water. Clear it: /target water default."
        ),
        "th": (
            "🎯 ตั้งเป้าหมายรายวัน: /target water 2000 หรือพิมพ์ว่า 'ต่อไปอยากดื่มน้ำวันละ 2.5 ลิตร' ก็ได้ "
            "ดูเป้าหมาย: /target water รีเซ็ต: /target water default"
        ),
    },
    "help_query": {
        "en": "📊 Ask me questions: 'how much water this week?' or 'did I stretch today?'",
        "th": "📊 ถามได้เลย เช่น 'อาทิตย์นี้ดื่มน้ำไปเท่าไหร่?' หรือ 'วันนี้ยืดเส้นหรือยัง?'",
    },
    "help_streaks": {
        "en": "🔥 Streaks are celebrated at {milestones}-day milestones.",
        "th": "🔥 ต่อเนื่องครบ {milestones} วัน จะมีข้อความให้กำลังใจ",
    },
    "help_daily_summary_on": {
        "en": "🌙 A daily summary is sent at {time}.",
        "th": "🌙 สรุปประจำวันจะส่งให้ตอน {time}",
    },
    "help_daily_summary_off": {
        "en": "🌙 The daily summary is currently turned off.",
        "th": "🌙 ตอนนี้ปิดการสรุปประจำวันอยู่",
    },
    "help_weekly_review": {
        "en": "📈 A weekly review is sent every {day} at {time}.",
        "th": "📈 สรุปประจำสัปดาห์จะส่งทุกวัน {day} เวลา {time}",
    },
    "help_snooze": {
        "en": "⏰ Snooze a reminder: type 'snooze' or 'snooze 30' (default {minutes} min).",
        "th": "⏰ เลื่อนการแจ้งเตือน: พิมพ์ 'เลื่อน' หรือ 'เลื่อน 30 นาที' (ค่าเริ่มต้น {minutes} นาที)",
    },
    "help_quiet_hours_on": {
        "en": "🌙 Quiet hours (no reminders sent): {windows}.",
        "th": "🌙 ช่วงเวลางดแจ้งเตือน: {windows}",
    },
    "help_quiet_hours_off": {
        "en": "🌙 No quiet hours are currently configured.",
        "th": "🌙 ตอนนี้ยังไม่ได้ตั้งช่วงเวลางดแจ้งเตือน",
    },
    # Integration step (IMPL-v1.2-preferences.md's own documented "Known
    # limitations" #3): `/help` predates the `access`/`preferences`/
    # `schedules` modules, so it had no mention of `/lang`/`/quiet`/
    # `/remind` at all. Added here, one line each, same "short imperative
    # + example" shape as help_snooze/help_target just above.
    "help_lang": {
        "en": '🌐 Set your reply language: /lang en, /lang th, or /lang auto (Thai: "ภาษา en|th|auto").',
        "th": '🌐 ตั้งภาษาที่ใช้ตอบ: /lang en, /lang th หรือ /lang auto (หรือ "ภาษา en|th|auto")',
    },
    "help_quiet_cmd": {
        "en": '🌙 Set your own quiet hours: /quiet 22:00-07:00, or /quiet off to clear (Thai: "เงียบ ...").',
        "th": '🌙 ตั้งช่วงเวลางดแจ้งเตือนของคุณเอง: /quiet 22:00-07:00 หรือ /quiet off เพื่อล้าง (หรือ "เงียบ ...")',
    },
    "help_remind_cmd": {
        "en": (
            '⏰ Set your own reminder times per habit: /remind water 08:00 12:00 (Thai: "เตือน ..."). '
            "Show: /remind water. Reset: /remind water default. Off: /remind water off."
        ),
        "th": (
            '⏰ ตั้งเวลาแจ้งเตือนของคุณเองรายกิจกรรม: /remind water 08:00 12:00 (หรือ "เตือน ...") '
            "ดู: /remind water รีเซ็ต: /remind water default ปิด: /remind water off"
        ),
    },
    "habits_overview_header": {
        "en": "📋 Your habits:",
        "th": "📋 กิจกรรมของคุณ:",
    },
    "habits_overview_line": {
        "en": "• {label} ({kind}) — {goal_phrase} · {today_phrase}",
        "th": "• {label} ({kind}) — {goal_phrase} · {today_phrase}",
    },
    "habits_overview_goal_target": {
        "en": "{goal:g} {unit}/day (your target)",
        "th": "{goal:g} {unit}/วัน (เป้าหมายของคุณ)",
    },
    "habits_overview_goal_default": {
        "en": "{goal:g} {unit}/day (default)",
        "th": "{goal:g} {unit}/วัน (ค่าเริ่มต้น)",
    },
    "habits_overview_goal_none": {
        "en": "no goal",
        "th": "ไม่มีเป้าหมาย",
    },
    "habits_overview_today_quantity": {
        "en": "today {total:g} {unit}",
        "th": "วันนี้ {total:g} {unit}",
    },
    "habits_overview_today_count": {
        "en": "today {total:g} time(s)",
        "th": "วันนี้ {total:g} ครั้ง",
    },
    "habit_kind_numeric": {
        "en": "numeric",
        "th": "ตัวเลข",
    },
    "habit_kind_duration": {
        "en": "duration",
        "th": "ระยะเวลา",
    },
    "habit_kind_text": {
        "en": "text",
        "th": "ข้อความ",
    },
    "habit_kind_boolean": {
        "en": "yes/no",
        "th": "ทำ/ไม่ทำ",
    },
    # ===================================================================
    # SPEC-v1.2.md "Multi-user support" -- shared-surface key-block
    # skeletons (§11: "the i18n key-block skeletons are created in the
    # shared surface first; each module then fills only its own disjoint
    # kinds/keys", so the three parallel modules below never collide on
    # this file). Each section is reserved for exactly one module; no key
    # is added here yet -- only the section marker.
    # ===================================================================

    # -----------------------------------------------------------------
    # Module `access` (onboarding/allowlist/admin: /start, /approve,
    # /block, /users, /invite -- R-A1-R-A5). SPEC-v1.2.md §3.2 gives the
    # illustrative copy for the unknown-first-contact reply, the
    # owner-notification, and the just-approved reply verbatim; R-A2/R-A3
    # both literally say "reply access_pending" for BOTH the unknown-
    # first-contact case (R-A2) and the pending-repeat case (R-A3) -- one
    # catalog id, one string, `i18n.t()` returns the identical text both
    # times by construction. `access_denied` (blocked, R-A3) reuses
    # §3.2's "not-yet-approved user messaging again" example text, since
    # that is the only denial-flavored copy the spec provides.
    # -----------------------------------------------------------------
    "access_pending": {
        "en": (
            "👋 Hi! This is a private habit bot. I've asked the owner to "
            "approve you — you'll hear back soon."
        ),
        "th": (
            "👋 สวัสดีค่ะ! นี่คือบอทติดตามกิจกรรมส่วนตัว ได้แจ้งเจ้าของบอทให้อนุมัติคุณแล้วนะ "
            "รอฟังผลอีกสักครู่"
        ),
    },
    "access_denied": {
        "en": "⏳ You're not approved to use this bot yet.",
        "th": "⏳ ยังไม่ได้รับอนุญาตให้ใช้บอทนี้นะ",
    },
    "access_request": {
        "en": "🔔 {name} (chat {chat_id}) asked for access. Approve with: /approve {chat_id}",
        "th": "🔔 {name} (แชท {chat_id}) ขอสิทธิ์เข้าใช้งาน อนุมัติด้วย: /approve {chat_id}",
    },
    "access_granted": {
        "en": '✅ You\'re in! Just type things like "500ml" or "10 min stretch". Send /help to see everything.',
        "th": '✅ เข้าใช้งานได้แล้ว! พิมพ์แบบนี้ได้เลย เช่น "500ml" หรือ "ยืดเส้น 10 นาที" พิมพ์ /help เพื่อดูทุกอย่างที่ทำได้',
    },
    "start_welcome": {
        "en": "👋 Welcome back! Send /help to see everything I can do.",
        "th": "👋 ยินดีต้อนรับกลับมา! พิมพ์ /help เพื่อดูทุกอย่างที่ทำได้",
    },
    "admin_usage": {
        "en": "🤔 Usage: /approve <chat_id>, /block <chat_id>, /invite <chat_id>, or /users to list everyone.",
        "th": "🤔 วิธีใช้: /approve <chat_id>, /block <chat_id>, /invite <chat_id> หรือ /users เพื่อดูรายชื่อผู้ใช้ทั้งหมด",
    },
    "admin_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "admin_approved_ack": {
        "en": "✅ {chat_id} approved.",
        "th": "✅ อนุมัติ {chat_id} แล้ว",
    },
    "admin_blocked_ack": {
        "en": "🚫 {chat_id} blocked.",
        "th": "🚫 บล็อก {chat_id} แล้ว",
    },
    "users_list_header": {
        "en": "👥 Users:",
        "th": "👥 รายชื่อผู้ใช้:",
    },
    "users_list_line": {
        "en": "• {chat_id} — {role} · {status}{lang_suffix}",
        "th": "• {chat_id} — {role} · {status}{lang_suffix}",
    },

    # -----------------------------------------------------------------
    # Module `preferences` (/lang, /quiet -- R-P1/R-P2, core/preferences.py).
    # `lang_set`'s {value} is the raw code the user just typed ("en"/"th"/
    # "auto") -- shown verbatim rather than translated to a language NAME,
    # same "echo the token back" convention `remind_set`'s {times} uses for
    # HH:MM lists. `preferences_save_failed` mirrors `target_save_failed`/
    # `admin_save_failed`/`remind_save_failed`'s identical text -- one key
    # per module by convention (SPEC-v1.2.md §11: "each module then fills
    # only its own disjoint kinds/keys"), not a shared id.
    # -----------------------------------------------------------------
    "lang_set": {
        "en": "✅ Got it — I'll reply in \"{value}\" from now on.",
        "th": "✅ ได้เลย ต่อไปนี้จะตอบเป็น \"{value}\" นะ",
    },
    "lang_usage": {
        "en": '🤔 Usage: "/lang en", "/lang th", or "/lang auto" (Thai: "ภาษา en|th|auto").',
        "th": '🤔 วิธีใช้: "/lang en", "/lang th" หรือ "/lang auto" (หรือ "ภาษา en|th|auto")',
    },
    "quiet_set": {
        "en": "🌙 Quiet hours set: {windows}. No reminders will be sent to you during that time.",
        "th": "🌙 ตั้งช่วงเวลางดแจ้งเตือนแล้ว: {windows} จะไม่มีการแจ้งเตือนส่งถึงคุณในช่วงนี้นะ",
    },
    "quiet_cleared": {
        "en": "🌙 Quiet hours cleared for you — reminders can be sent at any time now.",
        "th": "🌙 ล้างช่วงเวลางดแจ้งเตือนของคุณแล้ว — ตอนนี้จะได้รับการแจ้งเตือนได้ตลอดเวลา",
    },
    "quiet_usage": {
        "en": (
            '🤔 Usage: "/quiet 22:00-07:00" (comma-separate for more than one window), '
            'or "/quiet off" to clear (Thai: "เงียบ ...").'
        ),
        "th": (
            '🤔 วิธีใช้: "/quiet 22:00-07:00" (คั่นด้วยจุลภาคถ้ามีหลายช่วง) '
            'หรือ "/quiet off" เพื่อล้าง (หรือ "เงียบ ...")'
        ),
    },
    "quiet_invalid_window": {
        "en": (
            '🤔 Each quiet-hours window must look like "22:00-07:00" (24-hour HH:MM). '
            'Separate multiple windows with commas, or use "off" to clear.'
        ),
        "th": (
            '🤔 แต่ละช่วงเวลาต้องอยู่ในรูปแบบ "22:00-07:00" (HH:MM 24 ชั่วโมง) '
            'ถ้ามีหลายช่วงให้คั่นด้วยจุลภาค หรือใช้ "off" เพื่อล้าง'
        ),
    },
    "preferences_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },

    # -----------------------------------------------------------------
    # Module `schedules` (/remind -- R-S5). Keys go here.
    # -----------------------------------------------------------------
    "remind_set": {
        "en": "✅ {label} reminders set: {times}.",
        "th": "✅ ตั้งเวลาแจ้งเตือน{label}เป็น: {times} แล้ว",
    },
    "remind_off": {
        "en": "🔕 {label} reminders turned off for you.",
        "th": "🔕 ปิดการแจ้งเตือน{label}ให้คุณแล้ว",
    },
    "remind_cleared": {
        "en": "↩️ {label} reminders reset to default: {times}.",
        "th": "↩️ รีเซ็ตเวลาแจ้งเตือน{label}กลับเป็นค่าเริ่มต้น: {times} แล้ว",
    },
    "remind_show": {
        "en": "⏰ {label} reminders: {times} ({source})",
        "th": "⏰ เวลาแจ้งเตือน{label}: {times} ({source})",
    },
    "remind_show_off": {
        "en": "🔕 {label} reminders: off (no reminders for you)",
        "th": "🔕 การแจ้งเตือน{label}: ปิดอยู่ (ไม่มีการแจ้งเตือนให้คุณ)",
    },
    "remind_source_custom": {
        "en": "custom",
        "th": "กำหนดเอง",
    },
    "remind_source_default": {
        "en": "default",
        "th": "ค่าเริ่มต้น",
    },
    "remind_no_default_times": {
        "en": "no default times configured",
        "th": "ไม่มีเวลาเริ่มต้นที่ตั้งไว้",
    },
    "remind_invalid_time": {
        "en": '🤔 "{token}" isn\'t a valid time — use 24-hour HH:MM, e.g. "08:00".',
        "th": '🤔 "{token}" ไม่ใช่เวลาที่ถูกต้องนะ ใช้รูปแบบ HH:MM 24 ชั่วโมง เช่น "08:00"',
    },
    "remind_too_many_times": {
        "en": "🤔 That's too many times — please list at most {max} per habit.",
        "th": "🤔 ตั้งเวลาเยอะเกินไปนะ ใส่ได้สูงสุด {max} เวลาต่อกิจกรรม",
    },
    "remind_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "remind_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
}
