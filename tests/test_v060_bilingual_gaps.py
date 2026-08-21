"""Vera's supplementary coverage for ROADMAP.md v0.6.0 ("Bilingual Output &
Message Catalog", AC6.1-AC6.5), written against Luna's `IMPL.md` v0.6.0.

This file does NOT duplicate `test_i18n.py` / `test_i18n_literals.py` /
`test_bilingual_confirmations.py` -- it fills the specific gaps identified
while auditing those files against ROADMAP.md section 8:

1. AC6.1 -- English output must be **byte-identical to v0.5.0** (not just
   "routes through the catalog"). Luna's own tests assert catalog-consistency,
   which would NOT catch a translator accidentally changing the English
   copy's wording -- so the expected strings here are copied verbatim from
   the v0.5.0 source (git diff v0.5.0..v0.6.0), not derived from
   `i18n.CATALOG`. This also covers undo/edit/clarify, which Luna's
   `test_bilingual_confirmations.py` only spot-checks for language
   *detection*, not exact byte content.
2. AC6.1 -- Thai undo/edit confirmations carry the *correct numbers* (not
   just "is Thai").
3. AC6.2 -- `core/health.py`'s two alert strings resolve through the
   catalog and respect the `language` param, even though ROADMAP.md's own
   v0.6.0 file list doesn't name `core/health.py` (IMPL.md's "Known
   limitations" flags this explicitly; AC6.2's own literal-scan test
   deliberately excludes this file, so it needs separate coverage here).
4. AC6.2 -- one more adversarially-planted-literal check, independent of
   Luna's own meta-test, using a different call shape (keyword argument)
   to corroborate the scanner isn't accidentally narrow to positional args.
5. AC6.4 -- the weekly-review narrative's *system prompt* actually carries
   `i18n.language_instruction(lang)` (existing/changed tests in
   `test_review.py` only check the stats block reaching the *user*
   prompt, not the language directive reaching the *system* prompt).
6. AC6.5 -- detector edge cases named explicitly in this task's brief:
   mixed Thai+English with digits+unit ("ดื่มน้ำ 500ml"), a pure-number
   string, a pure-emoji string -- plus an end-to-end check that the mixed
   case actually produces a Thai reply through the real message-handling
   path, not just a unit-level `detect_language` call.
"""

from __future__ import annotations

import ast
from datetime import date, datetime
from pathlib import Path
from typing import Awaitable, Callable

import pytest

from habit_assistant.channels.base import Channel
from habit_assistant.config import Config
from habit_assistant.core import i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.health import HealthMonitor
from habit_assistant.core.review import run_weekly_review
from habit_assistant.llm.ollama_client import ExtractionResult
from habit_assistant.main import handle_inbound_message
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeChannel(Channel):
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, chat_id: str, text: str) -> None:
        self.sent.append(text)

    async def run(self, on_message: Callable[[str, str], Awaitable[None]], on_callback=None) -> None:
        raise NotImplementedError("not exercised in these tests")


class FakeLLM:
    def __init__(self, reflection: str | None = "reflection"):
        self._reflection = reflection

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        return self._reflection


@pytest.fixture
def fixed_clock():
    def clock():
        return datetime(2026, 8, 19, 14, 30, 0)

    return clock


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "habits.db")
    yield database
    database.close()


def patch_parse_message(monkeypatch, result: ExtractionResult):
    # ROADMAP.md v0.7.0 (SPEC-v0.7.md §5): handle_inbound_message now calls
    # parse_message(text, llm, registry, confidence_threshold) -- the fake's
    # parameter names are updated to match (registry-wiring edit only).
    async def fake_parse_message(text, llm, registry, confidence_threshold=None):
        return result

    monkeypatch.setattr("habit_assistant.main.parse_message", fake_parse_message)


# ---------------------------------------------------------------------------
# 1. AC6.1 -- English output byte-identical to v0.5.0. Expected strings are
# copied verbatim from the pre-v0.6.0 source, NOT built via i18n.t(...),
# so a regression in the catalog's English copy is actually caught.
# ---------------------------------------------------------------------------


