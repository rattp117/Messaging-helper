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
(§3.4's own illustrative copy, R-N1)."""

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
