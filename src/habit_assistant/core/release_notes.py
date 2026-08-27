"""Per-version bilingual "what's new" catalog (SPEC-v1.5.md §4 "Feature 4
-- Release announcements (module `announce`)", R-N1): the user-facing copy
`core/announce.py:announce_release` sends once per version per active user.

Deliberately its OWN catalog, separate from `core/i18n.py:CATALOG` -- a
release note is keyed by VERSION STRING, not by a fixed message id, and is
a multi-line feature summary rather than a single-purpose template string
(SPEC-v1.5.md §11: "release notes live in their own `core/release_notes.py`,
not the shared i18n catalog, so `announce` never collides with `checkins`").

**Process requirement (R-N1, Archi's release checklist):** add the entry
for a new version HERE, before tagging it -- a version with no entry here
simply announces nothing (`get_release_note` returns `None`, and
`announce_release` returns immediately with no sends/no error, R-N2/AC-22).
Every entry MUST carry both `en` and `th` variants (mirrors `core/i18n.py`'s
own catalog convention, enforced there by `tests/test_i18n.py`; enforced
for this catalog by `tests/test_announce.py`'s own structural check) --
`announce_release` resolves the recipient's own language and would
otherwise skip a user whose resolved language has no variant here.

v1.5.0 ships as the very first row -- this release announces itself
(§3.4's own illustrative copy, R-N1). v1.6.0 (SPEC-v1.6.md §4 R-X4) adds
its own entry below, same convention. v1.7.0 (SPEC-v1.7.md §4 R-A2) adds
its own entry too."""

from __future__ import annotations

from habit_assistant.core import i18n

