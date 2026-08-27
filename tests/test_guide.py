"""SPEC-v1.10.md "Never lose a log" -- module M2, functional 5 (`/guide`,
R16, AC15). Owns `core/discoverability.py:build_guide_text` (the pure,
deterministic, LLM-free card builder) and its `guide_*` i18n copy.

`/guide`'s DISPATCH recognition (`command.kind == "guide"`, `คู่มือ`
alias, `reserved_trigger_words()`) is shared surface (already covered by
`tests/test_v110_shared_surface.py`'s AC4 tests); the Telegram command
MENU registration (public 22->23, owner 27->28, `core/app.py:
GUIDE_COMMAND_DESCRIPTIONS`) and the `handle_inbound_message` routing
branch that actually calls `build_guide_text` are the sequential
integration seam's job (Archi's own file-ownership table: `core/app.py`/
`core/routing.py` are not in this module's owned files). These tests
therefore exercise `build_guide_text` directly, mirroring how
`tests/test_discoverability.py` tests `build_help_text` both directly and
via `handle_inbound_message` -- the direct half is this module's to own.
"""

from __future__ import annotations

import pytest

from habit_assistant.config import Config
from habit_assistant.core import discoverability, i18n
from habit_assistant.core.commands import _EDIT_TRIGGER
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET

CONFIG = Config()


# ---------------------------------------------------------------------------
# AC15 -- compact bilingual getting-started card, well under the 4096-char
# budget, in one string (one `channel.send`, per R16 -- this module hands
# back a single string, the caller decides how to send it).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "th"])
def test_build_guide_text_returns_a_non_empty_string(lang):
    text = discoverability.build_guide_text(CONFIG, lang)
    assert isinstance(text, str)
    assert text.strip() != ""


@pytest.mark.parametrize("lang", ["en", "th"])
def test_build_guide_text_stays_well_under_the_telegram_budget(lang):
    text = discoverability.build_guide_text(CONFIG, lang)
    assert len(text) <= TELEGRAM_MESSAGE_BUDGET
    assert len(text) < 1500  # a "20-second orientation", not a full manual


@pytest.mark.parametrize("lang", ["en", "th"])
def test_build_guide_text_is_deterministic(lang):
    """No config-driven values, no DB/registry reads -- calling twice
    produces the exact same string (fixed size, R16's own "not
    budget-capped -- fixed size")."""
    assert discoverability.build_guide_text(CONFIG, lang) == discoverability.build_guide_text(CONFIG, lang)


@pytest.mark.parametrize("lang", ["en", "th"])
def test_build_guide_text_is_five_lines_joined_by_blank_lines(lang):
    """Mirrors `build_help_text`'s own "\\n\\n"-joined-lines shape."""
    text = discoverability.build_guide_text(CONFIG, lang)
    lines = text.split("\n\n")
    assert len(lines) == 5
    assert all(line.strip() for line in lines)


def test_build_guide_text_covers_how_to_log_en():
    text = discoverability.build_guide_text(CONFIG, "en")
    assert "500ml" in text  # a plain number+unit example
    assert "/log" in text


def test_build_guide_text_covers_key_commands_en():
    text = discoverability.build_guide_text(CONFIG, "en")
    for cmd in ("/log", "/undo", "/target", "/habits", "/help"):
        assert cmd in text


def test_build_guide_text_covers_message_syntax_en():
    """The v1.10.0 reply-to-reminder shortcut is explicitly called out
    (R13's own discoverability half)."""
    text = discoverability.build_guide_text(CONFIG, "en")
    assert "reply" in text.lower()


def test_build_guide_text_covers_key_commands_th():
    text = discoverability.build_guide_text(CONFIG, "th")
    for cmd in ("/log", "/undo", "/target", "/habits", "/help"):
        assert cmd in text


def test_build_guide_text_points_to_help_for_the_full_list():
    for lang in ("en", "th"):
        text = discoverability.build_guide_text(CONFIG, lang)
        assert "/help" in text


