"""Refactor Stage 3 tests (SPEC-REFACTOR.md, AC9/AC10): the table-driven
`commands.dispatch()` conversion, plus the 5 EASY duplication-consolidation
clusters (language-pref, `_today*`/`_now_hhmm`, `ordinal`, the Thai-alias
token builder, `week_days`).

AC9 -- precedence-capture methodology: `GOLDEN_CORPUS` below was captured
by running the CURRENT if-chain `commands.dispatch()` (the one this stage
replaces) over this exact probe corpus, BEFORE any conversion touched
`core/commands.py` -- the precedence-capture step required before the
table-driven rewrite. Every expected `Command`/`None` here is what the OLD
if-chain actually produced (captured programmatically, not hand-derived),
so `test_dispatch_matches_precedence_captured_from_the_old_if_chain` is a
live proof the table reproduces the pre-conversion routing byte-for-byte --
not merely "passes because it matches whatever the current code does".

The 3 named precedence invariants (SPEC-REFACTOR.md rule 14) each get one
dedicated behavioral test PLUS a structural test directly on
`commands._MATCHERS`'s row order/shape, mirroring the production-side
`commands._assert_dispatch_invariants` guard (runs at import time) so a
future table edit that silently reorders/duplicates a row fails in three
independent places, not just one."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from habit_assistant.config import Config
from habit_assistant.core import commands, heatmap, timeutil, user_prefs
from habit_assistant.core.commands import Command
from habit_assistant.core.habits import HabitRegistry

REGISTRY = HabitRegistry.from_config(Config())
CORE_DIR = Path(__file__).resolve().parent.parent / "src" / "habit_assistant" / "core"
MAIN_PY = Path(__file__).resolve().parent.parent / "src" / "habit_assistant" / "main.py"


# ===========================================================================
# AC9 -- golden precedence corpus (captured from the OLD if-chain dispatch,
# before the Stage 3 table conversion). >=40 required; 102 cases here,
# covering every one of the 27 matcher kinds, the "falls through to None"
# adversarial near-misses for the Thai-alias matchers, and all 3 invariants.
# ===========================================================================

GOLDEN_CORPUS: list[tuple[str, Command | None]] = [
    ('500ml', None),
    ('10 min stretch', None),
    ('ดื่มน้ำ 2 แก้ว', None),
    ('wrote in my journal today', None),
    ('undo', Command(kind='undo')),
    ('/undo', Command(kind='undo')),
    ('ยกเลิก', Command(kind='undo')),
    ('delete that entry', Command(kind='undo')),
    ('change it to 500ml', Command(kind='edit', category='water', value_num=500.0)),
    ('make that 10 min', Command(kind='edit', category='stretch', value_num=10.0)),
    ('แก้ไขล่าสุดเป็น 300ml', Command(kind='edit', category='water', value_num=300.0)),
    ('change it to what?', None),
    ('edit that to banana', None),
    ('snooze', Command(kind='snooze')),
    ('snooze 30', Command(kind='snooze', minutes=30)),
    ('/snooze 15 min', Command(kind='snooze', minutes=15)),
    ('เลื่อนก่อน', Command(kind='snooze')),
    ('เลื่อน 20 นาที', Command(kind='snooze', minutes=20)),
    ('/target', Command(kind='target', target_action='show_all')),
    ('/target water 2000', Command(kind='target', category='water', value_num=2000.0, target_action='set')),
    ('/target water default', Command(kind='target', category='water', target_action='clear')),
    ('set water goal to 2500ml', Command(kind='target', category='water', value_num=2500.0, target_action='set')),
    ("change water's goal to 3l", Command(kind='target', target_action='usage')),
    ('ตั้งเป้า น้ำ 2000', Command(kind='target', category='water', value_num=2000.0, target_action='set')),
    ('/remind water 08:00 20:00', Command(kind='remind', category='water', times=['08:00', '20:00'])),
    ('/remind water off', Command(kind='remind', category='water', times=['off'])),
    ('เตือน น้ำ 08:00', Command(kind='remind', category='water', times=['08:00'])),
    ('เตือน ๆ หน่อยนะ', None),
    ('/start', Command(kind='start')),
    ('/users', Command(kind='users')),
    ('/approve 12345', Command(kind='approve', target_chat='12345')),
    ('/block', Command(kind='block')),
    ('/invite 999', Command(kind='invite', target_chat='999')),
    ('/audit', Command(kind='audit')),
    ('/audit 10', Command(kind='audit', limit=10)),
    ('ประวัติ', Command(kind='audit')),
    ('ประวัติ 5', Command(kind='audit', limit=5)),
    ('ประวัติศาสตร์ไทย', None),
    ('/lang th', Command(kind='lang', pref_value='th')),
    ('/lang', Command(kind='lang')),
    ('ภาษา th', Command(kind='lang', pref_value='th')),
    ('/quiet 22:00-07:00', Command(kind='quiet', pref_value='22:00-07:00')),
    ('/quiet off', Command(kind='quiet', pref_value='off')),
    ('เงียบ off', Command(kind='quiet', pref_value='off')),
    ('/checkin', Command(kind='checkin')),
    ('/checkin on', Command(kind='checkin', pref_value='on')),
    ('เช็คอิน', Command(kind='checkin')),
    ('/dnd 22:00-07:00', Command(kind='quiet', pref_value='22:00-07:00')),
    ('งดรบกวน off', Command(kind='quiet', pref_value='off')),
    ('/dashboard', Command(kind='dashboard')),
    ('/dashboard on', Command(kind='dashboard', pref_value='on')),
    ('แดชบอร์ด', Command(kind='dashboard')),
    ('/history', Command(kind='history')),
    ('/history water 10', Command(kind='history', category='water', limit=10)),
    ('ย้อนหลัง', Command(kind='history')),
    ('ย้อนหลัง น้ำ 5', Command(kind='history', category='water', limit=5)),
    ('ย้อนหลังไปสามปีที่แล้ว', None),
    ('/heatmap', Command(kind='heatmap')),
    ('/heatmap water 8', Command(kind='heatmap', category='water', limit=8)),
    ('ปฏิทิน', Command(kind='heatmap')),
    ('ปฏิทินจีนปีนี้', None),
    ('/records', Command(kind='records')),
    ('/records water', Command(kind='records', category='water')),
    ('สถิติ', Command(kind='records')),
    ('/trends', Command(kind='trends')),
    ('/trends water', Command(kind='trends', category='water')),
    ('แนวโน้ม', Command(kind='trends')),
    ('/wrapped', Command(kind='wrapped')),
    ('/recap', Command(kind='wrapped')),
    ('สรุปเดือน', Command(kind='wrapped', pref_value='month')),
    ('การ์ดสรุป', Command(kind='wrapped')),
    ('/addhabit id=coffee|en=coffee|th=กาแฟ|type=numeric', Command(kind='addhabit', fields={'id': 'coffee', 'en': 'coffee', 'th': 'กาแฟ', 'type': 'numeric'})),
    ('เพิ่มนิสัย id=coffee|en=coffee', Command(kind='addhabit', fields={'id': 'coffee', 'en': 'coffee'})),
    ('/delhabit water', Command(kind='delhabit', category='water')),
    ('ลบนิสัย น้ำ', Command(kind='delhabit', category='water')),
    ('/log', Command(kind='log')),
    ('บันทึก', Command(kind='log')),
    ('/routine', Command(kind='routine', routine_action='list')),
    ('/routine morning', Command(kind='routine', routine_action='run', routine_name='morning')),
    ('กิจวัตร', None),
    ('/cadence water 3', Command(kind='cadence', category='water', value_num=3.0)),
    ('/cadence water off', Command(kind='cadence', category='water', pref_value='off')),
    ('ต่อสัปดาห์ น้ำ 3', Command(kind='cadence', category='water', value_num=3.0)),
    ('กี่ครั้งต่อสัปดาห์ น้ำ 3', Command(kind='cadence', category='water', value_num=3.0)),
    ('/pause water 3d', Command(kind='pause', category='water', pref_value='3d')),
    ('พัก น้ำ 3d', Command(kind='pause', category='water', pref_value='3d')),
    ('หยุดพัก', None),
    ('/resume water', Command(kind='resume', category='water')),
    ('กลับมา', None),
    ('ต่อ', None),
    ('/help', Command(kind='help')),
    ('ช่วยเหลือ', Command(kind='help')),
    ('วิธีใช้', Command(kind='help')),
    ('/habits', Command(kind='habits')),
    ('นิสัย', Command(kind='habits')),
    ('how much water today?', Command(kind='query')),
    ('did I stretch today', Command(kind='query')),
    ('กี่แก้วน้ำวันนี้', Command(kind='query')),
    ('น้ำเท่าไหร่', Command(kind='query')),
    ('อาบน้ำหรือยัง', Command(kind='query')),
    ('is this good?', Command(kind='query')),
    ('น้ำ？', Command(kind='query')),
]


def test_golden_corpus_has_at_least_40_cases():
    assert len(GOLDEN_CORPUS) >= 40


@pytest.mark.parametrize("text,expected", GOLDEN_CORPUS)
def test_dispatch_matches_precedence_captured_from_the_old_if_chain(text, expected):
    assert commands.dispatch(text, REGISTRY) == expected


# ===========================================================================
# AC9 -- the 3 named invariants (rule 14), each a dedicated behavioral test.
# ===========================================================================


def test_invariant_cadence_precedes_query_for_ki_khrang_stem():
    """`กี่ครั้งต่อสัปดาห์` contains `กี่`, one of `_QUERY_PATTERNS`'s own
    substring anchors -- were cadence not checked before query in the
    table, this would misroute to kind='query' instead of 'cadence'."""
    result = commands.dispatch("กี่ครั้งต่อสัปดาห์ น้ำ 3", REGISTRY)
    assert result == Command(kind="cadence", category="water", value_num=3.0)


def test_invariant_edit_commit_on_trigger_terminates_before_query():
    """"change it to what?" matches the edit TRIGGER, but "what?" fails
    NUMBER parsing -- must return None immediately (commit-on-trigger),
    never falling through to snooze/target/.../query despite the trailing
    "?" that would otherwise satisfy `_match_query`'s own anchor."""
    assert commands.dispatch("change it to what?", REGISTRY) is None
    assert commands.dispatch("edit that to banana", REGISTRY) is None