RELEASE_NOTES: dict[str, dict[i18n.Language, str]] = {
    "1.5.0": {
        "en": (
            "🎉 What's new in v1.5.0\n"
            "• Hourly check-ins — send /checkin on for gentle nudges (default 08:00–20:00)\n"
            "• /dnd — set your own do-not-disturb hours (same as /quiet)\n"
            '• Simple logs like "500ml" are now faster, and still work even if the assistant '
            "is briefly offline\n"
            "• You'll get a short note like this whenever a new version ships"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.5.0\n"
            "• เช็คอินรายชั่วโมง — พิมพ์ /checkin on เพื่อรับการเตือนเบาๆ (ค่าเริ่มต้น 08:00–20:00)\n"
            "• /dnd — ตั้งช่วงเวลางดรบกวนของคุณเอง (เหมือนกับ /quiet)\n"
            '• บันทึกง่ายๆ อย่าง "500ml" ตอบเร็วขึ้น และยังใช้ได้แม้ระบบผู้ช่วยจะขัดข้องชั่วคราว\n'
            "• จะมีข้อความสั้นๆ แบบนี้แจ้งให้ทราบทุกครั้งที่มีเวอร์ชันใหม่"
        ),
    },
    "1.6.0": {
        "en": (
            "🎉 What's new in v1.6.0\n"
            "• Live dashboard — /dashboard on pins a Today board that updates itself as you log\n"
            "• Consistency heatmap — /heatmap for a calendar picture of your habits\n"
            "• Personal records — /records shows your best day, best week, and longest streak\n"
            "• Trends — /trends shows this week vs last, at a glance\n"
            "• A gentle end-of-day nudge when you're close to a goal (rides check-in enablement)"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.6.0\n"
            "• แดชบอร์ดสด — พิมพ์ /dashboard on เพื่อปักหมุดบอร์ดวันนี้ที่อัปเดตเองเมื่อคุณบันทึก\n"
            "• ปฏิทินความสม่ำเสมอ — พิมพ์ /heatmap เพื่อดูภาพปฏิทินกิจกรรมของคุณ\n"
            "• สถิติส่วนตัว — พิมพ์ /records เพื่อดูวันที่ดีที่สุด สัปดาห์ที่ดีที่สุด และสตรีคยาวที่สุด\n"
            "• แนวโน้ม — พิมพ์ /trends เพื่อดูเปรียบเทียบสัปดาห์นี้กับสัปดาห์ที่แล้วแบบรวดเร็ว\n"
            "• การเตือนใจเบาๆ ช่วงท้ายวันเมื่อใกล้ถึงเป้าหมาย (ผูกกับการเปิดใช้เช็คอิน)"
        ),
    },
    "1.7.0": {
        "en": (
            "🎉 What's new in v1.7.0\n"
            "• Custom habits — /addhabit lets you define your own tracker from chat\n"
            "• Once created, it works everywhere: logging, undo, /target, /remind, streaks, "
            "the daily summary, weekly review, /habits, /history, /heatmap, /records, /trends, "
            "check-ins, and the dashboard\n"
            "• /delhabit removes one — archived if it has history, deleted if it doesn't\n"
            "• Custom habits are entirely private to you"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.7.0\n"
            "• นิสัยที่กำหนดเอง — พิมพ์ /addhabit เพื่อสร้างตัวติดตามของคุณเองจากแชท\n"
            "• เมื่อสร้างแล้วใช้ได้ทุกที่: การบันทึก การยกเลิก /target /remind สตรีค "
            "สรุปประจำวัน รีวิวรายสัปดาห์ /habits /history /heatmap /records /trends "
            "เช็คอิน และแดชบอร์ด\n"
            "• /delhabit เพื่อลบนิสัย — เก็บเข้าคลังถ้ามีประวัติ หรือลบทิ้งถ้ายังไม่มี\n"
            "• นิสัยที่กำหนดเองเป็นส่วนตัวของคุณเท่านั้น"
        ),
    },
    "1.8.0": {
        "en": (
            "🎉 What's new in v1.8.0\n"
            "• One-tap quick-log — /log pops a keyboard of your habits, tap once to log\n"
            "• Instant reactions — the bot reacts on your typed logs, no extra reply\n"
            "• Routines — /routine morning = water 500, stretch 10 bundles a whole habit "
            "stack into one command or one tap\n"
            '• Backfill — log for a past day, e.g. "500ml yesterday" or "stretched 20 min '
            'on Monday"\n'
            "• Reminders, check-ins, and nudges now arrive silently by default (no more "
            "notification ping) — set [notifications] silent_proactive = false to go back"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.8.0\n"
            "• บันทึกด่วนแบบแตะเดียว — พิมพ์ /log เพื่อเปิดปุ่มนิสัยของคุณ แตะครั้งเดียวก็บันทึกได้\n"
            "• รีแอคชันทันที — บอทจะกดรีแอคชันบนข้อความบันทึกของคุณ ไม่ต้องตอบเพิ่ม\n"
            "• กิจวัตร — /routine morning = water 500, stretch 10 รวมชุดนิสัยไว้ในคำสั่งเดียว "
            "หรือแตะเดียว\n"
            '• บันทึกย้อนหลัง — บันทึกของวันก่อนได้ เช่น "เมื่อวาน ดื่มน้ำ 500" หรือ '
            '"ยืดเส้น 20 นาที วันจันทร์"\n'
            "• การเตือน เช็คอิน และการกระตุ้นเบาๆ จะส่งแบบไม่มีเสียงแจ้งเตือนเป็นค่าเริ่มต้นแล้ว "
            "— ตั้งค่า [notifications] silent_proactive = false เพื่อกลับไปแบบเดิม"
        ),
    },
    "1.9.0": {
        "en": (
            "🎉 What's new in v1.9.0\n"
            "• Weekly-cadence goals — /cadence gym 3 means rest days no longer break your streak, "
            "as long as you hit 3×/week\n"
            "• A gentle grace day — miss one day and, once a week, your streak is quietly protected "
            "with a kind note (no punishment, ever)\n"
            "• Pause / vacation mode — /pause water until 2026-09-01 mutes reminders and holds your "
            "streak; /resume picks up right where you left off\n"
            "• /wrapped (or /recap) — a shareable recap card with your records, trends, and a mini "
            "heatmap, last 4 weeks by default\n"
            "• Thai now renders as real text in every chart and heatmap, not boxes\n"
            "• A small emoji-burst celebrates milestones and new records"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.9.0\n"
            "• เป้าหมายรายสัปดาห์ — พิมพ์ /cadence gym 3 แล้ววันพักจะไม่ทำให้สตรีคขาด ตราบใดที่ทำครบ 3 ครั้ง/สัปดาห์\n"
            "• วันผ่อนผันแบบใจดี — พลาดไปหนึ่งวัน ระบบจะช่วยปกป้องสตรีคให้เงียบๆ สัปดาห์ละครั้ง พร้อมข้อความให้กำลังใจ "
            "(ไม่มีการตำหนิใดๆ)\n"
            "• โหมดพัก/ลาพัก — พิมพ์ /pause น้ำ until 2026-09-01 เพื่อหยุดการเตือนชั่วคราวและคงสตรีคไว้ "
            "ใช้ /resume เพื่อกลับมาต่อได้ทุกเมื่อ\n"
            "• /wrapped (หรือ /recap) — การ์ดสรุปที่แชร์ได้ รวมสถิติ แนวโน้ม และปฏิทินย่อ ค่าเริ่มต้นคือ 4 สัปดาห์ล่าสุด\n"
            "• ภาษาไทยแสดงผลเป็นตัวอักษรจริงในกราฟและปฏิทินความสม่ำเสมอแล้ว ไม่ใช่กล่องสี่เหลี่ยมอีกต่อไป\n"
            "• อีโมจิเฉลิมฉลองเล็กๆ เมื่อถึงไมล์สโตนหรือสถิติใหม่"
        ),
    },
    "1.10.0": {
        "en": (
            "🎉 What's new in v1.10.0\n"
            "• Never lose a log — a message the assistant still can't place after an outage now gets a "
            "kind heads-up (quoting what you sent) instead of being silently dropped forever\n"
            "• Tap-to-fix — when a message is unclear, you'll often get one-tap buttons guessing what "
            "you meant, built from your own habits\n"
            "• Reply to a reminder — just reply to a reminder message with a number to log it against "
            "that habit, no typing the habit name, works even if the assistant is briefly offline\n"
            "• Outage honesty — if the language brain is briefly offline, you're told right away and "
            "shown what still works instantly (number+unit, /log, /routine)\n"
            "• /guide — a quick, shareable getting-started card for anyone new to the bot"
        ),
        "th": (
            "🎉 มีอะไรใหม่ใน v1.10.0\n"
            "• ไม่มีบันทึกใดหายไปอีกต่อไป — ข้อความที่ระบบยังจับใจความไม่ได้หลังระบบขัดข้อง จะได้รับการแจ้งเตือนอย่างสุภาพ "
            "(พร้อมยกข้อความที่คุณส่งมา) แทนที่จะถูกทิ้งไปเงียบๆ ตลอดกาล\n"
            "• แตะเพื่อแก้ไข — เมื่อข้อความไม่ชัดเจน คุณอาจได้รับปุ่มให้แตะเลือกสิ่งที่คุณหมายถึง สร้างจากนิสัยของคุณเอง\n"
            "• ตอบกลับการเตือนเพื่อบันทึก — แค่ตอบกลับข้อความเตือนด้วยตัวเลข ก็บันทึกให้กิจกรรมนั้นได้เลย "
            "ไม่ต้องพิมพ์ชื่อกิจกรรม ใช้ได้แม้ระบบผู้ช่วยจะขัดข้องชั่วคราว\n"
            "• ความซื่อตรงยามระบบขัดข้อง — ถ้าระบบภาษาออฟไลน์ชั่วคราว คุณจะได้รับแจ้งทันทีพร้อมบอกว่าอะไรยังใช้ได้ "
            "(ตัวเลข+หน่วย, /log, /routine)\n"
            "• /guide — การ์ดแนะนำเริ่มต้นใช้งานแบบย่อ แชร์ต่อได้ เหมาะสำหรับผู้ใช้ใหม่"
        ),
    },
}


def get_release_note(version: str, lang: i18n.Language) -> str | None:
    """R-N1: the release-note body for `version` in `lang`, or `None` if
    `version` has no catalog entry at all (a not-yet-released version, or
    simply a release that predates this catalog's own introduction) --
    never raises. A version WITH an entry always has both language
    variants by construction (this module's own authoring convention,
    guarded by `tests/test_announce.py`), so in practice `None` is only
    ever returned for a version key that isn't in `RELEASE_NOTES` at all;
    the per-language `.get()` below is a defensive second layer, not a
    documented "partial entry" state."""
    variants = RELEASE_NOTES.get(version)
    if variants is None:
        return None
    return variants.get(lang)
