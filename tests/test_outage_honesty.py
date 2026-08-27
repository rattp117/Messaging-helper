"""SPEC-v1.10.md "Never lose a log" -- module M2, functional 4 (outage
honesty, R15, AC14). Owns the bilingual `outage_honest_reply` i18n copy
(`core/i18n.py`) -- the ROUTING branch that decides WHEN to send it
(Ollama-DOWN + preparse-miss, gated by `config.outage.honest_reply`, in
`core/routing.py`'s deferral branch) is the sequential integration seam's
job, not this module's (Archi's own dispatch note: "the routing-side
branch that USES it is integration's").

These tests therefore exercise the copy at MESSAGE-RENDER level --
`i18n.t("outage_honest_reply", lang, text=...)` directly -- rather than
through `handle_inbound_message`, mirroring how `deferred_ack`/
`clarifying_question` are themselves plain `i18n.t()` catalog lookups
with no dedicated builder function (see `core/routing.py`)."""

from __future__ import annotations

import pytest

from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.render_budget import TELEGRAM_MESSAGE_BUDGET

# SPEC-v1.10.md §3.4's own illustrative example -- the exact raw message
# quoted in the spec's EN/TH worked outputs.
_RAW_TEXT = "went for a run"


# ---------------------------------------------------------------------------
# AC14 -- bilingual, quotes the saved text, names the instant-working
# paths (number+unit, /log, /routine).
# ---------------------------------------------------------------------------


def test_outage_honest_reply_has_both_languages():
    assert set(i18n.CATALOG["outage_honest_reply"]) == {"en", "th"}


def test_outage_honest_reply_en_quotes_the_saved_text_verbatim():
    rendered = i18n.t("outage_honest_reply", "en", text=_RAW_TEXT)
    assert f'"{_RAW_TEXT}"' in rendered


def test_outage_honest_reply_th_quotes_the_saved_text_verbatim():
    rendered = i18n.t("outage_honest_reply", "th", text=_RAW_TEXT)
    assert f'"{_RAW_TEXT}"' in rendered


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_names_every_instant_working_path(lang):
    """R15: "names the instant-working paths (number+unit, /log,
    /routine)" -- all three must appear in BOTH language variants (the
    command names themselves, `/log`/`/routine`, are not translated;
    only the surrounding copy is)."""
    rendered = i18n.t("outage_honest_reply", lang, text=_RAW_TEXT)
    assert "500 ml" in rendered  # the number+unit example
    assert "/log" in rendered
    assert "/routine" in rendered


def test_outage_honest_reply_en_matches_spec_worked_example():
    """SPEC-v1.10.md §3.4's own EN example, byte-for-byte."""
    rendered = i18n.t("outage_honest_reply", "en", text=_RAW_TEXT)
    assert rendered == (
        '🧠 My language brain is offline right now, so I saved "went for a run" and will sort '
        "it out when it's back. These still work instantly: a number+unit like \"500 ml\", "
        "the /log buttons below, or a /routine."
    )


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_stays_well_under_the_telegram_budget(lang):
    rendered = i18n.t("outage_honest_reply", lang, text=_RAW_TEXT)
    assert len(rendered) <= TELEGRAM_MESSAGE_BUDGET
    assert len(rendered) < 500  # nowhere near needing render-budget capping


@pytest.mark.parametrize("lang", ["en", "th"])
def test_outage_honest_reply_handles_a_long_raw_message_without_crashing(lang):
    """Not truncated/capped (out of this module's scope -- SPEC-v1.10.md
    §3.4 shows no truncation for this message, unlike `core/audit_view.py`/
    `core/history_view.py`'s explicit render-budget use); this test only
    guards that formatting a long value doesn't raise."""
    long_text = "x" * 500
    rendered = i18n.t("outage_honest_reply", lang, text=long_text)
    assert long_text in rendered


# ---------------------------------------------------------------------------
# R15 -- `config.outage.honest_reply=false` restores the pre-1.10
# `deferred_ack` byte-for-byte. `deferred_ack` itself is shared-surface/
# pre-existing and untouched by this module -- this is a regression guard
# proving this pass didn't accidentally edit it.
# ---------------------------------------------------------------------------


def test_deferred_ack_is_unchanged_byte_for_byte():
    assert i18n.CATALOG["deferred_ack"]["en"] == "⏳ Got it — I'll process this once the connection to the assistant is back."
    assert i18n.CATALOG["deferred_ack"]["th"] == "⏳ รับทราบแล้วนะ เดี๋ยวประมวลผลให้ทันทีที่ระบบกลับมาใช้งานได้"


def test_outage_config_defaults_honest_reply_on():
    config = Config()
    assert config.outage.honest_reply is True


def test_outage_config_can_be_turned_off():
    config = Config.model_validate({"outage": {"honest_reply": False}})
    assert config.outage.honest_reply is False