def test_invariant_query_is_the_only_substring_matcher_and_stays_last():
    """`audit`'s ประวัติ and `history`'s ย้อนหลัง must each resolve to their
    OWN kind, never 'query', proving query (the only substring/`.search`
    matcher) never intercepts an earlier row's whole-message-anchored
    trigger; a genuine trailing-"?" query still resolves correctly."""
    assert commands.dispatch("ประวัติ", REGISTRY) == Command(kind="audit")
    assert commands.dispatch("ย้อนหลัง", REGISTRY) == Command(kind="history")
    assert commands.dispatch("อาบน้ำหรือยัง", REGISTRY) == Command(kind="query")


# ===========================================================================
# AC9 -- structural guard directly on `commands._MATCHERS` (the test-side
# counterpart to `commands._assert_dispatch_invariants`, which runs the
# same 3 checks at import time on the production side).
# ===========================================================================

_EXPECTED_ROW_ORDER = [
    "undo", "edit", "snooze", "target", "remind", "access", "audit", "lang",
    "quiet", "checkin", "dnd", "dashboard", "history", "heatmap", "records",
    "trends", "wrapped", "addhabit", "delhabit", "log", "routine", "cadence",
    "pause", "resume", "help", "habits",
    "guide",  # SPEC-v1.10.md R-SS8 (shared surface): additive 28th row, before "query" (R-SS8's own stated placement).
    "query",
]  # fmt: skip