def test_build_guide_text_never_calls_config_for_a_dynamic_value():
    """Unlike `build_help_text` (whose lines change with e.g.
    `config.gamification.milestones`), `build_guide_text` is content-fixed
    -- two different configs still produce byte-identical output."""
    other_config = Config.model_validate({"gamification": {"milestones": [1, 2, 3]}})
    assert discoverability.build_guide_text(CONFIG, "en") == discoverability.build_guide_text(other_config, "en")


# ---------------------------------------------------------------------------
# Catalog-level guards for the disjoint `guide_*` key block this module
# owns (the global bilingual-completeness/placeholder-cleanliness checks
# already live in tests/test_i18n.py and cover every catalog id
# automatically -- these are scoped, module-specific spot-checks).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg_id",
    ["guide_header", "guide_how_to_log", "guide_key_commands", "guide_message_syntax", "guide_footer"],
)
def test_every_guide_key_exists_with_both_languages(msg_id):
    assert set(i18n.CATALOG[msg_id]) == {"en", "th"}


# ---------------------------------------------------------------------------
# ARCHI-SANCTIONED EXTRA (a) -- `/edit` discoverability (Archi's follow-up
# directive after Vera's `tests/test_v110_m2_gaps.py` caught the original
# dispatch item as never having been delivered): a `help_edit_cmd` line,
# bilingual, describing the REAL natural-language correction phrases
# `core/commands.py:_EDIT_TRIGGER` accepts -- appended in
# `build_help_text` after `help_wrapped_cmd`. NL-triggered only (no slash
# command of its own beyond `/edit` itself), so there is deliberately no
# `set_my_commands` menu entry -- `core/app.py` (integration's file, not
# owned or touched here) was not edited for this, and no menu-related
# test belongs in this module's scope.
# ---------------------------------------------------------------------------


def test_help_edit_cmd_exists_with_both_languages():
    assert set(i18n.CATALOG["help_edit_cmd"]) == {"en", "th"}


def test_help_edit_cmd_appears_in_build_help_text_both_languages():
    for lang in ("en", "th"):
        text = discoverability.build_help_text(CONFIG, lang)
        assert i18n.t("help_edit_cmd", lang) in text


def test_help_edit_cmd_en_phrases_are_real_edit_triggers():
    """Ground-truth cross-check against the REAL matcher
    (`core/commands.py:_EDIT_TRIGGER`), not just plausible-looking copy --
    both EN phrases quoted in `help_edit_cmd`'s English variant must
    actually be accepted, with the trailing value captured correctly."""
    for phrase, expected_value in [("make that 500ml", "500ml"), ("change it to 500ml", "500ml")]:
        assert phrase in i18n.t("help_edit_cmd", "en")
        match = _EDIT_TRIGGER.match(phrase)
        assert match is not None, f"{phrase!r} should be a real /edit trigger"
        assert match.group("value") == expected_value


def test_help_edit_cmd_slash_form_is_a_real_edit_trigger():
    assert "/edit 500ml" in i18n.t("help_edit_cmd", "en")
    match = _EDIT_TRIGGER.match("/edit 500ml")
    assert match is not None
    assert match.group("value") == "500ml"


def test_help_edit_cmd_th_phrases_are_real_edit_triggers():
    for phrase in ("แก้เป็น 500 มล.", "แก้ไขเป็น 500 มล."):
        assert phrase in i18n.t("help_edit_cmd", "th")
        match = _EDIT_TRIGGER.match(phrase)
        assert match is not None, f"{phrase!r} should be a real /edit trigger"


def test_help_edit_cmd_placed_after_help_wrapped_cmd():
    """"Append after the v1.10 additions if any, else after
    help_delhabit_cmd's successors" -- help_wrapped_cmd is v1.9's last
    successor in that chain, so help_edit_cmd must render strictly after
    it (and, since it landed before any other v1.10 /help line, right
    after it)."""
    text = discoverability.build_help_text(CONFIG, "en")
    wrapped_idx = text.index(i18n.t("help_wrapped_cmd", "en"))
    edit_idx = text.index(i18n.t("help_edit_cmd", "en"))
    assert wrapped_idx < edit_idx
