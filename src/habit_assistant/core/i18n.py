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
    # SPEC-v1.10.md §4 "ARCHI-SANCTIONED EXTRAS" (b): the EN variant used to
    # omit {label} while TH always named the habit -- logging a custom
    # habit (e.g. "sleep") in English rendered "7 h logged", never saying
    # WHICH habit. Fixed to match confirm_duration's own EN word order
    # (value, unit, label, "logged"), which already included {label} and
    # is therefore the established in-app precedent for this sentence
    # shape, not a new invention. water/stretch/diary have their own
    # dedicated keys (water_confirmation/stretch_confirmation/
    # diary_confirmation) and are untouched by this fix.
    "confirm_numeric_goal": {
        "en": "✅ {value:g} {unit} {label} logged — today {total:g} / {goal:g} {unit} ({pct}%)",
        "th": "✅ บันทึก{label} {value:g} {unit} แล้ว — วันนี้ {total:g} / {goal:g} {unit} ({pct}%)",
    },
    "confirm_numeric_nogoal": {
        "en": "✅ {value:g} {unit} {label} logged today",
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

    # -----------------------------------------------------------------
    # SPEC-v1.3.md "Audit log" -- module `audit-view` (R-V1/R-V2,
    # core/audit_view.py): the owner-only `/audit [N]` recent-activity
    # viewer. `audit_line`'s {detail} (entity + old→new, already
    # humanized) is composed in PYTHON, not localized here -- same "raw
    # technical value, only the label/skeleton localized" convention
    # `core/access.py:_render_users_list`'s `users_list_line` already
    # established for this app's other owner-only technical view (role/
    # status shown verbatim, untranslated). `source` (command/nl/button/
    # admin) is likewise shown verbatim for the same reason. Every
    # `audit_action_*` id is one of `core/audit.py:ACTIONS`'s 18 values --
    # spelling can't drift because `core/audit_view.py` looks each one up
    # by the same literal.
    # -----------------------------------------------------------------
    "audit_header": {
        "en": "🧾 Recent activity (last {limit}):",
        "th": "🧾 กิจกรรมล่าสุด ({limit} รายการล่าสุด):",
    },
    "audit_empty": {
        "en": "🧾 No activity recorded yet.",
        "th": "🧾 ยังไม่มีการบันทึกกิจกรรมนะ",
    },
    "audit_line": {
        "en": "• {ts} · {actor} · {action} · {detail} ({source})",
        "th": "• {ts} · {actor} · {action} · {detail} ({source})",
    },
    # TEST-v1.3-view.md's finding: `core/audit_view.py:_fit_within_budget`'s
    # footer, appended only when the fully-rendered message would exceed
    # Telegram's `sendMessage` limit and the oldest shown rows had to be
    # dropped to fit -- {count} is how many rows were dropped.
    "audit_more_rows": {
        "en": "… {count} more",
        "th": "… อีก {count} รายการ",
    },
    "audit_actor_you": {
        "en": "you",
        "th": "คุณ",
    },
    "audit_action_undo": {
        "en": "undo",
        "th": "ยกเลิก",
    },
    "audit_action_edit": {
        "en": "edit",
        "th": "แก้ไข",
    },
    "audit_action_target_set": {
        "en": "target set",
        "th": "ตั้งเป้าหมาย",
    },
    "audit_action_target_clear": {
        "en": "target cleared",
        "th": "ล้างเป้าหมาย",
    },
    "audit_action_remind_set": {
        "en": "reminder times",
        "th": "เวลาแจ้งเตือน",
    },
    "audit_action_remind_off": {
        "en": "reminder off",
        "th": "ปิดแจ้งเตือน",
    },
    "audit_action_remind_default": {
        "en": "reminder reset",
        "th": "รีเซ็ตแจ้งเตือน",
    },
    "audit_action_lang_set": {
        "en": "language",
        "th": "ภาษา",
    },
    "audit_action_quiet_set": {
        "en": "quiet hours",
        "th": "ช่วงเวลาเงียบ",
    },
    "audit_action_quiet_off": {
        "en": "quiet hours off",
        "th": "ปิดช่วงเวลาเงียบ",
    },
    "audit_action_checkin_set": {
        "en": "check-in on",
        "th": "เปิดเช็คอิน",
    },
    "audit_action_checkin_off": {
        "en": "check-in off",
        "th": "ปิดเช็คอิน",
    },
    "audit_action_checkin_default": {
        "en": "check-in reset",
        "th": "รีเซ็ตเช็คอิน",
    },
    "audit_action_dashboard_set": {
        "en": "dashboard on",
        "th": "เปิดแดชบอร์ด",
    },
    "audit_action_dashboard_off": {
        "en": "dashboard off",
        "th": "ปิดแดชบอร์ด",
    },
    "audit_action_user_approve": {
        "en": "approved",
        "th": "อนุมัติ",
    },
    "audit_action_user_block": {
        "en": "blocked",
        "th": "บล็อก",
    },
    "audit_action_user_pending": {
        "en": "pending",
        "th": "รอดำเนินการ",
    },
    "audit_action_habit_create": {
        "en": "habit created",
        "th": "สร้างนิสัย",
    },
    "audit_action_habit_archive": {
        "en": "habit archived",
        "th": "เก็บนิสัยเข้าคลัง",
    },
    "audit_action_habit_delete": {
        "en": "habit deleted",
        "th": "ลบนิสัย",
    },
    # SPEC-v1.8.md R-S6 (shared surface, module `routines`' own dependency,
    # core/audit.py:ACTIONS' three new values).
    "audit_action_routine_create": {
        "en": "routine created",
        "th": "สร้างกิจวัตร",
    },
    "audit_action_routine_delete": {
        "en": "routine deleted",
        "th": "ลบกิจวัตร",
    },
    "audit_action_routine_run": {
        "en": "routine run",
        "th": "รันกิจวัตร",
    },
    # SPEC-v1.9.md §5/§6 (shared surface, core/audit.py:ACTIONS' five new
    # values -- modules `cadence`/`pause`/`grace` are each this vocabulary's
    # own later, disjoint writer).
    "audit_action_cadence_set": {
        "en": "cadence set",
        "th": "ตั้งความถี่รายสัปดาห์",
    },
    "audit_action_cadence_clear": {
        "en": "cadence cleared",
        "th": "ยกเลิกความถี่รายสัปดาห์",
    },
    "audit_action_pause_set": {
        "en": "paused",
        "th": "พัก",
    },
    "audit_action_pause_clear": {
        "en": "resumed",
        "th": "กลับมาทำต่อ",
    },
    "audit_action_grace_consumed": {
        "en": "grace used",
        "th": "ใช้สิทธิ์ผ่อนผัน",
    },

    # -----------------------------------------------------------------
    # Module `history` (/history, ย้อนหลัง -- SPEC-v1.4.md R-R1-R-R5,
    # core/history_view.py). Disjoint from every `audit_*` key above --
    # this module owns its own catalog block, same convention as every
    # other parallel/sequential module before it. `history_line`'s
    # `{undone}` is either "" (a live row) or the localized
    # `history_undone_marker` text (a soft-deleted row) -- the marker
    # itself carries its own leading spacing so `history_line`'s template
    # doesn't need a conditional space.
    # -----------------------------------------------------------------
    "history_header": {
        "en": "🧾 Your last {limit} entries:",
        "th": "🧾 {limit} รายการล่าสุดของคุณ:",
    },
    "history_header_filtered": {
        "en": "🧾 Your last {limit} {habit} entries:",
        "th": "🧾 {limit} รายการล่าสุดของ{habit}",
    },
    "history_line": {
        "en": '• {ts} · {description} · "{message}"{undone}',
        "th": '• {ts} · {description} · "{message}"{undone}',
    },
    "history_undone_marker": {
        "en": "  (undone)",
        "th": "  (ยกเลิกแล้ว)",
    },
    "history_empty": {
        "en": "🧾 No entries yet.",
        "th": "🧾 ยังไม่มีรายการนะ",
    },
    "history_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "history_more_rows": {
        "en": "… {count} more",
        "th": "… อีก {count} รายการ",
    },

    # -----------------------------------------------------------------
    # Module `checkins` (/checkin, เช็คอิน -- SPEC-v1.5.md §4 R-K1-R-K8,
    # core/checkins.py). Disjoint from every key above -- this module owns
    # its own catalog block, same convention as every other parallel/
    # sequential module before it. `/dnd`/`งดรบกวน` (R-D5) is a PURE alias
    # of `/quiet` -- it reuses `quiet_set`/`quiet_cleared`/`quiet_usage`/
    # `quiet_invalid_window`/`preferences_save_failed` verbatim (no new
    # keys needed for it at all).
    #
    # `checkin_set_on`/`checkin_set_off`/`checkin_set_window`/`checkin_show`
    # are SPEC-v1.5.md §3.2's own named ids. `/checkin default` deliberately
    # reuses `checkin_show`'s own two variants (via `_build_show_reply`,
    # `core/checkins.py`) rather than a fifth id -- the reply after a reset
    # is just "here's your current (now-inherited) effective state", the
    # same shape a bare `/checkin` already shows.
    # -----------------------------------------------------------------
    "checkin_set_on": {
        "en": "✅ Check-ins turned on for you — hourly nudges from {start} to {end}.",
        "th": "✅ เปิดเช็คอินให้คุณแล้ว — แจ้งเตือนทุกชั่วโมงตั้งแต่ {start} ถึง {end}",
    },
    "checkin_set_off": {
        "en": "🔕 Check-ins turned off for you.",
        "th": "🔕 ปิดเช็คอินให้คุณแล้ว",
    },
    "checkin_set_window": {
        "en": "✅ Check-in window set: {start}-{end}. Hourly nudges in that range.",
        "th": "✅ ตั้งช่วงเวลาเช็คอินแล้ว: {start}-{end} จะแจ้งเตือนทุกชั่วโมงในช่วงนี้",
    },
    "checkin_show": {
        "en": "🌤️ Check-ins: on, {start}-{end}.",
        "th": "🌤️ เช็คอิน: เปิดอยู่ {start}-{end}",
    },
    "checkin_show_off": {
        "en": "🌤️ Check-ins: off.",
        "th": "🌤️ เช็คอิน: ปิดอยู่",
    },
    "checkin_usage": {
        "en": (
            '🤔 Usage: "/checkin on", "/checkin off", "/checkin 09:00-18:00", '
            'or "/checkin default" (Thai: "เช็คอิน ...").'
        ),
        "th": (
            '🤔 วิธีใช้: "/checkin on", "/checkin off", "/checkin 09:00-18:00" '
            'หรือ "/checkin default" (หรือ "เช็คอิน ...")'
        ),
    },
    "checkin_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    # --- the hourly check-in message itself (core/checkins.py: R-K6) -----
    "checkin_header": {
        "en": "🌤️ Quick check-in",
        "th": "🌤️ เช็คอินด่วน",
    },
    "checkin_line_progress": {
        "en": "• {label}: {total:g} / {goal:g} {unit}",
        "th": "• {label}: {total:g} / {goal:g} {unit}",
    },
    "checkin_line_not_yet": {
        "en": "• {label}: not yet today",
        "th": "• {label}: ยังไม่ได้ทำวันนี้",
    },
    "checkin_invite": {
        "en": "Log anything you've done? 💬",
        "th": "ทำอะไรไปแล้วบ้าง อยากบันทึกไหม? 💬",
    },
    "checkin_generic_nudge": {
        "en": "🌤️ Quick check-in — log anything you've done today?",
        "th": "🌤️ เช็คอินด่วน — วันนี้ทำอะไรไปแล้วบ้าง อยากบันทึกไหม?",
    },
    # --- /help additions (SPEC-v1.5.md §11: "the check-in + DND lines,
    # opt-in framing" -- data only; wiring these two lines into
    # `core/discoverability.build_help_text` is an integration-time append,
    # same precedent as `help_lang`/`help_quiet_cmd`/`help_remind_cmd`'s own
    # documented "added after this module itself landed" note just above
    # in this file's `help_*` block) ---------------------------------------
    "help_checkin_cmd": {
        "en": (
            '🌤️ Get hourly check-in nudges (off by default): /checkin on (08:00-20:00), '
            '/checkin 09:00-18:00, or /checkin off (Thai: "เช็คอิน ...").'
        ),
        "th": (
            '🌤️ เปิดแจ้งเตือนเช็คอินรายชั่วโมง (ปิดโดยค่าเริ่มต้น): /checkin on (08:00-20:00), '
            '/checkin 09:00-18:00 หรือ /checkin off (หรือ "เช็คอิน ...")'
        ),
    },
    "help_dnd_cmd": {
        "en": '🌙 /dnd is another name for /quiet — same do-not-disturb hours (Thai: "งดรบกวน ...").',
        "th": '🌙 /dnd คือชื่ออีกชื่อของ /quiet — ตั้งช่วงเวลางดรบกวนแบบเดียวกัน (หรือ "งดรบกวน ...")',
    },

    # -----------------------------------------------------------------
    # Module `nudge` (SPEC-v1.6.md §4 Feature 5 "Almost there" end-of-day
    # nudge, R-N1-R-N3, core/nudge.py). Disjoint block, no command of its
    # own (OQ2: rides `/checkin` enablement) -- just the once/day message
    # body. `nudge_header` + one `nudge_line` per close-but-unmet habit are
    # joined into a SINGLE message (R-N2: at most one nudge/user/day, even
    # when several habits qualify simultaneously), mirroring `checkin_
    # header`/`checkin_line_progress`'s own header+bullet-per-habit shape.
    # -----------------------------------------------------------------
    "nudge_header": {
        "en": "🎯 So close today!",
        "th": "🎯 ใกล้ถึงเป้าหมายวันนี้แล้ว!",
    },
    "nudge_line": {
        "en": "• Just {remaining:g} {unit} to hit your {label} goal today — you've got this.",
        "th": "• อีกแค่ {remaining:g} {unit} ก็ถึงเป้าหมาย{label}วันนี้แล้ว สู้ๆ นะ",
    },

    # -----------------------------------------------------------------
    # Module `dashboard` (/dashboard, แดชบอร์ด -- SPEC-v1.6.md §4 Feature 1
    # R-D1-R-D6, core/dashboard.py). Disjoint from every key above -- this
    # module owns its own catalog block, same convention as every other
    # parallel module (mirrors `checkins`'s own block precedent exactly).
    # -----------------------------------------------------------------
    "dashboard_header": {
        "en": "📌 Today — {date}",
        "th": "📌 วันนี้ — {date}",
    },
    "dashboard_line_goal": {
        "en": "• {label}: {total:g} / {goal:g} {unit} {bar} {pct}% · streak {streak}d",
        "th": "• {label}: {total:g} / {goal:g} {unit} {bar} {pct}% · ต่อเนื่อง {streak} วัน",
    },
    "dashboard_line_boolean": {
        "en": "• {label}: {status} · streak {streak}d",
        "th": "• {label}: {status} · ต่อเนื่อง {streak} วัน",
    },
    "dashboard_line_count": {
        "en": "• {label}: {count:g} · streak {streak}d",
        "th": "• {label}: {count:g} · ต่อเนื่อง {streak} วัน",
    },
    "dashboard_set_on": {
        "en": "📌 Live dashboard turned on — I'll keep it pinned and updated as you log.",
        "th": "📌 เปิดแดชบอร์ดสดแล้ว — จะปักหมุดและอัปเดตให้อัตโนมัติเมื่อคุณบันทึก",
    },
    "dashboard_set_off": {
        "en": "🔕 Live dashboard turned off.",
        "th": "🔕 ปิดแดชบอร์ดสดแล้ว",
    },
    "dashboard_show_on": {
        "en": "📌 Live dashboard: on.",
        "th": "📌 แดชบอร์ดสด: เปิดอยู่",
    },
    "dashboard_show_off": {
        "en": '📌 Live dashboard: off. Turn it on with "/dashboard on".',
        "th": '📌 แดชบอร์ดสด: ปิดอยู่ เปิดได้ด้วย "/dashboard on"',
    },
    "dashboard_usage": {
        "en": '🤔 Usage: "/dashboard on" or "/dashboard off" (Thai: "แดชบอร์ด on/off").',
        "th": '🤔 วิธีใช้: "/dashboard on" หรือ "/dashboard off" (หรือ "แดชบอร์ด on/off")',
    },
    "dashboard_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "dashboard_unsupported": {
        "en": "📌 This chat can't pin messages, so I can't keep a live dashboard here.",
        "th": "📌 แชทนี้ปักหมุดข้อความไม่ได้ เลยเปิดแดชบอร์ดสดให้ไม่ได้",
    },
    # Gap-pass additions (TEST-v1.6-dashboard.md findings #1/#5, Archi-ruled
    # 2026-08-24): "on" while already on refreshes in place instead of a
    # second pin (finding #1); a large habit registry gets a truncated
    # board + this footer instead of an over-length message (finding #5,
    # `render_budget.fit_within_budget` -- same "… N more" shape as
    # `audit_more_rows`/`history_more_rows`).
    "dashboard_already_on": {
        "en": "📌 Live dashboard is already on — refreshed.",
        "th": "📌 แดชบอร์ดสดเปิดอยู่แล้ว — รีเฟรชให้แล้ว",
    },
    "dashboard_more_rows": {
        "en": "… {count} more",
        "th": "… อีก {count} รายการ",
    },
    # --- /help addition (integration-time append, same "data only, wiring
    # is a later step" posture as `help_checkin_cmd`/`help_dnd_cmd`/
    # `help_heatmap_cmd` -- `core/dashboard.py`'s own IMPL flagged this as
    # not in its scope; landed here as part of the integration pass) -----
    "help_dashboard_cmd": {
        "en": '📌 Get a live pinned "Today" board: /dashboard on, /dashboard off, or just /dashboard to check (Thai: "แดชบอร์ด on/off").',
        "th": '📌 เปิดแดชบอร์ดสด "วันนี้" แบบปักหมุด: /dashboard on, /dashboard off หรือพิมพ์ /dashboard เฉยๆ เพื่อดูสถานะ (หรือ "แดชบอร์ด on/off")',
    },

    # -----------------------------------------------------------------
    # Module `heatmap` (/heatmap, ปฏิทิน -- SPEC-v1.6.md §4 Feature 2,
    # R-H1-R-H4, core/heatmap.py). Disjoint block, own module. R-H3: the
    # PNG itself carries no bilingual text at all -- the caption keys below
    # (`heatmap_caption_*`) are what `send_image`'s `caption` argument
    # gets, and the fallback keys (`heatmap_fallback_*`) are the R-H2
    # "matplotlib unavailable / render failed" text reply. `{habit}` here
    # is always a `Habit.label(lang)` (bilingual), never `Habit.id` --
    # unlike the ASCII `id` this module's own PNG rows use internally, the
    # chat-facing caption/fallback text is exactly where the bilingual
    # label belongs (R-H3's own "bilingual label ... lives in the
    # caption, not the image").
    # -----------------------------------------------------------------
    "heatmap_caption_single": {
        "en": "📅 {habit} — consistency over the last {weeks} weeks.",
        "th": "📅 {habit} — ความสม่ำเสมอช่วง {weeks} สัปดาห์ที่ผ่านมา",
    },
    "heatmap_caption_all": {
        "en": "📅 Consistency heatmap — last {weeks} weeks. Top to bottom: {habit_list}.",
        "th": "📅 ปฏิทินความสม่ำเสมอ — {weeks} สัปดาห์ที่ผ่านมา เรียงจากบนลงล่าง: {habit_list}",
    },
    "heatmap_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "heatmap_no_habits": {
        "en": "📅 No habits configured yet, so there's nothing to show a heatmap for.",
        "th": "📅 ยังไม่มีนิสัยที่ตั้งค่าไว้ เลยยังไม่มีอะไรให้แสดงในปฏิทิน",
    },
    "heatmap_fallback_header": {
        "en": "📅 Charts aren't available right now — here's a quick summary (last {weeks} weeks):",
        "th": "📅 ตอนนี้ยังแสดงกราฟไม่ได้ — สรุปแบบย่อให้แทน ({weeks} สัปดาห์ที่ผ่านมา):",
    },
    "heatmap_fallback_line": {
        "en": "• {habit}: on track {qualifying}/{total} days",
        "th": "• {habit}: ทำได้ {qualifying}/{total} วัน",
    },
    # --- /help addition (integration-time append, same "data only, wiring
    # is a later step" posture as `help_checkin_cmd`/`help_dnd_cmd` above)
    "help_heatmap_cmd": {
        "en": '📅 See your consistency calendar: /heatmap, /heatmap water, or /heatmap water 8 (Thai: "ปฏิทิน ...").',
        "th": '📅 ดูปฏิทินความสม่ำเสมอ: /heatmap, /heatmap water หรือ /heatmap water 8 (หรือ "ปฏิทิน ...")',
    },

    # -----------------------------------------------------------------
    # Module `insights` (SPEC-v1.6.md §4 Features 3+4, R-R1-R-R4/R-T1-R-T3,
    # core/records.py + core/trends.py). Disjoint block, own module (records
    # + trends share one block since both are "history insight" and share
    # one file section, mirroring how `checkin`/`dnd` share `checkins.py`'s
    # own catalog block). `record_broken_*` are the R-R2 celebration-suffix
    # lines `core/records.py:format_celebration` renders (appended to a log
    # confirmation, once per broken record); `records_*` are the `/records`
    # view (R-R3); `trends_*` are the `/trends` view + weekly-review block
    # (R-T2).
    # -----------------------------------------------------------------
    "record_broken_best_day": {
        "en": "🎉 New personal best — {label} best day: {value:g} {unit}!",
        "th": "🎉 สถิติใหม่ — {label} วันที่ดีที่สุด: {value:g} {unit}!",
    },
    "record_broken_best_day_count": {
        "en": "🎉 New personal best — {label} best day: {count} entries!",
        "th": "🎉 สถิติใหม่ — {label} วันที่ดีที่สุด: {count} ครั้ง!",
    },
    "record_broken_best_week": {
        "en": "🎉 New personal best — {label} best week: {value:g} {unit}!",
        "th": "🎉 สถิติใหม่ — {label} สัปดาห์ที่ดีที่สุด: {value:g} {unit}!",
    },
    "record_broken_best_week_count": {
        "en": "🎉 New personal best — {label} best week: {count} entries!",
        "th": "🎉 สถิติใหม่ — {label} สัปดาห์ที่ดีที่สุด: {count} ครั้ง!",
    },
    "record_broken_longest_streak": {
        "en": "🎉 New personal best — longest {label} streak: {days} days!",
        "th": "🎉 สถิติใหม่ — ต่อเนื่อง{label}นานที่สุด: {days} วัน!",
    },
    "records_habit_header": {
        "en": "🏆 {habit} records",
        "th": "🏆 สถิติ{habit}",
    },
    "records_none_yet": {
        "en": "No records yet — keep logging to set your first one!",
        "th": "ยังไม่มีสถิติเลยนะ ลองบันทึกต่อไปเรื่อยๆ เดี๋ยวก็มีสถิติแรกของคุณ!",
    },
    "records_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "records_line_best_day": {
        "en": "• Best day: {value:g} {unit} ({achieved_on})",
        "th": "• วันที่ดีที่สุด: {value:g} {unit} ({achieved_on})",
    },
    "records_line_best_day_count": {
        "en": "• Best day: {count} entries ({achieved_on})",
        "th": "• วันที่ดีที่สุด: {count} ครั้ง ({achieved_on})",
    },
    "records_line_best_week": {
        "en": "• Best week: {value:g} {unit} ({achieved_on})",
        "th": "• สัปดาห์ที่ดีที่สุด: {value:g} {unit} ({achieved_on})",
    },
    "records_line_best_week_count": {
        "en": "• Best week: {count} entries ({achieved_on})",
        "th": "• สัปดาห์ที่ดีที่สุด: {count} ครั้ง ({achieved_on})",
    },
    "records_line_longest_streak": {
        "en": "• Longest streak: {days} days ({achieved_on})",
        "th": "• ต่อเนื่องนานที่สุด: {days} วัน ({achieved_on})",
    },
    "records_more_habits": {
        "en": "… {count} more",
        "th": "… อีก {count} รายการ",
    },
    "records_render_failed": {
        "en": "😥 Couldn't load your records right now — please try again in a moment.",
        "th": "😥 ตอนนี้โหลดสถิติไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "trends_line": {
        "en": "📊 {label} — this week vs last: {previous:g} → {current:g} {unit} ({pct:+d}%)",
        "th": "📊 {label} — สัปดาห์นี้เทียบสัปดาห์ที่แล้ว: {previous:g} → {current:g} {unit} ({pct:+d}%)",
    },
    "trends_line_count": {
        "en": "📊 {label} — this week vs last: {previous:g} → {current:g} entries ({pct:+d}%)",
        "th": "📊 {label} — สัปดาห์นี้เทียบสัปดาห์ที่แล้ว: {previous:g} → {current:g} ครั้ง ({pct:+d}%)",
    },
    "trends_line_no_pct": {
        "en": "📊 {label} — this week vs last: {previous:g} → {current:g} {unit}",
        "th": "📊 {label} — สัปดาห์นี้เทียบสัปดาห์ที่แล้ว: {previous:g} → {current:g} {unit}",
    },
    "trends_line_no_pct_count": {
        "en": "📊 {label} — this week vs last: {previous:g} → {current:g} entries",
        "th": "📊 {label} — สัปดาห์นี้เทียบสัปดาห์ที่แล้ว: {previous:g} → {current:g} ครั้ง",
    },
    "trends_line_no_history": {
        "en": "📊 {label} — not enough history yet.",
        "th": "📊 {label} — ยังมีข้อมูลไม่พอนะ",
    },
    "trends_rising_suffix": {
        "en": "{weeks} weeks rising 📈",
        "th": "ขึ้นต่อเนื่อง {weeks} สัปดาห์ 📈",
    },
    "trends_falling_suffix": {
        "en": "{weeks} weeks falling 📉",
        "th": "ลดลงต่อเนื่อง {weeks} สัปดาห์ 📉",
    },
    "trends_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "trends_more_habits": {
        "en": "… {count} more",
        "th": "… อีก {count} รายการ",
    },
    "trends_render_failed": {
        "en": "😥 Couldn't load your trends right now — please try again in a moment.",
        "th": "😥 ตอนนี้โหลดแนวโน้มไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "trends_review_header": {
        "en": "📊 Trends",
        "th": "📊 แนวโน้ม",
    },
    # --- /help additions (integration-time append, same "data only,
    # wiring is a later step" posture as `help_checkin_cmd`/
    # `help_heatmap_cmd` above) -------------------------------------------
    "help_records_cmd": {
        "en": '🏆 See your personal bests: /records or /records water (Thai: "สถิติ ...").',
        "th": '🏆 ดูสถิติส่วนตัวของคุณ: /records หรือ /records water (หรือ "สถิติ ...")',
    },
    "help_trends_cmd": {
        "en": '📊 See week-over-week trends: /trends or /trends water (Thai: "แนวโน้ม ...").',
        "th": '📊 ดูแนวโน้มรายสัปดาห์: /trends หรือ /trends water (หรือ "แนวโน้ม ...")',
    },

    # ===================================================================
    # SPEC-v1.7.md "Per-user custom habits" -- shared-surface key-block
    # skeleton (§11: "the i18n key-block skeletons are created in the
    # shared surface first; each module then fills only its own disjoint
    # keys", same convention as v1.2's own skeleton markers above). This
    # section is reserved for module `habitdef`'s own `/addhabit`/
    # `/delhabit` copy (confirmations, validation errors, usage replies) --
    # no key is added here yet, only the section marker. `habitdef`'s own
    # keys must use an `addhabit_*`/`delhabit_*` prefix (disjoint from
    # every existing key-name prefix in this file) so its later edit here
    # never collides with this shared-surface pass's own additions above.
    # ===================================================================
    "help_addhabit_cmd": {
        "en": '➕ Add your own habit: /addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30 (Thai alias: "เพิ่มนิสัย ...").',
        "th": '➕ เพิ่มนิสัยของคุณเอง: /addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30 (หรือ "เพิ่มนิสัย ...")',
    },
    "help_delhabit_cmd": {
        "en": '➖ Remove a habit you created: /delhabit reading (Thai alias: "ลบนิสัย reading").',
        "th": '➖ ลบนิสัยที่คุณสร้างไว้: /delhabit reading (หรือ "ลบนิสัย reading")',
    },

    # --- /addhabit / /delhabit (module `habitdef`, SPEC-v1.7.md R-C1/R-C2/
    # R-V1-R-V5) -- `core/habitdef.py` composes `detail`/`example`/`label`/
    # `other_label` before calling `i18n.t` with the templates below; see
    # its own module docstring for the composition logic. ------------------
    "addhabit_usage": {
        "en": (
            "🤔 Usage: /addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30 "
            '(id, type, en required; th/unit/goal/alias optional; Thai alias: "เพิ่มนิสัย ...").'
        ),
        "th": (
            "🤔 รูปแบบ: /addhabit id=reading|type=duration|en=reading|th=อ่านหนังสือ|unit=min/นาที|goal=30 "
            '(ต้องมี id, type, en — th/unit/goal/alias จะใส่หรือไม่ใส่ก็ได้ หรือใช้ "เพิ่มนิสัย ...")'
        ),
    },
    "addhabit_reserved_word": {
        "en": '🤔 Couldn\'t add that: "{word}" can\'t be a habit name (it\'s a command). Try another name.',
        "th": '🤔 เพิ่มไม่ได้นะ: "{word}" ใช้เป็นชื่อนิสัยไม่ได้ (เป็นคำสั่งอยู่แล้ว) ลองชื่ออื่นดู',
    },
    "addhabit_invalid_id": {
        "en": '🤔 "{id}" isn\'t a valid id — use only lowercase letters, numbers, and underscores (max 32 characters).',
        "th": '🤔 "{id}" ใช้เป็น id ไม่ได้ — ใช้ได้แค่ตัวอักษรพิมพ์เล็ก ตัวเลข และ _ (ไม่เกิน 32 ตัวอักษร)',
    },
    "addhabit_shadow_base": {
        "en": '🤔 Couldn\'t add that: "{id}" is already one of my built-in habits. Try another id.',
        "th": '🤔 เพิ่มไม่ได้นะ: "{id}" เป็นนิสัยพื้นฐานของฉันอยู่แล้ว ลอง id อื่นดู',
    },
    "addhabit_duplicate_id": {
        "en": '🤔 You already have a habit called "{id}". Use /delhabit {id} first, or pick another id.',
        "th": '🤔 คุณมีนิสัยชื่อ "{id}" อยู่แล้ว ลบด้วย /delhabit {id} ก่อน หรือเลือก id อื่น',
    },
    "addhabit_archived_id": {
        "en": '🤔 "{id}" was used before and its history is kept, so the id stays reserved. Pick another id.',
        "th": '🤔 "{id}" เคยใช้มาก่อนและยังเก็บประวัติไว้ id นี้จึงถูกจองไว้ ลอง id อื่นดู',
    },
    "addhabit_invalid_type": {
        "en": '🤔 "{type}" isn\'t a valid type — use numeric, duration, text, or boolean.',
        "th": '🤔 "{type}" ไม่ใช่ประเภทที่ใช้ได้ — ใช้ numeric, duration, text หรือ boolean',
    },
    "addhabit_missing_unit": {
        "en": "🤔 A numeric/duration habit needs a unit — e.g. unit=min/นาที.",
        "th": "🤔 นิสัยแบบ numeric/duration ต้องมีหน่วย — เช่น unit=min/นาที",
    },
    "addhabit_unexpected_unit": {
        "en": "🤔 A text/boolean habit can't have a unit — remove the unit=... part.",
        "th": "🤔 นิสัยแบบ text/boolean มีหน่วยไม่ได้ — เอา unit=... ออก",
    },
    "addhabit_invalid_goal": {
        "en": "🤔 goal must be a positive number, and only applies to numeric/duration habits.",
        "th": "🤔 goal ต้องเป็นตัวเลขบวก และใช้ได้เฉพาะนิสัยแบบ numeric/duration",
    },
    "addhabit_invalid_alias": {
        "en": "🤔 alias must look like alias=token:multiplier,... — e.g. alias=page:1.",
        "th": "🤔 alias ต้องอยู่ในรูป alias=token:multiplier,... — เช่น alias=page:1",
    },
    "addhabit_duplicate_label": {
        "en": '🤔 You already have a habit labeled "{label}" — pick a different label.',
        "th": '🤔 คุณมีนิสัยชื่อ "{label}" อยู่แล้ว — ลองใช้ชื่ออื่น',
    },
    "addhabit_cap_reached": {
        "en": "🤔 You've reached the {cap}-habit limit for custom habits. Archive or remove one first with /delhabit.",
        "th": "🤔 คุณสร้างนิสัยครบ {cap} รายการแล้ว ลบหรือเก็บเข้าคลังก่อนด้วย /delhabit",
    },
    "addhabit_save_failed": {
        "en": "😥 Couldn't save that habit right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกนิสัยไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "addhabit_detail_goal": {
        "en": "{kind} in {unit}, goal {goal:g}/day",
        "th": "{kind} หน่วย {unit} เป้าหมาย {goal:g}/วัน",
    },
    "addhabit_detail_no_goal": {
        "en": "{kind} in {unit}",
        "th": "{kind} หน่วย {unit}",
    },
    "addhabit_detail_bare": {
        "en": "{kind}",
        "th": "{kind}",
    },
    "addhabit_success": {
        "en": '✅ Added "{label}" ({other_label}) — {detail}. Log it like "{example}" or use /remind {id}.',
        "th": '✅ เพิ่ม "{label}" ({other_label}) แล้ว — {detail} บันทึกได้แบบ "{example}" หรือใช้ /remind {id}',
    },
    "addhabit_success_bare": {
        "en": '✅ Added "{label}" ({other_label}) — {detail}. Log it anytime, or use /remind {id}.',
        "th": '✅ เพิ่ม "{label}" ({other_label}) แล้ว — {detail} บันทึกได้ทุกเมื่อ หรือใช้ /remind {id}',
    },
    "delhabit_usage": {
        "en": '🤔 Usage: /delhabit <id> — e.g. /delhabit reading (Thai alias: "ลบนิสัย reading").',
        "th": '🤔 รูปแบบ: /delhabit <id> — เช่น /delhabit reading (หรือ "ลบนิสัย reading")',
    },
    "delhabit_not_found": {
        "en": '🤔 You don\'t have a custom habit called "{id}".',
        "th": '🤔 คุณไม่มีนิสัยที่สร้างเองชื่อ "{id}"',
    },
    "delhabit_save_failed": {
        "en": "😥 Couldn't remove that habit right now — please try again in a moment.",
        "th": "😥 ตอนนี้ลบนิสัยไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "delhabit_archived": {
        "en": '🗄️ Archived "{id}" — it\'s hidden now, but your past entries stay in /history.',
        "th": '🗄️ เก็บ "{id}" เข้าคลังแล้ว — ซ่อนไว้แล้วนะ แต่รายการเก่ายังอยู่ใน /history',
    },
    "delhabit_deleted": {
        "en": '🗑️ Removed "{id}".',
        "th": '🗑️ ลบ "{id}" แล้ว',
    },

    # ===================================================================
    # SPEC-v1.8.md "One-tap quick-log keyboard + reactions, routines,
    # backfill, gentle riders" -- shared-surface key-block skeletons (§11:
    # "the i18n key-block skeletons are created in the shared surface
    # first; each module then fills only its own disjoint keys", same
    # convention as SPEC-v1.7.md's own skeleton markers above). No key is
    # added under any of the four markers below yet, only the section
    # marker + the reserved key-name prefix each module must use, so a
    # later module edit here can never collide with another parallel
    # module's own addition. Module `riders` (silent sends/owner-scoped
    # menus/the `/audit` language fix) owns no new user-facing copy at all
    # -- no marker needed for it.
    #
    # Module `quicklog` (R-Q1-R-Q6): the /log inline keyboard (button
    # labels, the "nothing to quick-log yet" hint) and reaction path (which
    # has no user-facing copy of its own -- `core/reactions.py` never calls
    # `i18n.t`). Keys must use a `quicklog_*` prefix.
    "quicklog_prompt": {
        "en": "👇 Tap to log:",
        "th": "👇 แตะเพื่อบันทึก:",
    },
    "quicklog_empty": {
        "en": "🤔 Nothing to quick-log yet — add a habit with /addhabit first.",
        "th": "🤔 ยังไม่มีอะไรให้บันทึกด่วนนะ ลองเพิ่มนิสัยด้วย /addhabit ก่อน",
    },
    "quicklog_done_button": {
        "en": "done ✓",
        "th": "เสร็จแล้ว ✓",
    },
    "quicklog_unknown_habit": {
        "en": "🤷 That's not one of your habits.",
        "th": "🤷 นี่ไม่ใช่นิสัยของคุณนะ",
    },
    # --- /help addition (v1.8.1 gap-fix, integration-time append, same
    # "data only, wiring is a later step" posture as `help_dashboard_cmd`/
    # `help_heatmap_cmd`/`help_addhabit_cmd` above -- `core/quicklog.py`'s
    # own IMPL never claimed `core/discoverability.py`'s file ownership) --
    "help_log_cmd": {
        "en": '👇 One-tap quick-log: /log pops a keyboard of your habits, tap once to log (Thai: "บันทึก").',
        "th": '👇 บันทึกด่วนแบบแตะเดียว: พิมพ์ /log เพื่อเปิดปุ่มนิสัยของคุณ แตะครั้งเดียวก็บันทึกได้ (หรือ "บันทึก")',
    },
    #
    # Module `routines` (R-R1-R-R6): /routine create/list/run/delete
    # confirmations + validation errors (§3.3's own sample copy). Keys must
    # use a `routine_*` prefix -- disjoint from `audit_action_routine_*`
    # above (a different key-name region entirely).
    "routine_create_usage": {
        "en": "🤔 To create a routine: /routine <name> = <habit> <value>[, <habit> <value> ...]\n"
        "Example: /routine morning = water 500, stretch 10",
        "th": "🤔 สร้างกิจวัตรแบบนี้: /routine <ชื่อ> = <นิสัย> <ค่า>[, <นิสัย> <ค่า> ...]\n"
        "ตัวอย่าง: /routine morning = water 500, stretch 10",
    },
    "routine_invalid_name": {
        "en": "🤔 Couldn't save that: routine names can only use lowercase letters, numbers, and _ (up to 32 characters).",
        "th": "🤔 บันทึกไม่ได้: ชื่อกิจวัตรใช้ได้เฉพาะตัวพิมพ์เล็ก ตัวเลข และ _ (ไม่เกิน 32 ตัวอักษร)",
    },
    "routine_name_taken": {
        "en": '🤔 You already have a routine named "{name}". Delete it first with /routine delete {name}.',
        "th": '🤔 คุณมีกิจวัตรชื่อ "{name}" อยู่แล้ว ลบก่อนด้วย /routine delete {name}',
    },
    "routine_cap_reached": {
        "en": "🤔 You've reached your limit of {cap} routines. Delete one with /routine delete <name> to make room.",
        "th": "🤔 คุณสร้างกิจวัตรครบ {cap} รายการแล้ว ลบสักรายการด้วย /routine delete <ชื่อ> เพื่อเพิ่มที่ว่าง",
    },
    "routine_invalid_habit": {
        "en": '🤔 Couldn\'t save that: "{token}" isn\'t one of your habits. Use /habits to see them.',
        "th": '🤔 บันทึกไม่ได้: "{token}" ไม่ใช่นิสัยของคุณ ดูรายการได้ที่ /habits',
    },
    "routine_invalid_value": {
        "en": '🤔 Couldn\'t save that: couldn\'t understand the value for "{habit}" ("{value}").',
        "th": '🤔 บันทึกไม่ได้: เข้าใจค่าของ "{habit}" ไม่ได้ ("{value}")',
    },
    "routine_save_failed": {
        "en": "😥 Couldn't save that right now. Try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "routine_create_success": {
        "en": '✅ Saved routine "{name}": {items}. Run it with /routine {name}.',
        "th": '✅ บันทึกกิจวัตร "{name}" แล้ว: {items} รันได้ด้วย /routine {name}',
    },
    "routine_list_empty": {
        "en": "You don't have any routines yet. Create one: /routine <name> = <habit> <value>[, ...]",
        "th": "คุณยังไม่มีกิจวัตร สร้างได้ด้วย /routine <ชื่อ> = <นิสัย> <ค่า>[, ...]",
    },
    "routine_list_header": {
        "en": "📋 Your routines:",
        "th": "📋 กิจวัตรของคุณ:",
    },
    "routine_list_item": {
        "en": '• "{name}": {items}',
        "th": '• "{name}": {items}',
    },
    "routine_run_button_label": {
        "en": "▶️ {name}",
        "th": "▶️ {name}",
    },
    "routine_list_more": {
        "en": "…and {count} more routine(s). Use /routine <name> to run one.",
        "th": "…และอีก {count} กิจวัตร ใช้ /routine <ชื่อ> เพื่อรัน",
    },
    "routine_run_usage": {
        "en": "🤔 Usage: /routine <name>",
        "th": "🤔 วิธีใช้: /routine <ชื่อ>",
    },
    "routine_run_not_found": {
        "en": '🤔 No routine named "{name}". Use /routine to see yours.',
        "th": '🤔 ไม่พบกิจวัตรชื่อ "{name}" ดูรายการได้ที่ /routine',
    },
    "routine_run_summary_full": {
        "en": "▶️ {name} — logged {items} ({count} of {total}).",
        "th": "▶️ {name} — บันทึกแล้ว {items} ({count} จาก {total})",
    },
    "routine_run_summary_partial": {
        "en": "▶️ {name} — logged {items} ({count} of {total}). Skipped: {skipped}.",
        "th": "▶️ {name} — บันทึกแล้ว {items} ({count} จาก {total}) ข้าม: {skipped}",
    },
    "routine_run_nothing": {
        "en": "▶️ {name} — nothing to log.",
        "th": "▶️ {name} — ไม่มีอะไรให้บันทึก",
    },
    "routine_run_nothing_skipped": {
        "en": "▶️ {name} — nothing to log. Skipped: {skipped}.",
        "th": "▶️ {name} — ไม่มีอะไรให้บันทึก ข้าม: {skipped}",
    },
    "routine_skip_removed": {
        "en": "{habit} (removed)",
        "th": "{habit} (ถูกลบแล้ว)",
    },
    "routine_skip_text": {
        "en": "{habit} (can't auto-log text)",
        "th": "{habit} (บันทึกข้อความอัตโนมัติไม่ได้)",
    },
    "routine_delete_usage": {
        "en": "🤔 Usage: /routine delete <name>",
        "th": "🤔 วิธีใช้: /routine delete <ชื่อ>",
    },
    "routine_delete_not_found": {
        "en": '🤔 No routine named "{name}" to delete.',
        "th": '🤔 ไม่พบกิจวัตรชื่อ "{name}" ให้ลบ',
    },
    "routine_delete_success": {
        "en": '🗑️ Deleted routine "{name}".',
        "th": '🗑️ ลบกิจวัตร "{name}" แล้ว',
    },
    # --- /help addition (v1.8.1 gap-fix, integration-time append, same
    # posture as `help_log_cmd` above) -------------------------------------
    "help_routine_cmd": {
        "en": (
            '📋 Bundle a habit stack into one command: /routine morning = water 500, stretch 10, '
            'then /routine morning to run it (Thai: "กิจวัตร ...").'
        ),
        "th": (
            '📋 รวมชุดนิสัยไว้ในคำสั่งเดียว: /routine morning = water 500, stretch 10 '
            'แล้วพิมพ์ /routine morning เพื่อรัน (หรือ "กิจวัตร ...")'
        ),
    },
    #
    # Module `backfill` (R-B1-R-B6): the backdated-log confirmation prefix
    # and the future/too-old bounds errors (§3.4). Keys must use a
    # `backfill_*` prefix.
    # -----------------------------------------------------------------
    "backfill_confirmation_prefix": {
        "en": "📅 Logged for {day} — ",
        "th": "📅 บันทึกสำหรับ {day} — ",
    },
    "backfill_error_future": {
        "en": "🤔 That date is in the future — I can only backfill up to {max_days} day(s) back.",
        "th": "🤔 วันที่นั้นเป็นอนาคตนะ ย้อนหลังบันทึกได้สูงสุด {max_days} วันเท่านั้น",
    },
    "backfill_error_too_old": {
        "en": "🤔 That's too far back — I can only backfill up to {max_days} day(s).",
        "th": "🤔 ย้อนหลังไปไกลเกินไปนะ บันทึกย้อนหลังได้สูงสุด {max_days} วัน",
    },
    # --- /help addition (v1.8.1 gap-fix, integration-time append, same
    # posture as `help_log_cmd`/`help_routine_cmd` above): a capability
    # line, not a command -- `{max_days}` is read live from
    # `config.backfill.max_days_back`, same "never hard-coded" rule as
    # `help_snooze`'s `{minutes}` (SPEC-v1.1.md AC36) -------------------
    "help_backfill": {
        "en": (
            '📅 Log for a past day: add "yesterday", "3 days ago", or "on Monday" to your log '
            "— up to {max_days} day(s) back."
        ),
        "th": (
            '📅 บันทึกย้อนหลังได้: เติม "เมื่อวาน", "3 วันที่แล้ว" หรือ "วันจันทร์" ต่อท้ายข้อความบันทึก '
            "— ย้อนหลังได้สูงสุด {max_days} วัน"
        ),
    },

    # ===================================================================
    # SPEC-v1.9.md "Life happens" (streak-engine rework) + Recap wrapped
    # card -- shared-surface key-block skeletons (§11: "the i18n
    # key-block skeletons are created in the shared surface first; each
    # module then fills only its own disjoint keys", same convention as
    # SPEC-v1.7.md's/SPEC-v1.8.md's own skeleton markers above). No key is
    # added under any of the four markers below yet, only the section
    # marker + the reserved key-name prefix each module must use, so a
    # later module edit here can never collide with another parallel
    # module's own addition. The five `audit_action_*` labels these
    # modules depend on are already filled above (shared-surface owned,
    # not a module concern) -- see the SPEC-v1.9.md block just after
    # `audit_action_routine_run`.
    #
    # Module `cadence` (R18-R20): `/cadence <habit> <N>|off` confirmations
    # + validation errors (§3's own sample copy: "✅ gym is now 3×/week..."),
    # the "/habits"/dashboard "X of N this week" indicator, and
    # `/addhabit ... | cadence=<N>w`'s own `addhabit_invalid_cadence`
    # error. Keys must use a `cadence_*` prefix.
    "cadence_set": {
        "en": "✅ {label} is now {n}×/week. This week: {done} of {n} ✅",
        "th": "✅ {label}เป็นเป้าหมาย {n} ครั้ง/สัปดาห์แล้ว สัปดาห์นี้: {done} จาก {n} ✅",
    },
    "cadence_cleared": {
        "en": "↩️ {label}'s weekly cadence is off — back to a daily streak.",
        "th": "↩️ ปิดเป้าหมายรายสัปดาห์ของ{label}แล้ว — กลับไปนับสตรีครายวันตามเดิม",
    },
    "cadence_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "cadence_invalid_value": {
        "en": '🤔 A weekly cadence has to be a whole number from 1 to {max} times/week, e.g. "/cadence {habit_id} 3".',
        "th": '🤔 เป้าหมายรายสัปดาห์ต้องเป็นจำนวนเต็มตั้งแต่ 1 ถึง {max} ครั้ง/สัปดาห์ เช่น "/cadence {habit_id} 3"',
    },
    "cadence_usage": {
        "en": '🤔 Usage: "/cadence <habit> <N>" (1-7 times/week) to set, or "/cadence <habit> off" to clear it.',
        "th": '🤔 วิธีใช้: "/cadence <กิจกรรม> <จำนวน>" (1-7 ครั้ง/สัปดาห์) เพื่อตั้ง หรือ "/cadence <กิจกรรม> off" เพื่อปิด',
    },
    "cadence_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "cadence_status_line": {
        "en": "🗓 {label} — {n}×/week · this week {done} of {n}{check}",
        "th": "🗓 {label} — {n} ครั้ง/สัปดาห์ · สัปดาห์นี้ {done} จาก {n}{check}",
    },
    "addhabit_invalid_cadence": {
        "en": '🤔 cadence has to be like "3w" — a whole number from 1 to {max} times/week.',
        "th": '🤔 cadence ต้องอยู่ในรูปแบบ "3w" — จำนวนเต็มตั้งแต่ 1 ถึง {max} ครั้ง/สัปดาห์',
    },
    #
    # Module `grace` (R8-R11): the one-time kind grace-consumed message
    # (§3's own sample: "🛟 No worries — I used your grace day..."), and the
    # `/habits` grace-balance line ("available this week" / "used Tue").
    # Keys must use a `grace_*` prefix.
    "grace_message_line": {
        "en": "🛟 No worries — I used your grace day for {label}, so your {streak}-day streak is safe. (one grace per week)",
        "th": "🛟 ไม่ต้องห่วงนะ — ฉันใช้สิทธิ์ผ่อนผันของคุณให้{label}แล้ว สตรีค {streak} วันของคุณยังปลอดภัย (ผ่อนผันได้สัปดาห์ละครั้ง)",
    },
    "grace_status_available": {
        "en": "🛟 grace: available this week",
        "th": "🛟 สิทธิ์ผ่อนผัน: ยังใช้ได้สัปดาห์นี้",
    },
    "grace_status_used": {
        "en": "🛟 grace: used {weekday} (streak protected)",
        "th": "🛟 สิทธิ์ผ่อนผัน: ใช้ไปแล้วเมื่อ {weekday} (สตรีคได้รับการปกป้อง)",
    },
    #
    # Module `pause` (R12-R17): `/pause`/`/resume` confirmations +
    # validation errors (`pause_invalid_habit`/`pause_invalid_date`/
    # `pause_too_long`/`pause_none_active`), and the `/dashboard`/`/habits`
    # "⏸ paused until <date>" marker. Keys must use a `pause_*` prefix.
    "pause_invalid_habit": {
        "en": '🤔 "{habit_id}" isn\'t a habit I track. I track: {habit_list}.',
        "th": '🤔 "{habit_id}" ไม่ใช่สิ่งที่ติดตามอยู่นะ ตอนนี้ติดตาม: {habit_list}',
    },
    "pause_invalid_date": {
        "en": '🤔 That date has to be today or later, and look like "until 2026-09-01" or "until monday".',
        "th": '🤔 วันที่ต้องเป็นวันนี้หรือหลังจากนี้ และมีรูปแบบ "until 2026-09-01" หรือ "until monday"',
    },
    "pause_too_long": {
        "en": '🤔 A pause can be at most {max_days} days. Try a shorter duration, like "{max_days}d".',
        "th": '🤔 พักได้สูงสุด {max_days} วัน ลองสั้นลง เช่น "{max_days}d"',
    },
    "pause_usage": {
        "en": '🤔 Try "/pause [habit] 5d" or "/pause [habit] until 2026-09-01". No habit = pauses everything.',
        "th": '🤔 ลองพิมพ์ "/pause [นิสัย] 5d" หรือ "/pause [นิสัย] until 2026-09-01" ถ้าไม่ระบุนิสัย จะพักทุกอย่าง',
    },
    "pause_save_failed": {
        "en": "😥 Couldn't save that right now — please try again in a moment.",
        "th": "😥 ตอนนี้บันทึกไม่ได้ ลองใหม่อีกครั้งนะ",
    },
    "pause_set_habit": {
        "en": "⏸ Paused {label} until {date}. Reminders muted, streak held. /resume {label} to end early.",
        "th": "⏸ พัก{label}จนถึง {date} แล้ว การแจ้งเตือนจะเงียบและสตรีคจะถูกคงไว้ พิมพ์ /resume {label} เพื่อกลับมาก่อนกำหนด",
    },
    "pause_set_all": {
        "en": "⏸ Paused everything until {date}. Reminders muted, streaks held. /resume to end early.",
        "th": "⏸ พักทุกอย่างจนถึง {date} แล้ว การแจ้งเตือนจะเงียบและสตรีคจะถูกคงไว้ พิมพ์ /resume เพื่อกลับมาก่อนกำหนด",
    },
    "pause_status_none": {
        "en": "✅ Nothing is paused right now.",
        "th": "✅ ตอนนี้ไม่มีอะไรถูกพักอยู่",
    },
    "pause_status_header": {
        "en": "⏸ Active pauses:",
        "th": "⏸ กำลังพักอยู่:",
    },
    "pause_status_none_habit": {
        "en": "✅ {label} isn't paused right now.",
        "th": "✅ {label}ไม่ได้ถูกพักอยู่ตอนนี้",
    },
    "pause_status_habit_header": {
        "en": "⏸ {label} is paused:",
        "th": "⏸ {label}กำลังพักอยู่:",
    },
    "pause_status_line": {
        "en": "• {target} — until {date}",
        "th": "• {target} — จนถึง {date}",
    },
    "pause_status_all_target": {
        "en": "everything",
        "th": "ทุกอย่าง",
    },
    "pause_resumed_habit": {
        "en": "▶ Resumed {label}. Welcome back!",
        "th": "▶ กลับมา{label}แล้ว ยินดีต้อนรับกลับมานะ!",
    },
    "pause_resumed_all": {
        "en": "▶ Resumed everything. Welcome back!",
        "th": "▶ กลับมาทุกอย่างแล้ว ยินดีต้อนรับกลับมานะ!",
    },
    "pause_none_active_habit": {
        "en": "🤷 {label} isn't paused, so there's nothing to resume.",
        "th": "🤷 {label}ไม่ได้ถูกพักอยู่ เลยไม่มีอะไรให้กลับมา",
    },
    # Archi ruling 2 (v1.9.0 round-2 fix, TEST-v1.9-pause.md finding 2):
    # `/resume <habit>` against an all-habits pause finds no HABIT-scoped
    # row to clear (R13's literal reading, unchanged), but the reply must
    # stay truthful -- {label} is in fact still paused via the untouched
    # all-habits row, so this key (not `pause_none_active_habit`, which
    # keeps its own wording for the genuinely-not-paused-at-all case) is
    # used whenever `is_paused(...)` is still true for that habit.
    "pause_covered_by_all": {
        "en": "🤷 {label} is covered by your pause-all until {date} — use /resume (no habit) to end it.",
        "th": "🤷 {label}ยังถูกพักอยู่จากคำสั่งพักทั้งหมดจนถึง {date} — พิมพ์ /resume (ไม่ระบุนิสัย) เพื่อกลับมา",
    },
    "pause_none_active_all": {
        "en": "🤷 Nothing is paused, so there's nothing to resume.",
        "th": "🤷 ไม่มีอะไรถูกพักอยู่ เลยไม่มีอะไรให้กลับมา",
    },
    #
    # Module `wrapped` (R21-R26, + shared-surface font mechanism): the
    # `/wrapped`/`/recap` PNG caption, its bilingual text fallback (mirrors
    # `heatmap_fallback_*`), and `wrapped_error_*` for a render failure.
    # Keys must use a `wrapped_*` prefix. The celebration emoji-burst
    # itself (R25, `celebration_burst`) is a plain string constant, not an
    # i18n catalog entry -- no key needed for it here.
    # ===================================================================
    "wrapped_no_habits": {
        "en": "🎉 No habits configured yet, so there's nothing to recap.",
        "th": "🎉 ยังไม่มีนิสัยที่ตั้งค่าไว้ เลยยังไม่มีอะไรให้สรุป",
    },
    "wrapped_title": {
        "en": "🎉 Your Recap",
        "th": "🎉 สรุปของคุณ",
    },
    "wrapped_period_4w": {
        "en": "Last 4 weeks",
        "th": "4 สัปดาห์ที่ผ่านมา",
    },
    "wrapped_period_month": {
        "en": "{month}",
        "th": "เดือน {month}",
    },
    "wrapped_empty_period": {
        "en": "📭 No logs yet this period — let's get started!",
        "th": "📭 ยังไม่มีบันทึกในช่วงนี้ — เริ่มกันเลย!",
    },
    "wrapped_best_day_none": {
        "en": "–",
        "th": "–",
    },
    "wrapped_count_total": {
        "en": "{count} entries",
        "th": "{count} ครั้ง",
    },
    "wrapped_streak_days": {
        "en": "streak {count}d",
        "th": "ต่อเนื่อง {count} วัน",
    },
    "wrapped_streak_weeks": {
        "en": "streak {count}w",
        "th": "ต่อเนื่อง {count} สัปดาห์",
    },
    "wrapped_habit_line": {
        "en": "{label}: {total} · best day {best_day} · {streak}",
        "th": "{label}: {total} · วันที่ดีที่สุด {best_day} · {streak}",
    },
    "wrapped_biggest_mover_pct": {
        "en": "📈 Biggest mover: {label} {sign}{pct}% vs last week",
        "th": "📈 เปลี่ยนแปลงมากที่สุด: {label} {sign}{pct}% เทียบสัปดาห์ที่แล้ว",
    },
    "wrapped_biggest_mover_delta": {
        "en": "📈 Biggest mover: {label} {sign}{delta:g} vs last week",
        "th": "📈 เปลี่ยนแปลงมากที่สุด: {label} {sign}{delta:g} เทียบสัปดาห์ที่แล้ว",
    },
    "wrapped_caption_4w": {
        "en": "🎉 Your last 4 weeks — {habit_list}. Nice work!",
        "th": "🎉 4 สัปดาห์ที่ผ่านมาของคุณ — {habit_list} เก่งมาก!",
    },
    "wrapped_caption_month": {
        "en": "🎉 Your {month} — {habit_list}. Nice work!",
        "th": "🎉 {month} ของคุณ — {habit_list} เก่งมาก!",
    },
    "wrapped_fallback_header": {
        "en": "🎉 Charts aren't available right now — here's your recap ({period_label}):",
        "th": "🎉 ตอนนี้ยังแสดงกราฟไม่ได้ — สรุปแบบย่อให้แทน ({period_label}):",
    },
    "wrapped_fallback_line": {
        "en": "• {label}: {total} · {streak}",
        "th": "• {label}: {total} · {streak}",
    },
    # ===================================================================
    # SPEC-v1.9.md §6/§11 integration pass: renderer wiring keys (AC9,
    # AC10, AC17, AC22, AC30) -- the unit-aware ("week" instead of "day")
    # sibling of an existing key, the cadence-row/pause-marker appends the
    # dashboard/`/habits` renderers now use, and the four new `/help`
    # lines. Every `_weeks`-suffixed key here is ONLY ever selected when
    # `streaks.streak_unit(...) == "week"` (a cadence habit) -- a
    # non-cadence habit always resolves through its pre-existing sibling
    # key, unchanged (AC3's byte-identical gate).
    # ===================================================================
    "cadence_weekly_streak_suffix": {
        "en": " · weekly streak {streak} week(s)",
        "th": " · ต่อเนื่อง {streak} สัปดาห์",
    },
    "pause_dashboard_marker": {
        "en": " ⏸ paused until {date} (held)",
        "th": " ⏸ พักจนถึง {date} (หยุดชั่วคราว)",
    },
    "milestone_reached_weeks": {
        "en": "🔥 {streak}-week {label} streak — nice work, keep it going!",
        "th": "🔥 ต่อเนื่อง {streak} สัปดาห์แล้วสำหรับ{label} — เก่งมากเลยนะ!",
    },
    "record_broken_longest_streak_weeks": {
        "en": "🎉 New personal best — longest {label} streak: {weeks} weeks!",
        "th": "🎉 สถิติใหม่ — ต่อเนื่อง{label}นานที่สุด: {weeks} สัปดาห์!",
    },
    "records_line_longest_streak_weeks": {
        "en": "• Longest streak: {weeks} weeks ({achieved_on})",
        "th": "• ต่อเนื่องนานที่สุด: {weeks} สัปดาห์ ({achieved_on})",
    },
    "daily_summary_numeric_goal_weeks": {
        "en": "  {label}: {total:g} / {goal:g} {unit} ({pct}%) · streak {streak}wk",
        "th": "  {label}: {total:g} / {goal:g} {unit} ({pct}%) · ต่อเนื่อง {streak} สัปดาห์",
    },
    "daily_summary_numeric_nogoal_weeks": {
        "en": "  {label}: {total:g} {unit} today · streak {streak}wk",
        "th": "  {label}: {total:g} {unit} วันนี้ · ต่อเนื่อง {streak} สัปดาห์",
    },
    "daily_summary_duration_nogoal_weeks": {
        "en": "  {label}: {total} session(s) today · streak {streak}wk",
        "th": "  {label}: {total} ครั้งวันนี้ · ต่อเนื่อง {streak} สัปดาห์",
    },
    "daily_summary_boolean_weeks": {
        "en": "  {label}: {status} · streak {streak}wk",
        "th": "  {label}: {status} · ต่อเนื่อง {streak} สัปดาห์",
    },
    "daily_summary_text_weeks": {
        "en": "  {label}: {total} entry(ies) today · streak {streak}wk",
        "th": "  {label}: บันทึกแล้ว {total} ครั้งวันนี้ · ต่อเนื่อง {streak} สัปดาห์",
    },
    "stats_generic_duration_summary_weeks": {
        "en": "{label} sessions this week: {total}, current streak: {streak} week(s)",
        "th": "{label}สัปดาห์นี้: {total} ครั้ง ต่อเนื่อง {streak} สัปดาห์",
    },
    "stats_stretch_summary_weeks": {
        "en": "Stretch sessions this week: {stretch_total}, current streak: {stretch_streak} week(s)",
        "th": "ยืดเส้นสัปดาห์นี้: {stretch_total} ครั้ง ต่อเนื่อง {stretch_streak} สัปดาห์",
    },
    "chart_caption_duration_weeks": {
        "en": "{label}: {total:g} sessions this week — {streak}-week streak",
        "th": "{label}: {total:g} ครั้งสัปดาห์นี้ — ต่อเนื่อง {streak} สัปดาห์",
    },
    "help_cadence_cmd": {
        "en": '📅 "/cadence <habit> <N>" sets a weekly goal (e.g. "gym 3x/week") so rest days don\'t break your streak; "/cadence <habit> off" clears it.',
        "th": '📅 "/cadence <กิจกรรม> <จำนวน>" ตั้งเป้าหมายรายสัปดาห์ (เช่น ยิม 3 ครั้ง/สัปดาห์) วันพักจะไม่ทำให้สตรีคขาด · "/cadence <กิจกรรม> off" เพื่อปิด',
    },
    "help_grace": {
        "en": "🛟 A single missed day is automatically forgiven once a week (a free \"grace day\") so one bad day doesn't break your streak.",
        "th": "🛟 พลาดไป 1 วัน จะได้รับการยกเว้นให้อัตโนมัติสัปดาห์ละครั้ง (\"วันผ่อนผัน\") เพื่อไม่ให้สตรีคขาดเพราะวันแย่ๆ วันเดียว",
    },
    "help_pause_cmd": {
        "en": '⏸ "/pause [habit] <5d|until DATE>" mutes reminders and holds your streak for a planned break; no habit = pauses everything.',
        "th": '⏸ "/pause [กิจกรรม] <5d|until วันที่>" ปิดแจ้งเตือนและคงสตรีคไว้ระหว่างพัก ถ้าไม่ระบุกิจกรรม จะพักทุกอย่าง',
    },
    "help_resume_cmd": {
        "en": '▶ "/resume [habit]" ends a pause early; no habit = resumes everything.',
        "th": '▶ "/resume [กิจกรรม]" กลับมาก่อนกำหนด ถ้าไม่ระบุกิจกรรม จะกลับมาทุกอย่าง',
    },
    "help_wrapped_cmd": {
        "en": '🎉 "/wrapped" (or "/recap") sends a picture recap of your last 4 weeks; "/wrapped month" recaps the current month.',
        "th": '🎉 "/wrapped" (หรือ "/recap") ส่งการ์ดสรุปภาพของ 4 สัปดาห์ที่ผ่านมา · "/wrapped month" สรุปเดือนปัจจุบัน',
    },
    # SPEC-v1.10.md "Never lose a log" -- ARCHI-SANCTIONED EXTRA (a): `/edit`
    # (`core/commands.py:_EDIT_TRIGGER`) is a real, working correction
    # command that has apparently never had its own `/help` line -- same
    # "gap-fix, no dedicated module, plain append" shape as the v1.8.1
    # `help_log_cmd`/`help_routine_cmd` precedent above. NL-triggered only
    # (no `/edit` menu entry of its own beyond the slash form itself, and
    # no Telegram command-menu registration -- SPEC-v1.10.md's dispatch
    # explicitly says "NL-triggered, no slash command -- help line only"),
    # so this is a `/help` addition only, not a `set_my_commands` one.
    # Phrases quoted below are verified verbatim against `_EDIT_TRIGGER`'s
    # real alternatives (`core/commands.py`): EN "make that <value>" /
    # "change it to <value>" / "edit it to <value>" (also "edit that to"/
    # "edit last to", not all shown -- two representative examples per the
    # established help-line brevity convention); Thai "แก้เป็น <value>" /
    # "แก้ไขเป็น <value>" (also "...ล่าสุดเป็น" variants, likewise not all
    # shown); the slash form `/edit <value>` works in both languages.
    "help_edit_cmd": {
        "en": (
            '✏️ Correct your last entry: say "make that 500ml" or "change it to 500ml" '
            '(or /edit 500ml; Thai: "แก้เป็น 500 มล.").'
        ),
        "th": (
            '✏️ แก้ไขรายการล่าสุด: พิมพ์ "แก้เป็น 500 มล." หรือ "แก้ไขเป็น 500 มล." '
            '(หรือ /edit 500ml; English: "make that 500ml")'
        ),
    },

    # ===================================================================
    # SPEC-v1.10.md "Never lose a log" -- shared-surface key-block
    # skeletons (§11: "the i18n key-block skeletons are created in the
    # shared surface first; each module then fills only its own disjoint
    # keys", same convention as SPEC-v1.7.md's/SPEC-v1.8.md's/SPEC-v1.9.md's
    # own skeleton markers above). No key is added under either marker
    # below yet, only the section marker + the reserved key-name prefix
    # each module must use, so a later module edit here can never collide
    # with the other parallel module's own addition. Module `riders`
    # (pause fail-open unification + pytest-xdist) owns no new user-facing
    # copy at all -- no marker needed for it.
    #
    # Module `clarify` (M1, functionals 1+2 -- R1-R12): the unparsed-
    # closure notification (§3.1), the tap-to-fix guess offer (§3.2), and
    # the generic clarifying-question-plus-keyboard fallback (R10). Keys
    # must use a `closure_*`/`clarify_*` prefix.
    "closure_notification": {
        "en": (
            '🧠 I couldn\'t make sense of "{text}" — my language brain was offline when you sent it, and I '
            'still can\'t place it. Nothing was logged. If you\'d like to log it, tap a habit below, or type '
            'it like "500 ml".'
        ),
        "th": (
            '🧠 ฉันยังไม่เข้าใจ "{text}" — ตอนที่คุณส่งมาระบบภาษาออฟไลน์อยู่ และตอนนี้ก็ยังจับใจความไม่ได้ '
            'ยังไม่มีการบันทึกใดๆ ถ้าต้องการบันทึก แตะกิจกรรมด้านล่าง หรือพิมพ์แบบ "500 ml"'
        ),
    },
    "clarify_offer": {
        "en": '🤔 I couldn\'t parse "{text}". Did you mean one of these? (Or type it like "500 ml".)',
        "th": '🤔 ฉันแยกแยะ "{text}" ไม่ได้ หมายถึงอันไหนนี้ไหม (หรือพิมพ์แบบ "500 ml")',
    },
    "clarify_already_handled": {
        "en": "🤷 That one's already been taken care of.",
        "th": "🤷 รายการนี้จัดการเรียบร้อยไปแล้วนะ",
    },
    #
    # Module `reply_attribution`/`discoverability` (M2, functionals 3+4+5
    # -- R13-R17): the outage-honesty reply (§3.4) and the /guide card
    # (§3.6). Keys must use an `outage_*`/`guide_*` prefix.
    # ===================================================================

    # -----------------------------------------------------------------
    # Module M2 -- outage honesty (SPEC-v1.10.md §3.4/R15, core/routing.py's
    # integration-owned deferral branch). {text} is the exact raw message
    # that was just saved (quoted verbatim, same "quote the user's own
    # words" posture as `closure_*` (M1) will use for the terminal-failure
    # notification) -- callers pass the same `text` that goes into the
    # `LogEntry.raw_message` write right beside this send. Config-gated by
    # `config.outage.honest_reply`; `false` keeps sending the pre-1.10
    # `deferred_ack` above byte-for-byte instead of this key (R15's own
    # "false restores the pre-1.10 deferred_ack byte-for-byte").
    # -----------------------------------------------------------------
    "outage_honest_reply": {
        "en": (
            '🧠 My language brain is offline right now, so I saved "{text}" and will sort '
            'it out when it\'s back. These still work instantly: a number+unit like "500 ml", '
            "the /log buttons below, or a /routine."
        ),
        "th": (
            '🧠 ตอนนี้ระบบภาษาออฟไลน์อยู่ ฉันเลยเก็บ "{text}" ไว้ และจะจัดการให้เมื่อกลับมา '
            'สิ่งที่ยังใช้ได้ทันที: ตัวเลข+หน่วยแบบ "500 ml", ปุ่ม /log ด้านล่าง หรือ /routine'
        ),
    },

    # -----------------------------------------------------------------
    # Module M2 -- `/guide` card (SPEC-v1.10.md §3.6/R16, core/
    # discoverability.py:build_guide_text). Five short lines, joined
    # "\n\n" exactly like `build_help_text`'s own lines above -- a
    # deliberately SHORT companion to the ever-growing `/help`, not a
    # replacement for it (`guide_footer` points there). Fixed size, no
    # config-driven values, no per-habit content -- so it never needs
    # render-budget capping (R16's own "not budget-capped, fixed size").
    # -----------------------------------------------------------------
    "guide_header": {
        "en": "🧭 Quick start — here's the 20-second version:",
        "th": "🧭 เริ่มต้นใช้งานฉบับย่อ (20 วินาทีก็เข้าใจ):",
    },
    "guide_how_to_log": {
        "en": (
            "📝 Log anything: type freely (e.g. \"drank 500ml\", \"ยืดเส้น 10 นาที\"), a plain "
            'number+unit like "500ml" or "10 min" (works even if I\'m offline), or tap /log '
            "for one-tap buttons."
        ),
        "th": (
            '📝 บันทึกได้หลายแบบ: พิมพ์ธรรมชาติ (เช่น "น้ำ 500 มล.", "10 min stretch") '
            'ตัวเลข+หน่วยตรงๆ เช่น "500ml" หรือ "10 นาที" (ใช้ได้แม้ระบบภาษาออฟไลน์) '
            "หรือพิมพ์ /log เพื่อกดปุ่มลัด"
        ),
    },
    "guide_key_commands": {
        "en": (
            "⚡ Key commands: /log (quick buttons), /undo (remove your last entry), "
            "/target (set a goal), /habits (today's progress), /help (the full list)."
        ),
        "th": (
            "⚡ คำสั่งสำคัญ: /log (ปุ่มลัด), /undo (ลบรายการล่าสุด), /target (ตั้งเป้าหมาย), "
            "/habits (ความคืบหน้าวันนี้), /help (รายการทั้งหมด)"
        ),
    },
    "guide_message_syntax": {
        "en": (
            '💬 Message syntax: reply to one of my reminders with just a number (e.g. "500") '
            "to log it against that habit — no typing needed."
        ),
        "th": (
            '💬 รูปแบบข้อความ: ตอบกลับข้อความแจ้งเตือนด้วยตัวเลขอย่างเดียว (เช่น "500") '
            "เพื่อบันทึกกิจกรรมนั้นได้เลย ไม่ต้องพิมพ์อะไรเพิ่ม"
        ),
    },
    "guide_footer": {
        "en": "🤖 Type /help anytime for the complete guide.",
        "th": "🤖 พิมพ์ /help เมื่อไหร่ก็ได้เพื่อดูคู่มือฉบับเต็ม",
    },

    # ===================================================================
    # SPEC-LINE.md §4 R-S6 (shared surface, branch `line-version`): the
    # LINE edition's own new bilingual copy -- the trimmed daily digest
    # (module C) and the no-LLM NL-target pointer (module B). Keys must use
    # a `digest_*`/`target_nl_*` prefix. `clarify.tier1_guesses`'s own
    # tap-to-fix offer (R-B2) and the generic clarifying question reuse the
    # EXISTING `clarify_offer`/`clarifying_question` keys above verbatim --
    # both are already channel-agnostic copy (no Telegram-specific
    # "tap the button below" framing), so no new key is needed there.
    # -----------------------------------------------------------------
    # Module C -- trimmed daily digest (SPEC-LINE.md §4 R-C1/R-C5/R-C7,
    # core/digest.py). `digest_header` opens the one daily push;
    # `digest_due_reminders_header`/`digest_all_caught_up` frame the (a)
    # due-reminders section (R-C1); `digest_review_ready_line` is the
    # optional one-liner appended on the weekly-review weekday (R-C5,
    # `[digest].include_weekly_review_day`); `digest_quota_warning` is the
    # owner-only line appended once `monthly_push_total >= [digest].
    # warn_cap` (R-C7, default 280 against LINE's free-plan ceiling of
    # ~300/month) -- it never blocks the digest from sending.
    # -----------------------------------------------------------------
    "digest_header": {
        "en": "📋 Your day, in one message:",
        "th": "📋 สรุปวันนี้ในข้อความเดียว:",
    },
    "digest_due_reminders_header": {
        "en": "⏰ Still due today:",
        "th": "⏰ วันนี้ยังไม่ครบ:",
    },
    "digest_all_caught_up": {
        "en": "✅ Everything's logged for today — nice work.",
        "th": "✅ วันนี้บันทึกครบทุกอย่างแล้ว เก่งมาก",
    },
    "digest_review_ready_line": {
        "en": "📈 Your weekly review is ready — send /review to see it.",
        "th": "📈 รีวิวประจำสัปดาห์ของคุณพร้อมแล้ว พิมพ์ /review เพื่อดู",
    },
    "digest_quota_warning": {
        "en": (
            "⚠️ Owner note: this month's LINE push total is {total}/{cap} "
            "(free-plan ceiling ≈300). New digest subscribers may need to wait "
            "until next month, or the account may need paid message capacity."
        ),
        "th": (
            "⚠️ ข้อความถึงเจ้าของบอท: ยอดพุชข้อความเดือนนี้อยู่ที่ {total}/{cap} "
            "(เพดานแพ็กเกจฟรี ≈300) ผู้สมัครสรุปรายวันใหม่อาจต้องรอเดือนถัดไป "
            "หรืออาจต้องซื้อโควตาข้อความเพิ่ม"
        ),
    },

    # -----------------------------------------------------------------
    # Module C -- `/digest on|off` setter (SPEC-LINE.md §4 R-C4/§9 OQ4,
    # core/digest.py:execute_digest_toggle). Mirrors `checkin_show`/
    # `checkin_show_off`/`checkin_set_on`/`checkin_set_off`/`checkin_usage`/
    # `checkin_save_failed`'s own naming shape one-for-one.
    # -----------------------------------------------------------------
    "digest_toggle_show": {
        "en": "📋 Daily digest: ON (around {time}).",
        "th": "📋 สรุปรายวัน: เปิดอยู่ (ประมาณ {time})",
    },
    "digest_toggle_show_off": {
        "en": "📋 Daily digest: OFF. Send /digest on to turn it back on.",
        "th": "📋 สรุปรายวัน: ปิดอยู่ พิมพ์ /digest on เพื่อเปิดอีกครั้ง",
    },
    "digest_toggle_set_on": {
        "en": "✅ Daily digest turned on — you'll get one message a day with your summary.",
        "th": "✅ เปิดสรุปรายวันแล้ว — คุณจะได้รับข้อความสรุปวันละหนึ่งครั้ง",
    },
    "digest_toggle_set_off": {
        "en": "🔕 Daily digest turned off. Send /digest on anytime to turn it back on.",
        "th": "🔕 ปิดสรุปรายวันแล้ว พิมพ์ /digest on เมื่อไหร่ก็ได้เพื่อเปิดอีกครั้ง",
    },
    "digest_toggle_usage": {
        "en": "Usage: /digest on, /digest off, or bare /digest to check the current setting.",
        "th": "วิธีใช้: /digest on, /digest off หรือพิมพ์ /digest เฉยๆ เพื่อดูสถานะปัจจุบัน",
    },
    "digest_toggle_save_failed": {
        "en": "⚠️ Couldn't save your digest setting — please try again.",
        "th": "⚠️ บันทึกการตั้งค่าสรุปรายวันไม่สำเร็จ ลองอีกครั้งนะ",
    },
    "audit_action_digest_set": {
        "en": "digest on",
        "th": "เปิดสรุปรายวัน",
    },
    "audit_action_digest_off": {
        "en": "digest off",
        "th": "ปิดสรุปรายวัน",
    },

    # -----------------------------------------------------------------
    # Module B -- no-LLM NL-target pointer (SPEC-LINE.md §4 R-B3,
    # core/target_nl.py / core/routing.py). NL target-setting ("from now
    # on 3L a day") has no deterministic fallback in no-LLM mode -- this
    # points the user at the explicit, LLM-free /target command instead
    # of silently doing nothing (AC17).
    # -----------------------------------------------------------------
    "target_nl_no_llm_pointer": {
        "en": "🎯 I can't read free-form goal changes right now — use /target <habit> <value> instead.",
        "th": "🎯 ตอนนี้ยังอ่านการตั้งเป้าหมายแบบข้อความอิสระไม่ได้ ใช้คำสั่ง /target <กิจกรรม> <ค่า> แทนนะ",
    },

    # -----------------------------------------------------------------
    # Module B -- no-LLM NL-query pointer (SPEC-LINE.md §4 R-B4,
    # core/query.py). AC17: "how much water this week?" has no
    # deterministic fallback in no-LLM mode -- this points the user at the
    # deterministic /records, /trends, /dashboard commands instead of
    # spending an LLM call on classification. Additive-only: the existing
    # `query_cant_answer` key (the fail-closed classify-miss fallback,
    # used identically whether ollama.enabled is True or False) is left
    # byte-unchanged so the enabled=true path stays byte-identical.
    # -----------------------------------------------------------------
    "query_no_llm_pointer": {
        "en": "🤔 I can't answer free-form questions right now — try /records, /trends, or /dashboard instead.",
        "th": "🤔 ตอนนี้ยังตอบคำถามแบบข้อความอิสระไม่ได้ ลองใช้ /records, /trends หรือ /dashboard แทนนะ",
    },
}