def test_matchers_table_has_all_27_rows_in_the_exact_pre_conversion_order():
    """Name kept for historical continuity with the Stage 3 refactor this
    file documents (the ORIGINAL 27-row conversion, still exactly
    reproduced here) -- `_EXPECTED_ROW_ORDER` above now carries one
    additive 28th row (SPEC-v1.10.md "guide")."""
    assert [m.kind for m in commands._MATCHERS] == _EXPECTED_ROW_ORDER


def test_matchers_table_query_is_the_last_row():
    assert commands._MATCHERS[-1].kind == "query"


def test_matchers_table_cadence_row_precedes_query_row():
    kinds = [m.kind for m in commands._MATCHERS]
    assert kinds.index("cadence") < kinds.index("query")


def test_matchers_table_edit_is_the_sole_commit_on_trigger_row():
    commit_rows = [m.kind for m in commands._MATCHERS if m.triggered is not None]
    assert commit_rows == ["edit"]


def test_production_side_invariant_guard_already_ran_clean_at_import():
    """`commands._assert_dispatch_invariants(commands._MATCHERS)` runs once
    at module import -- if this test file imported `commands` at all
    (it did, at the top), a violation would have raised AssertionError
    before any test in this file even collected. Re-running it here makes
    that implicit proof explicit and independently re-checkable."""
    commands._assert_dispatch_invariants(commands._MATCHERS)


