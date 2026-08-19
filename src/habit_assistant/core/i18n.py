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


def resolve_reply_language(inbound_text: str, config: "Config") -> Language:
    """AC6.3: `language` = "th"/"en" forces that language regardless of
    the inbound text; "auto" matches the detected language of the
    message being replied to (AC6.1)."""
    forced = config.i18n.language
    if forced in ("th", "en"):
        return forced
    return detect_language(inbound_text)


def resolve_unprompted_language(config: "Config") -> Language:
    """Same forced-language override as `resolve_reply_language`, but for
    sends with no triggering inbound message (reminders, health alerts,
    the weekly review): "auto" uses the configured primary language
    (default Thai) instead of detecting from anything."""
    forced = config.i18n.language
    if forced in ("th", "en"):
        return forced
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
        "en": "✅ {water_ml} ml logged — today {total} / {goal} ml ({pct}%)",
        "th": "✅ บันทึกน้ำ {water_ml} มล. แล้ว — วันนี้ดื่มไป {total} / {goal} มล. ({pct}%)",
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
        "en": "↩️ Undone — removed {description}. Today: {total} / {goal} ml ({pct}%)",
        "th": "↩️ ยกเลิกแล้ว — ลบ {description} วันนี้เหลือ {total} / {goal} มล. ({pct}%)",
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
        "en": "✏️ Updated to {value_num:g} ml — today {total} / {goal} ml ({pct}%)",
        "th": "✏️ แก้เป็น {value_num:g} มล. แล้ว — วันนี้ดื่มไป {total} / {goal} มล. ({pct}%)",
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
}
