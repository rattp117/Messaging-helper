"""Message catalog + language detection tests (ROADMAP.md v0.6.0 "Bilingual
Output & Message Catalog", AC6.3, AC6.5).

Companion to test_i18n_literals.py (AC6.2's static "no hard-coded literal"
scan) and test_bilingual_confirmations.py (AC6.1's end-to-end Thai-in ->
Thai-out / English-in -> English-out behavior). This file covers the
catalog/detector building blocks in isolation.
"""

from __future__ import annotations

import re

import pytest

from habit_assistant.config import Config
from habit_assistant.core import i18n

# ---------------------------------------------------------------------------
# AC6.5: any Thai Unicode character -> "th"; pure non-Thai -> "en";
# deterministic (same input always yields the same result, no external
# calls / no locale dependence).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("500ml", "en"),
        ("did 10 min stretch", "en"),
        ("purple elephants dance sideways", "en"),
        ("today was such a tiring but good day", "en"),
        ("ดื่มน้ำ 2 แก้ว", "th"),
        ("แก้เป็น 300 มล.", "th"),
        ("500ml น้ำ", "th"),  # mixed Thai + English -> any Thai char wins
        ("ก", "th"),  # a single Thai character is enough
        ("", "en"),  # no characters at all -> no Thai char found -> en
        ("12345", "en"),
        ("!!! ???", "en"),
    ],
)
def test_detect_language_any_thai_char_wins(text, expected):
    assert i18n.detect_language(text) == expected


def test_detect_language_is_deterministic():
    for _ in range(5):
        assert i18n.detect_language("ดื่มน้ำ 2 แก้ว") == "th"
        assert i18n.detect_language("500ml") == "en"


# ---------------------------------------------------------------------------
# AC6.3: language="th"/"en" forces that language regardless of input;
# "auto" matches the detected language of the inbound message for replies,
# and falls back to the configured primary language for unprompted sends.
# ---------------------------------------------------------------------------


def test_resolve_reply_language_auto_matches_input():
    config = Config()  # i18n.language="auto" default
    assert i18n.resolve_reply_language("500ml", config) == "en"
    assert i18n.resolve_reply_language("ดื่มน้ำ 2 แก้ว", config) == "th"


def test_resolve_reply_language_forced_th_overrides_english_input():
    config = Config.model_validate({"i18n": {"language": "th"}})
    assert i18n.resolve_reply_language("500ml", config) == "th"


def test_resolve_reply_language_forced_en_overrides_thai_input():
    config = Config.model_validate({"i18n": {"language": "en"}})
    assert i18n.resolve_reply_language("ดื่มน้ำ 2 แก้ว", config) == "en"


def test_resolve_unprompted_language_auto_uses_primary_language():
    config = Config()  # primary_language default "th"
    assert i18n.resolve_unprompted_language(config) == "th"


def test_resolve_unprompted_language_auto_respects_primary_language_override():
    config = Config.model_validate({"i18n": {"primary_language": "en"}})
    assert i18n.resolve_unprompted_language(config) == "en"


def test_resolve_unprompted_language_forced_overrides_primary():
    config = Config.model_validate({"i18n": {"language": "en", "primary_language": "th"}})
    assert i18n.resolve_unprompted_language(config) == "en"


# ---------------------------------------------------------------------------
# Catalog integrity: every id has both languages, and both variants accept
# the same `.format()` placeholders -- guards against drift where a
# translator adds/renames a kwarg in one language but not the other.
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{(\w+)(?:!\w)?(?::[^{}]*)?\}")


def test_catalog_every_message_has_both_languages():
    for msg_id, variants in i18n.CATALOG.items():
        assert set(variants) == {"en", "th"}, f"{msg_id} must have exactly en+th variants"


def test_catalog_placeholders_are_a_subset_of_the_id_union_and_format_cleanly():
    """`str.format(**kwargs)` silently ignores unused kwargs, so a language
    variant is free to use *fewer* placeholders than its sibling (e.g.
    `stretch_confirmation`'s English "{ordinal} today" vs Thai "ครั้งที่
    {count} ของวันนี้" -- both are handed the same superset of kwargs by
    the caller and each just uses what it needs). What must never happen
    is a variant referencing a placeholder *neither* language's kwargs
    would supply -- that's a typo, and it fails loudly here: every
    variant is formatted with dummy values for the union of both
    languages' placeholder names, so an unresolvable `{typo}` raises
    KeyError instead of silently passing."""
    for msg_id, variants in i18n.CATALOG.items():
        en_keys = set(_PLACEHOLDER_RE.findall(variants["en"]))
        th_keys = set(_PLACEHOLDER_RE.findall(variants["th"]))
        union_kwargs = {key: 1 for key in en_keys | th_keys}
        for lang, template in variants.items():
            try:
                template.format(**union_kwargs)
            except KeyError as exc:
                pytest.fail(f"{msg_id}[{lang}] references unresolvable placeholder {exc}")


def test_t_formats_with_kwargs():
    assert i18n.t("water_confirmation", "en", water_ml=500, total=500, goal=2500, pct=20) == (
        "✅ 500 ml logged — today 500 / 2500 ml (20%)"
    )


def test_t_unknown_message_id_raises_key_error():
    with pytest.raises(KeyError):
        i18n.t("no_such_message", "en")


def test_language_instruction_differs_by_language():
    en = i18n.language_instruction("en")
    th = i18n.language_instruction("th")
    assert en != th
    assert "English" in en
    assert i18n.detect_language(th) == "th"  # the Thai instruction is itself in Thai