# ===========================================================================
# AC10 -- the 5 EASY dedup clusters. One regression test per cluster
# (byte-identical output through the new canonical implementation) plus a
# source-sweep per cluster proving the removed duplicate `def`s are
# actually gone -- the permanent, re-checkable form of the "grep proof"
# in IMPL-refactor-s3.md.
# ===========================================================================

# --- (a) language-pref: 4 remaining copies -> core/user_prefs.py ----------

_LANG_PREF_CONSOLIDATED_FILES = ["announce.py", "checkins.py", "dashboard.py", "nudge.py"]


class _FakeUserRow(dict):
    pass


class _FakeDbForLangPref:
    def __init__(self, language_pref: str | None):
        self._row = _FakeUserRow(language_pref=language_pref) if language_pref is not None else None

    def get_user(self, chat_id: str):
        return self._row


def test_user_prefs_stored_language_pref_byte_identical_to_the_removed_copies():
    assert user_prefs.stored_language_pref(_FakeDbForLangPref("th"), "u1") == "th"
    assert user_prefs.stored_language_pref(_FakeDbForLangPref(None), "u1") == "auto"

    class _RaisingDb:
        def get_user(self, chat_id):
            raise RuntimeError("boom")

    assert user_prefs.stored_language_pref(_RaisingDb(), "u1") == "auto"  # fail-open


@pytest.mark.parametrize("filename", _LANG_PREF_CONSOLIDATED_FILES)
def test_language_pref_duplicate_definition_removed(filename):
    text = (CORE_DIR / filename).read_text(encoding="utf-8")
    assert "def _user_language_pref" not in text
    assert "user_prefs.stored_language_pref(" in text


def test_reminders_own_language_pref_wrapper_is_untouched_and_still_delegates():
    """`core/reminders.py:_user_language_pref` predates Stage 3 (SPEC-v1.8.md
    integration) and is deliberately OUT of Stage 3's scope -- it already
    delegates to `user_prefs.stored_language_pref` and is directly
    referenced by name in its own module docstrings, so it stays a thin
    wrapper rather than being inlined away."""
    from habit_assistant.core import reminders

    text = (CORE_DIR / "reminders.py").read_text(encoding="utf-8")
    assert "def _user_language_pref" in text
    assert reminders._user_language_pref(_FakeDbForLangPref("en"), "u1") == "en"


# --- (b) `_today*`/`_now_hhmm`: 8 + 3 sites -> core/timeutil.py -----------

_TODAY_FULLY_CONSOLIDATED_FILES = ["records.py", "trends.py", "dashboard.py", "checkins.py", "nudge.py", "query.py", "wrapped.py"]
_NOW_HHMM_FULLY_CONSOLIDATED_FILES = ["reminders.py", "checkins.py", "nudge.py"]


def test_timeutil_today_in_timezone_naive_clock_treated_as_already_local():
    naive = datetime(2026, 8, 24, 23, 30)
    assert timeutil.today_in_timezone(lambda: naive, "Asia/Bangkok") == naive.date()