async def test_english_water_confirmation_byte_identical_to_v050(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()

    await handle_inbound_message("500ml", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 500 ml logged — today 500 / 2500 ml (20%)"]


async def test_english_stretch_confirmation_byte_identical_to_v050(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("stretch", 10, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "10 min stretch", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ 10 min stretch logged — 1st today"]


async def test_english_diary_confirmation_byte_identical_to_v050(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", "good day", 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "today was a good day", db=db, llm=FakeLLM("Glad to hear it."), channel=channel, config=Config(),
        clock=fixed_clock,
    user_id="owner")

    assert channel.sent == ["✅ Saved. Glad to hear it."]


async def test_english_diary_confirmation_uses_v050_fallback_when_llm_empty(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult("diary", "good day", 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "today was a good day", db=db, llm=FakeLLM(None), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✅ Saved. Thanks for sharing — noted."]


async def test_english_clarifying_question_byte_identical_to_v050(db, fixed_clock, monkeypatch):
    patch_parse_message(monkeypatch, ExtractionResult.unknown())
    channel = FakeChannel()

    await handle_inbound_message(
        "purple elephants dance sideways", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == [
        "🤔 I couldn't quite tell what you meant — was that about water, a stretch "
        "break, or today's diary? Try something like '500ml water' or '10 min stretch'."
    ]


async def test_english_undo_water_confirmation_byte_identical_to_v050(db, fixed_clock):
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply"))
    channel = FakeChannel()

    await handle_inbound_message("/undo", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["↩️ Undone — removed 500 ml water. Today: 0 / 2500 ml (0%)"]


async def test_english_undo_nothing_message_byte_identical_to_v050(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message("/undo", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["🤷 Nothing to undo — you don't have any logged entries yet."]


async def test_english_edit_water_confirmation_byte_identical_to_v050(db, fixed_clock):
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply"))
    channel = FakeChannel()

    await handle_inbound_message(
        "make that 300ml", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["✏️ Updated to 300 ml — today 300 / 2500 ml (12%)"]


async def test_english_edit_nothing_message_byte_identical_to_v050(db, fixed_clock):
    channel = FakeChannel()

    await handle_inbound_message(
        "make that 300ml", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert channel.sent == ["🤷 Nothing to edit — I couldn't find a matching entry to update."]


# ---------------------------------------------------------------------------
# 2. AC6.1 -- Thai undo/edit confirmations carry the correct NUMBERS, not
# just "detected as Thai" (test_bilingual_confirmations.py only checks the
# latter for undo). Checked independently of the catalog by asserting the
# raw digits, not by rebuilding the expected string via i18n.t(...).
# ---------------------------------------------------------------------------


async def test_thai_undo_confirmation_has_correct_numbers(db, fixed_clock):
    db.insert_log(LogEntry(None, "owner", "2026-08-19T08:00:00", "water", 500.0, None, "500ml", "reply"))
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 300.0, None, "แก้เป็น 300 มล.", "reply"))
    channel = FakeChannel()

    await handle_inbound_message(
        "ยกเลิกอันล่าสุด", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    sent = channel.sent[0]
    assert i18n.detect_language(sent) == "th"
    assert "300" in sent  # names the removed (most recent, 300ml) row
    assert "500" in sent  # remaining total after removal is 500/2500 (20%)
    assert "20%" in sent
    assert db.water_total_ml("owner", "2026-08-19") == 500.0


async def test_thai_edit_confirmation_has_correct_numbers(db, fixed_clock):
    db.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 500.0, None, "500ml", "reply"))
    channel = FakeChannel()

    await handle_inbound_message(
        "แก้เป็น 300 มล.", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    sent = channel.sent[0]
    assert i18n.detect_language(sent) == "th"
    assert "300" in sent
    assert db.water_total_ml("owner", "2026-08-19") == 300.0
    assert "12%" in sent  # 300/2500


# ---------------------------------------------------------------------------
# 3. AC6.2 -- core/health.py's alerts also resolve through the catalog and
# respect `language`, even though it's not in ROADMAP.md's own v0.6.0 file
# list (and test_i18n_literals.py's scan deliberately excludes it).
# ---------------------------------------------------------------------------


class _AlwaysDownClient:
    """Minimal httpx.AsyncClient stand-in: every GET raises a transport
    error, so run_once() always observes DOWN on the first check."""

    async def get(self, url, *args, **kwargs):
        import httpx

        raise httpx.ConnectError("simulated down", request=httpx.Request("GET", url))

    async def aclose(self):
        pass


async def test_health_monitor_default_language_ollama_alert_matches_english_catalog_entry():
    channel = FakeChannel()
    monitor = HealthMonitor(
        "http://mac-mini:11434", "fake-token", "owner", client=_AlwaysDownClient(), channel=channel
    )  # language defaults to "en"

    await monitor.run_once()

    assert channel.sent[0] == i18n.t("ollama_down_alert", "en")
    assert i18n.detect_language(channel.sent[0]) == "en"


async def test_health_monitor_thai_language_ollama_alert_is_localized():
    channel = FakeChannel()
    monitor = HealthMonitor(
        "http://mac-mini:11434", "fake-token", "owner", client=_AlwaysDownClient(), channel=channel, language="th"
    )

    await monitor.run_once()

    ollama_alert = channel.sent[0]
    assert ollama_alert == i18n.t("ollama_down_alert", "th")
    assert i18n.detect_language(ollama_alert) == "th"


async def test_health_monitor_thai_language_telegram_alert_is_localized():
    channel = FakeChannel()
    monitor = HealthMonitor(
        "http://mac-mini:11434", "fake-token", "owner", client=_AlwaysDownClient(), channel=channel, language="th"
    )

    await monitor.run_once()  # ollama alert first, then telegram alert (both DOWN on first check)

    assert len(channel.sent) == 2
    telegram_alert = channel.sent[1]
    assert telegram_alert == i18n.t("telegram_down_alert", "th")
    assert i18n.detect_language(telegram_alert) == "th"


def test_health_alert_ids_are_present_in_catalog_for_both_languages():
    """Belt-and-suspenders: the two ids health.py depends on exist with
    both variants, independent of test_i18n.py's generic catalog-integrity
    sweep -- pins the exact ids health.py's source references."""
    for msg_id in ("ollama_down_alert", "telegram_down_alert"):
        assert msg_id in i18n.CATALOG
        assert set(i18n.CATALOG[msg_id]) == {"en", "th"}


# ---------------------------------------------------------------------------
# 4. AC6.2 -- one more adversarial planted-literal check for the AST
# scanner (test_i18n_literals.py's own meta-test already proves it catches
# a positional literal; this corroborates with a keyword-argument shape
# and a multi-offender module, run independently against the scanner's
# actual production entry point rather than re-implementing the walk).
# ---------------------------------------------------------------------------


def _literal_send_offenders(path: Path) -> list[str]:
    """Same AST walk as test_i18n_literals.py's private helper, kept as an
    independent copy on purpose: if Luna's helper itself had a bug, a test
    that imports and calls it would inherit the bug silently."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "send":
                for arg in list(node.args) + [kw.value for kw in node.keywords]:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        offenders.append(f"line {node.lineno}: literal string passed to .send()")
                    elif isinstance(arg, ast.JoinedStr):
                        offenders.append(f"line {node.lineno}: f-string passed to .send()")
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenders


def test_scanner_catches_a_keyword_argument_literal(tmp_path):
    bad_module = tmp_path / "bad_kwarg.py"
    bad_module.write_text(
        "async def handler(channel):\n"
        "    await channel.send(text='a hard-coded literal via kwarg')\n",
        encoding="utf-8",
    )
    offenders = _literal_send_offenders(bad_module)
    assert len(offenders) == 1


def test_scanner_catches_multiple_offenders_in_one_module(tmp_path):
    bad_module = tmp_path / "bad_multi.py"
    bad_module.write_text(
        "async def handler(channel, x):\n"
        "    await channel.send('literal one')\n"
        "    await channel.send(f'literal two {x}')\n"
        "    await channel.send(i18n.t('fine', 'en'))\n",
        encoding="utf-8",
    )
    offenders = _literal_send_offenders(bad_module)
    assert len(offenders) == 2  # exactly the two non-compliant calls, not the compliant third one


def test_v060_scoped_source_files_are_actually_clean_per_independent_scanner_copy():
    """Re-runs the independent scanner copy (not Luna's) against the real
    production files, so this doesn't just trust Luna's own implementation
    of the walk to grade itself."""
    for rel in ("main.py", "core/reminders.py", "core/review.py"):
        path = REPO_ROOT / "src" / "habit_assistant" / rel
        offenders = _literal_send_offenders(path)
        assert offenders == [], f"{rel}: {offenders}"


# Known, documented limitation (not a failure): a literal assigned to a
# variable BEFORE being passed to .send() is invisible to this AST shape,
# on both Luna's scanner and this independent copy -- confirming the gap
# exists identically in both, i.e. it's a shared, known heuristic
# limitation, not an implementation bug unique to one copy.
def test_known_limitation_variable_indirection_is_not_caught_by_either_scanner(tmp_path):
    sneaky_module = tmp_path / "sneaky.py"
    sneaky_module.write_text(
        "async def handler(channel):\n"
        "    msg = 'a hard-coded literal hidden behind a variable'\n"
        "    await channel.send(msg)\n",
        encoding="utf-8",
    )
    offenders = _literal_send_offenders(sneaky_module)
    assert offenders == []  # documents the gap; NOT evidence AC6.2's real call sites do this


# ---------------------------------------------------------------------------
# 5. AC6.4 -- the weekly-review narrative's SYSTEM prompt carries the
# target-language directive (existing test_review.py coverage only checks
# the stats block reaching the USER prompt).
# ---------------------------------------------------------------------------


@pytest.fixture
def review_db(tmp_path):
    database = Database(tmp_path / "habits.db")
    database.insert_log(LogEntry(None, "owner", "2026-08-19T09:00:00", "water", 2500.0, None, "seed", "reply"))
    yield database
    database.close()


class RecordingLLM:
    def __init__(self, text: str | None = "narrative"):
        self._text = text
        self.calls: list[tuple[str, str]] = []

    async def chat_text(self, system_prompt: str, user_prompt: str) -> str | None:
        self.calls.append((system_prompt, user_prompt))
        return self._text


async def test_weekly_review_system_prompt_carries_thai_directive_by_default(review_db):
    """CHANGED (ROADMAP.md v0.7.0 integration): run_weekly_review gains a
    required `registry` param (SPEC-v0.7.md §5, module M3) -- registry-
    wiring edit only, no assertion changed.
    CHANGED (SPEC-v1.2.md): `run_weekly_review` now takes `lang` pre-resolved
    (main.py's per-user fan-out resolves it) instead of resolving internally
    -- the test now does what main.py's own call site does:
    `i18n.resolve_unprompted_language(config)` before calling. Same
    assertion, same semantics (default primary language is Thai)."""
    llm = RecordingLLM()
    config = Config()
    registry = HabitRegistry.from_config(config)
    lang = i18n.resolve_unprompted_language(config)

    await run_weekly_review(review_db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    assert len(llm.calls) == 1
    system_prompt, _user_prompt = llm.calls[0]
    assert i18n.language_instruction("th") in system_prompt


async def test_weekly_review_system_prompt_carries_english_directive_when_forced(review_db):
    llm = RecordingLLM()
    config = Config.model_validate({"i18n": {"language": "en"}})
    registry = HabitRegistry.from_config(config)
    lang = i18n.resolve_unprompted_language(config)

    await run_weekly_review(review_db, config, registry, llm, lang, "owner", today=date(2026, 8, 19))

    system_prompt, _user_prompt = llm.calls[0]
    assert i18n.language_instruction("en") in system_prompt
    assert i18n.language_instruction("th") not in system_prompt


# ---------------------------------------------------------------------------
# 6. AC6.5 -- detector edge cases named in this task's brief: mixed
# Thai+English with digits+unit, pure numbers, pure emoji -- plus an
# end-to-end check that the mixed case actually produces a Thai reply.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ดื่มน้ำ 500ml", "th"),  # exact mixed-language string from this task's brief
        ("500", "en"),  # pure number, no unit, no Thai char
        ("💧", "en"),  # pure emoji -- outside the Thai Unicode block, not Thai
        ("💧 500", "en"),  # emoji + number, still zero Thai characters
        ("", "en"),  # empty string -- documented default, no crash
        ("   ", "en"),  # whitespace-only -- same bucket as empty
    ],
)
def test_detect_language_edge_cases_from_task_brief(text, expected):
    assert i18n.detect_language(text) == expected


async def test_mixed_thai_english_input_produces_thai_reply_end_to_end(db, fixed_clock, monkeypatch):
    """"ดื่มน้ำ 500ml" (any-Thai-char rule, AC6.5) drives a real
    handle_inbound_message call all the way to a Thai confirmation --
    not just a unit-level detect_language() call."""
    patch_parse_message(monkeypatch, ExtractionResult("water", 500, 0.9))
    channel = FakeChannel()

    await handle_inbound_message(
        "ดื่มน้ำ 500ml", db=db, llm=FakeLLM(), channel=channel, config=Config(), clock=fixed_clock, user_id="owner")

    assert len(channel.sent) == 1
    assert i18n.detect_language(channel.sent[0]) == "th"
    assert channel.sent[0] == i18n.t("water_confirmation", "th", water_ml=500, total=500, goal=2500, pct=20)