def test_timeutil_today_in_timezone_aware_utc_clock_converts_to_target_tz():
    aware_utc = datetime(2026, 8, 24, 20, 0, tzinfo=timezone.utc)  # 03:00 next day in Bangkok (+7)
    result = timeutil.today_in_timezone(lambda: aware_utc, "Asia/Bangkok")
    assert result == aware_utc.astimezone(ZoneInfo("Asia/Bangkok")).date()
    assert result.day == 25


def test_timeutil_now_hhmm_naive_clock():
    naive = datetime(2026, 8, 24, 8, 5)
    assert timeutil.now_hhmm(lambda: naive, "Asia/Bangkok") == "08:05"


def test_timeutil_week_days_returns_7_iso_strings_ending_at_the_given_date():
    days = timeutil.week_days(date(2026, 1, 3))  # rolls back across a year boundary
    assert days == [
        "2025-12-28", "2025-12-29", "2025-12-30", "2025-12-31",
        "2026-01-01", "2026-01-02", "2026-01-03",
    ]  # fmt: skip


def test_heatmap_today_in_timezone_wrapper_delegates_to_timeutil():
    """`heatmap._today_in_timezone` is the ONE `_today*` site kept as a
    thin same-named wrapper (mirrors `reminders._user_language_pref`'s own
    precedent) because `tests/test_heatmap_gaps.py` calls it directly by
    name -- proving it still forwards to the canonical implementation
    rather than carrying its own copy of the logic."""
    naive = datetime(2026, 8, 24, 10, 0)
    assert heatmap._today_in_timezone(lambda: naive, "Asia/Bangkok") == timeutil.today_in_timezone(
        lambda: naive, "Asia/Bangkok"
    )


@pytest.mark.parametrize("filename", _TODAY_FULLY_CONSOLIDATED_FILES)
def test_today_helper_duplicate_definition_removed(filename):
    text = (CORE_DIR / filename).read_text(encoding="utf-8")
    assert "def _today_str(" not in text
    assert "def _today_date(" not in text
    assert "def _today_in_timezone(" not in text
    assert "def _today(" not in text
    assert "timeutil.today_in_timezone(" in text


def test_heatmap_today_helper_kept_as_wrapper_not_a_duplicate_body():
    text = (CORE_DIR / "heatmap.py").read_text(encoding="utf-8")
    assert "def _today_in_timezone(clock, tz_name: str) -> date:" in text
    assert "return timeutil.today_in_timezone(clock, tz_name)" in text
    assert "ZoneInfo" not in text  # the inline body (and its only ZoneInfo use) is gone


@pytest.mark.parametrize("filename", _NOW_HHMM_FULLY_CONSOLIDATED_FILES)
def test_now_hhmm_duplicate_definition_removed(filename):
    text = (CORE_DIR / filename).read_text(encoding="utf-8")
    assert "def _now_hhmm(" not in text
    assert "timeutil.now_hhmm(" in text


def test_reminders_own_today_str_config_only_variant_is_untouched():
    """`core/reminders.py:_today_str(config)` takes a DIFFERENT signature
    (no injectable `clock` -- always `datetime.now`) from the 8
    `timeutil.today_in_timezone`-shaped sites -- SPEC-REFACTOR.md's own
    rule 12(b) lists only 8 sites, deliberately excluding this one.
    Out of Stage 3's scope; still present, unconsolidated, by design."""
    text = (CORE_DIR / "reminders.py").read_text(encoding="utf-8")
    assert "def _today_str(config: Config) -> str:" in text


# --- (c) ordinal: already consolidated in Stage 2 (core/confirmation.py) -

def test_ordinal_has_exactly_one_definition_across_the_whole_src_tree():
    """SPEC-REFACTOR.md rule 12(c) names `ordinal`/`_ordinal` (main.py <->
    quicklog.py) as an EASY cluster, but Stage 2 (v1.9.2, core/confirmation.py)
    already consolidated it -- `main.py` only re-exports the name (`# noqa:
    F401 -- back-compat re-export`) for callers that still import it from
    there. Nothing for Stage 3 to do here; this test documents/locks that
    the cluster is genuinely zero-duplication, not silently re-duplicated
    since Stage 2."""
    hits = []
    for path in CORE_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if "def ordinal(" in text or "def _ordinal(" in text:
            hits.append(path.name)
    if MAIN_PY.exists():
        text = MAIN_PY.read_text(encoding="utf-8")
        if "def ordinal(" in text or "def _ordinal(" in text:
            hits.append("main.py")
    assert hits == ["confirmation.py"]


# --- (d) registry-anchored Thai-alias token builder: 7 sites in commands.py

_TH_PATTERN_BUILDER_FUNCS = [
    "_build_target_th_set_pattern",
    "_build_remind_th_pattern",
    "_build_history_th_pattern",
    "_build_heatmap_th_pattern",
    "_build_insights_th_pattern",
    "_build_delhabit_th_pattern",
    "_build_cadence_th_pattern",
]


def test_registry_th_tokens_sorts_longest_first_and_escapes():
    from habit_assistant.core.habits import Habit

    def numeric_habit(id_: str, label_th: str) -> Habit:
        return Habit(
            id=id_,
            type="numeric",
            label_en=id_,
            label_th=label_th,
            unit_en=None,
            unit_th=None,
            goal=None,
            reminder_times=(),
            reminder_text_en=None,
            reminder_text_th=None,
            unit_aliases={},
        )

    tiny_registry = HabitRegistry([numeric_habit("a", "น้ำ"), numeric_habit("ab", "น้ำมาก")])
    tokens = commands._registry_th_tokens(tiny_registry)
    # longest-first: "น้ำมาก" (6 chars) before "น้ำ" (3 chars) before the 2-char id "ab" before "a"
    assert tokens.index("น้ำมาก") < tokens.index("น้ำ")
    assert tokens.index("ab") < tokens.index("a")


@pytest.mark.parametrize("func_name", _TH_PATTERN_BUILDER_FUNCS)
def test_th_pattern_builder_delegates_to_the_shared_token_helper(func_name):
    import inspect

    source = inspect.getsource(getattr(commands, func_name))
    assert "_registry_th_tokens(registry)" in source
    assert "tokens: set[str] = set()" not in source  # the old inline 5-line block is gone


def test_th_pattern_builders_still_carry_their_own_untouched_trigger_literals():
    """The shared helper changed the token-COLLECTION mechanism only --
    each builder's own trigger literal(s) (untouched by this
    consolidation) must still be exactly what's baked into the compiled
    pattern. Byte-identical routing itself is proven by GOLDEN_CORPUS
    above; this spot-checks the literal wasn't accidentally altered."""
    assert commands._build_target_th_set_pattern(REGISTRY).pattern.startswith(r"^(?:ตั้งเป้า|เป้า)\s*(?P<habit>")
    assert commands._build_remind_th_pattern(REGISTRY).pattern.startswith(r"^เตือน\s+(?P<habit>")
    assert commands._build_delhabit_th_pattern(REGISTRY).pattern == (
        r"^ลบนิสัย\s+(?P<habit>" + "|".join(commands._registry_th_tokens(REGISTRY)) + r")$"
    )
    assert commands._build_cadence_th_pattern(REGISTRY).pattern.startswith(
        r"^(?:กี่ครั้งต่อสัปดาห์|ต่อสัปดาห์)\s*(?P<habit>"
    )


# --- (e) week_days: 4 sites -> core/timeutil.py ---------------------------

_WEEK_DAYS_FULLY_CONSOLIDATED_FILES = ["charts.py", "garmin.py", "review.py", "records.py"]


@pytest.mark.parametrize("filename", _WEEK_DAYS_FULLY_CONSOLIDATED_FILES)
def test_week_days_duplicate_definition_removed(filename):
    text = (CORE_DIR / filename).read_text(encoding="utf-8")
    assert "def _week_days(" not in text
    assert "def week_day_strs(" not in text
    assert "timeutil.week_days(" in text


def test_trends_week_days_import_switched_from_records_to_timeutil():
    text = (CORE_DIR / "trends.py").read_text(encoding="utf-8")
    assert "week_day_strs" not in text
    assert "timeutil.week_days(" in text
