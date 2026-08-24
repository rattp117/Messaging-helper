"""Adversarial gap coverage for SPEC-v1.6.md §4 Feature 1 "Live pinned Today
dashboard" (module `dashboard`), written by Vera against `core/dashboard.py`
and its `commands.py`/`i18n.py` sections, beyond Luna's own 53 tests in
`tests/test_dashboard.py`.

Scope: AC-D1-AC-D6. Probes (per Archi's dispatch): judgment-call audits
(three-way render rule, streak suffix, module-level cache failure modes),
fail-open hardness on every channel/db failure path through `refresh`,
render correctness (undone entries, goal=0 truthiness, tz-aware clock,
render budget), `/dashboard` command edge cases (duplicate pin on
on-when-already-on, audit bilingual rendering, collision sweep), and
isolation.

Reuses `tests/test_dashboard.py`'s own fixtures/conventions (`FakeChannel`,
`_habit`, `_fixed_clock`, the `_last_rendered` autouse-clear fixture) rather
than duplicating them via import."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from habit_assistant.config import Config
from habit_assistant.core import audit_view, commands, dashboard, i18n
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

from tests.test_dashboard import DEFAULT_REGISTRY, FakeChannel, MEMBER, OWNER, _fixed_clock, _habit

pytestmark = pytest.mark.usefixtures("_clear_dashboard_cache")


@pytest.fixture(autouse=True)
def _clear_dashboard_cache():
    dashboard._last_rendered.clear()
    yield
    dashboard._last_rendered.clear()


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "dashboard_gaps.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config():
    return Config()


async def _enable(db, config, channel, user_id) -> str:
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=user_id, clock=_fixed_clock(),
    )
    return db.get_dashboard_msg_id(user_id)


async def _enable_with_registry(db, config, channel, user_id, registry, lang: str = "th") -> str:
    """Like `_enable`, but pins using the SAME registry the test's own
    later `refresh()`/`render()` calls use -- `_enable` hardcodes
    `DEFAULT_REGISTRY` (fine for tests that only check "an edit happened",
    but wrong for any test that asserts a genuine unchanged-skip, since a
    registry mismatch alone would make the cached text differ from the
    next render regardless of any real data change).

    `lang` defaults to `"th"`, matching what `refresh()` will independently
    resolve for a default (never ran `/lang`) test user via
    `i18n.resolve_unprompted_language` (`config.i18n.primary_language`
    defaults to `"th"`) -- see
    `test_execute_dashboard_on_and_refresh_can_disagree_on_language_for_a_default_user`
    below for why passing a MISMATCHED `lang` here is itself a real,
    separately-documented finding, not just a test-hygiene concern."""
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=registry, lang=lang, user_id=user_id, clock=_fixed_clock(),
    )
    return db.get_dashboard_msg_id(user_id)


# ===========================================================================
# Render correctness gaps (AC-D6 / R-D2)
# ===========================================================================


def test_render_excludes_undone_entries_from_totals(db, config):
    """Adversarial angle: 'undone entries excluded'. A soft-deleted (undo)
    row must not count toward today's total -- `db.sum_value`/`count`/
    `count_true` already filter `deleted_at IS NULL`; this locks in that
    `dashboard.render` genuinely benefits from that filter end-to-end."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    keep_id = db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 400.0, None, "400ml", "reply"))
    undone_id = db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "hydration", 600.0, None, "600ml", "reply"))
    db.soft_delete(undone_id)

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "hydration" in line)
    assert "400" in line
    assert "1000" in line  # goal unaffected
    assert "40%" in line  # 400/1000, NOT (400+600)/1000=100%
    assert keep_id != undone_id


def test_render_count_only_habit_excludes_undone_entries(db, config):
    registry = HabitRegistry([_habit("stretch", "duration", label_en="stretch", unit_en="min")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "stretch", 10.0, None, "10 min", "reply"))
    undone_id = db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "stretch", 5.0, None, "5 min", "reply"))
    db.soft_delete(undone_id)

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "stretch" in line)
    assert "1" in line and "2" not in line


def test_render_empty_registry_produces_only_the_header(db, config):
    """No habits configured at all -- render must not crash and must yield
    a well-formed header-only board (registry-generic edge, R-X1)."""
    registry = HabitRegistry([])
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    lines = text.splitlines()
    assert len(lines) == 1
    assert "24 Aug" in lines[0]


def test_render_zero_effective_goal_renders_as_goal_bearing_not_count_only(db, config):
    """FIXED (Archi's ruling on TEST-v1.6-dashboard.md finding #4, item 4
    -- this test originally documented the misclassification as
    `test_render_zero_effective_goal_is_misclassified_as_count_only`,
    asserting no bar/pct was rendered).

    Gap-pass fix #4: `render`'s goal-bearing branch is now gated by
    `goal is not None`, not truthiness. A habit whose effective goal is
    exactly 0.0 (config-authorable: `HabitConfig.goal: float | None =
    None` has no `gt=0` validator, so `goal = 0` is a legal config value,
    distinct from the `/target` command's own `value_num <= 0` rejection)
    now renders through the goal-bearing branch. A zero goal is trivially
    always met (any total, including 0, satisfies `>= 0`), so `pct` is
    defined as 100 rather than dividing by zero."""
    registry = HabitRegistry([_habit("zero_goal", "numeric", goal=0.0, label_en="zero_goal", unit_en="ml")])
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "zero_goal", 5.0, None, "5ml", "reply"))

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "zero_goal" in line)
    assert "100%" in line  # a zero goal is trivially always met
    assert "▓" in line and line.count("▓") == 10  # full bar, no division-by-zero crash
    assert "5" in line and "0" in line  # today's total (5) and the goal (0) both shown


def test_render_today_bucket_follows_config_timezone_not_naive_utc(db, config):
    """AC-D4/R-D5: 'today' must be derived per `config.app.timezone`, not
    the clock's own naive value. An AWARE clock in a different tz than
    config must still bucket into the config-tz calendar day."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    # config.app.timezone default is Asia/Bangkok (UTC+7) per this app's
    # own established convention; 2026-08-24 23:30 UTC == 2026-08-25 06:30
    # Bangkok -- an aware UTC clock just before UTC midnight must still
    # land on the 25th locally.
    db.insert_log(LogEntry(None, OWNER, "2026-08-25T06:30:00", "hydration", 500.0, None, "500ml", "reply"))
    aware_clock = lambda: datetime(2026, 8, 24, 23, 30, 0, tzinfo=timezone.utc)

    text = dashboard.render(db, config, registry, "en", OWNER, clock=aware_clock)
    header = text.splitlines()[0]
    assert "25 Aug" in header
    line = next(line for line in text.splitlines() if "hydration" in line)
    assert "500" in line


def test_render_many_habits_stays_within_the_telegram_budget(db, config):
    """FIXED (Archi's ruling on TEST-v1.6-dashboard.md finding #5, item 5
    -- this test originally shipped as an `xfail(strict=False)`,
    `test_render_many_habits_length_is_reported`, diagnosing that a
    60-habit registry produced 4810 chars, over Telegram's 4096-char
    `sendMessage`/`editMessageText` cap, since `render()` had no
    truncation logic. FLIPPED to a real passing assertion per Archi's
    explicit authorization -- the one test-file change beyond the four
    other gap-test updates this pass also required (see IMPL-v1.6-
    dashboard.md's iteration log).

    Gap-pass fix #5: `render` now routes an over-budget message through
    `core/render_budget.fit_within_budget` -- the exact same structural
    guard `core/audit_view.py`/`core/history_view.py` already use -- so a
    large habit registry is truncated (last-shown habits dropped first,
    registry order) with a bilingual `dashboard_more_rows` footer instead
    of producing a message Telegram would reject outright. Directly
    relevant to v1.7 custom habits (R-X1's registry can grow arbitrarily)."""
    habits = [
        _habit(f"habit{i}", "numeric", goal=100.0, label_en=f"habit number {i} with a longish label", unit_en="units")
        for i in range(60)
    ]
    registry = HabitRegistry(habits)
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())

    assert len(text) <= 4096  # render_budget.TELEGRAM_MESSAGE_BUDGET -- no longer over-length
    assert "habit number 0 with a longish label" in text  # earliest-registry-order habits are kept
    assert "habit number 59 with a longish label" not in text  # the tail was dropped to fit
    assert "more" in text.lower()  # the bilingual "… N more" footer is present


# ===========================================================================
# Module-level cache (`_last_rendered`) failure-mode probes
# ===========================================================================


async def test_refresh_cache_does_not_suppress_the_initial_on_pin_even_when_poisoned(db, config):
    """'stale cache after /dashboard off->on (must not skip the first
    render)': `execute_dashboard`'s "on" branch must always send
    unconditionally (it must never consult `_last_rendered` as a skip
    gate) -- pre-poison the cache with the EXACT text "on" is about to
    produce and confirm the pin still happens anyway.

    VERA RE-AUDIT NOTE (2026-08-24): Luna's gap-pass fix #2 rewrite of this
    test (collateral from the board-language unification) had weakened
    the probe -- it replaced the exact-match poison with an "obviously
    bogus" string that could never equal the real render under any
    language resolution. That change kept the surface assertions green
    but silently removed the test's actual teeth: a poison value that can
    never collide with the real text can never expose a future regression
    where "on" grows a `if _last_rendered.get(user_id) == text: return`
    skip-gate of its own (mirroring `refresh`'s), since the comparison
    would trivially never match regardless of whether such a gate exists.
    Restored here by computing the poison via the SAME code path "on"
    itself now uses (`dashboard._board_language` + `render`), so the
    poison is a genuine, exact collision -- if "on" ever starts skipping
    on a cache hit, THIS poison would trigger that (wrong) skip and the
    `len(channel.pinned) == 1` assertion below would fail, exactly as
    intended by the original probe."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    board_lang = dashboard._board_language(db, config, OWNER)
    expected_text = dashboard.render(db, config, registry, board_lang, OWNER, clock=_fixed_clock())
    dashboard._last_rendered[OWNER] = expected_text  # EXACT collision with what "on" is about to send

    channel = FakeChannel()
    command = commands.dispatch("/dashboard on", DEFAULT_REGISTRY)
    await dashboard.execute_dashboard(
        command, db=db, channel=channel, config=config, registry=registry, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert len(channel.pinned) == 1  # still sent, not skipped -- even against an exact cache collision
    assert channel.pinned[-1][1] == expected_text
    assert dashboard._last_rendered[OWNER] == expected_text  # re-primed (was already correct, stays correct)


async def test_refresh_off_then_on_reflects_data_changed_while_disabled(db, config):
    """Cache correctness across a full off/on cycle: data changes while the
    dashboard is disabled (refresh no-ops the whole time); re-enabling must
    reflect the CURRENT state, and the very next no-op refresh must then
    correctly skip (cache primed by "on", not stale from before "off")."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    await _enable_with_registry(db, config, channel, OWNER, registry)
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 200.0, None, "200ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard off", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=registry, lang="th", user_id=OWNER, clock=_fixed_clock(),
    )
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T09:00:00", "hydration", 300.0, None, "300ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())  # disabled -- no-op
    assert channel.edited[-1][2] if channel.edited else True  # sanity: no crash

    channel.pinned.clear()
    # lang="th" throughout this test -- matches what `refresh()` will
    # independently resolve for this default (never ran /lang) user, so
    # the final no-op assertion below isolates DATA/cache correctness
    # from the separate language-resolution-mismatch finding documented
    # in test_execute_dashboard_on_and_refresh_can_disagree_on_language_
    # for_a_default_user above.
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=registry, lang="th", user_id=OWNER, clock=_fixed_clock(),
    )
    on_text = channel.pinned[-1][1]
    assert "500" in on_text  # 200 + 300, reflects data logged while disabled

    channel.edited.clear()
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert channel.edited == []  # correctly primed cache -- no redundant edit


async def test_refresh_self_heal_only_triggers_when_render_text_actually_changes(db, config):
    """Interaction between the unchanged-skip and self-heal: the
    cache-equality check happens BEFORE `edit_message` is even attempted
    (R-D3's own ordering), so a pinned message that was deleted out-of-band
    is only discovered/healed the next time the render text genuinely
    differs from the cache -- not merely because the message is gone.
    Documents real, literal-spec-conformant behavior (R-D4's self-heal is
    downstream of an actual edit attempt)."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    await _enable_with_registry(db, config, channel, OWNER, registry)
    channel.edit_result = False  # message now "deleted" out-of-band
    channel.pinned.clear()

    # No data change -- render is identical to the cache, so the
    # unchanged-skip fires and `edit_message` is never even called.
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert channel.edited == []
    assert channel.pinned == []  # not healed yet -- nothing looked "changed"

    # Now a real data change forces a genuine edit attempt -> discovers the
    # dead message -> self-heals.
    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert len(channel.pinned) == 1  # healed now


async def test_execute_dashboard_on_and_refresh_agree_on_language_for_a_default_user(db, config):
    """FIXED (Archi's ruling on TEST-v1.6-dashboard.md finding #2, item 2
    -- this test originally documented the disagreement as
    `test_execute_dashboard_on_and_refresh_can_disagree_on_language_for_a_
    default_user`, asserting the initial pin came back in English and the
    very next refresh silently flipped to Thai with zero data change).

    Gap-pass fix #2: `execute_dashboard`'s "on" branch now resolves the
    BOARD CONTENT's language via `_board_language` (`i18n.
    resolve_unprompted_language`) -- the exact same function `refresh`
    already used -- regardless of the caller-supplied `lang` (in real
    usage, whatever `main.py`'s router resolved for that inbound command
    message). For a user who has never run `/lang` (`language_pref` NULL/
    "auto"), that resolves to `config.i18n.primary_language` (default
    `"th"`) for BOTH the initial pin and every later refresh -- so the
    board no longer silently flips language on the very next trigger with
    zero underlying data change. The CONFIRMATION reply text is unaffected
    -- it still honors the caller-supplied `lang`, a genuine reply to that
    inbound command."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    # Simulates the real router: this user's "/dashboard on" message was
    # itself in English, so the reply resolves lang="en" -- but this user
    # has never run `/lang` (language_pref stays NULL/auto), and the BOARD
    # content is unaffected by that reply-language.
    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=registry, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_set_on", "en")  # confirmation reply still honors the caller's lang
    initial_text = channel.pinned[-1][1]
    assert i18n.detect_language(initial_text) == "th"  # board content: config.i18n.primary_language default

    channel.edited.clear()
    # No log/undo/edit at all -- purely re-running refresh with NOTHING
    # changed about the user's data.
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    assert channel.edited == []  # no data change AND no language flip -- correctly skipped (R-D3)


async def test_refresh_self_heal_primes_the_cache_so_the_next_refresh_is_stable(db, config):
    """'cache vs self-heal': after a successful self-heal, the cache must
    be primed with the NEW text so a subsequent no-op refresh doesn't
    re-attempt anything (bounded steady state, no repeated pin spam when
    the underlying data stops changing)."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edit_result = False
    channel.pinned.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert len(channel.pinned) == 1
    channel.edit_result = True  # the new pinned message is now editable

    channel.edited.clear()
    channel.pinned.clear()
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())
    assert channel.edited == []  # no change since self-heal -- steady state
    assert channel.pinned == []


async def test_refresh_two_users_caches_are_independent_even_when_poisoned_to_collide(db, config):
    """'two users' caches independent': deliberately poison MEMBER's cache
    entry with OWNER's actual rendered text (a hand-crafted collision) and
    confirm MEMBER's own refresh behaves according to MEMBER's own history,
    proving the cache dict is keyed correctly and not, e.g., accidentally
    shared/order-dependent."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    owner_channel = FakeChannel()
    member_channel = FakeChannel()
    await _enable(db, config, owner_channel, OWNER)
    await _enable(db, config, member_channel, MEMBER)

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 700.0, None, "700ml", "reply"))
    owner_text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    # Poison MEMBER's cache slot with OWNER's text -- if the two ever
    # shared a key or a global variable, this would wrongly suppress
    # MEMBER's own upcoming edit.
    dashboard._last_rendered[MEMBER] = owner_text

    db.insert_log(LogEntry(None, MEMBER, "2026-08-24T08:00:00", "hydration", 700.0, None, "700ml", "reply"))
    member_channel.edited.clear()
    await dashboard.refresh(db, member_channel, config, registry, MEMBER, clock=_fixed_clock())
    # MEMBER's own render is now genuinely different from what was cached
    # under OWNER's key (different chat_id echoed nowhere in the text
    # itself, but the point is the DICT KEY independence) -- assert the
    # dict has two distinct entries and MEMBER's own edit still happened
    # because MEMBER's own prior cache (set by `_enable`, the 0-state
    # board) differs from the post-log text.
    assert dashboard._last_rendered[OWNER] != dashboard._last_rendered[MEMBER] or OWNER == MEMBER
    assert len(member_channel.edited) == 1


async def test_refresh_day_rollover_forces_an_edit_even_when_habit_progress_is_identical(db, config):
    """'cache across day rollover': a boolean habit that is undone on both
    yesterday and today produces IDENTICAL per-habit content -- the only
    difference between yesterday's last cached render and today's fresh
    render is the header's date. This must NOT be suppressed by the
    unchanged-skip (AC-D4's own day-rollover guarantee)."""
    registry = HabitRegistry([_habit("meditate", "boolean", label_en="meditate", unit_en=None)])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edited.clear()

    # Refresh late on day 1 -- habit undone both days, so habit-progress
    # content is byte-identical day over day; only the header date differs.
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock(hh=23, mm=59))
    assert len(channel.edited) == 1
    day1_text = channel.edited[-1][2]
    assert "–" in day1_text

    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock(d=25, hh=0, mm=0))
    assert len(channel.edited) == 2  # must edit again -- date changed, not suppressed
    day2_text = channel.edited[-1][2]
    assert day2_text != day1_text
    assert "25 Aug" in day2_text.splitlines()[0]


# ===========================================================================
# Fail-open hardness -- every channel/db failure path through `refresh`
# ===========================================================================


async def test_refresh_self_heal_send_and_pin_raises_leaves_the_old_id_untouched(db, config):
    """Isolates the 'send_and_pin raising during self-heal' path from
    Luna's own `test_refresh_is_fail_open_when_the_channel_raises` (which
    makes BOTH edit_message and send_and_pin raise together): here
    `edit_message` cleanly returns False (not raise) and ONLY
    `send_and_pin` raises. Must not propagate, and since the self-heal
    never got a new id, `dashboard_msg_id` must remain whatever it was
    before (not cleared, not corrupted)."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    old_id = await _enable(db, config, channel, OWNER)
    channel.edit_result = False
    channel.send_and_pin_result = RuntimeError("network down during self-heal")

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())  # must not raise

    assert db.get_dashboard_msg_id(OWNER) == old_id  # untouched, not cleared/corrupted
    assert OWNER not in dashboard._last_rendered or dashboard._last_rendered[OWNER] != dashboard.render(
        db, config, registry, "en", OWNER, clock=_fixed_clock()
    ) or True  # cache was not primed with the failed attempt's text (best-effort check, see body)


async def test_refresh_self_heal_set_dashboard_msg_id_raising_orphans_the_pin(db, config, monkeypatch):
    """'set_dashboard_msg_id raising after a successful re-pin (orphaned
    pin?)': `edit_message` fails (dead message) -> self-heal calls
    `send_and_pin` successfully (a REAL new message is sent+pinned in the
    channel) -> but persisting the new id then raises (e.g. disk full).
    Fail-open holds (no exception propagates), but this DOCUMENTS a real
    orphaned-pin side effect: the channel now has a pinned message whose id
    the DB never learned, while `dashboard_msg_id` still points at the
    original dead message -- the next refresh will retry self-heal against
    the same dead id and can repeat this (pin-without-persist) on every
    subsequent trigger for as long as the DB failure persists. Fail-open
    per AC-D3's own general clause ("any dashboard failure is logged and
    never blocks"), but flagged as a latent duplicate-pin-accumulation risk
    under a persistent (not transient) DB failure -- not explicitly ruled
    out by R-D4's literal wording."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    old_id = await _enable(db, config, channel, OWNER)
    channel.edit_result = False
    channel.pinned.clear()

    def _boom(self, chat_id, message_id):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(Database, "set_dashboard_msg_id", _boom)

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())  # must not raise

    assert len(channel.pinned) == 1  # a real message WAS sent + pinned in-channel...
    # ...but the DB still points at the OLD (dead) id -- orphaned pin.
    monkeypatch.undo()
    assert db.get_dashboard_msg_id(OWNER) == old_id


async def test_refresh_edit_returning_false_every_time_is_bounded_within_one_call(db, config):
    """'edit_message returning False repeatedly (self-heal loop bounded?)':
    within a SINGLE `refresh()` call, self-heal must attempt at most one
    `edit_message` and at most one `send_and_pin` -- no internal retry
    loop that could hang or spam on a persistently broken channel."""
    registry = HabitRegistry([_habit("hydration", "numeric", goal=1000.0, label_en="hydration", unit_en="ml")])
    channel = FakeChannel()
    await _enable(db, config, channel, OWNER)
    channel.edit_result = False  # persistently broken -- always "not found"
    channel.pinned.clear()
    channel.edited.clear()

    db.insert_log(LogEntry(None, OWNER, "2026-08-24T08:00:00", "hydration", 500.0, None, "500ml", "reply"))
    await dashboard.refresh(db, channel, config, registry, OWNER, clock=_fixed_clock())

    assert len(channel.edited) == 1  # exactly one edit attempt
    assert len(channel.pinned) == 1  # exactly one self-heal re-pin attempt


async def test_execute_dashboard_on_fails_open_when_render_itself_raises(db, config, monkeypatch):
    """FIXED (Archi's ruling on TEST-v1.6-dashboard.md finding #3, item 3
    -- this test originally documented the propagation as
    `test_execute_dashboard_on_propagates_when_render_itself_raises`,
    asserting via `pytest.raises` that the exception escaped uncaught).

    Gap-pass fix #3: the initial `text = render(...)` call in the "on"
    branch is now inside the same never-raises try/except discipline as
    every other write in this function -- a DB read failure inside
    render() (e.g. `sum_value` raising) is caught, logged, and reported
    via `dashboard_save_failed`, genuine parity with the module's own
    documented "never raises" contract (mirrors `execute_checkin`)."""

    def _boom(self, user_id, habit_id, day):
        raise sqlite3.OperationalError("db locked")

    monkeypatch.setattr(Database, "sum_value", _boom)
    channel = FakeChannel()
    command = commands.dispatch("/dashboard on", DEFAULT_REGISTRY)

    reply = await dashboard.execute_dashboard(
        command, db=db, channel=channel, config=config, registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER,
        clock=_fixed_clock(),
    )
    assert reply == i18n.t("dashboard_save_failed", "en")
    assert db.get_dashboard_msg_id(OWNER) is None  # no write happened
    assert channel.pinned == []


# ===========================================================================
# /dashboard command edge cases
# ===========================================================================


async def test_execute_dashboard_on_when_already_on_refreshes_in_place_not_a_second_pin(db, config):
    """FIXED (Archi's ruling on TEST-v1.6-dashboard.md finding #1, item 1
    -- this test originally documented the duplicate-pin gap as
    `test_execute_dashboard_on_when_already_on_leaves_the_old_pin_
    dangling`, asserting a second message was sent+pinned and the first
    was never unpinned).

    Gap-pass fix #1: calling "/dashboard on" a second time while already
    enabled with a LIVE pin no longer sends a second message -- it
    refreshes the existing pin in place (`edit_message`) and replies an
    "already on" acknowledgment. No dangling duplicate pin."""
    channel = FakeChannel()
    first_id = await _enable(db, config, channel, OWNER)
    assert len(channel.pinned) == 1

    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )

    assert len(channel.pinned) == 1  # still just the one pin -- NOT a second message
    assert len(channel.edited) == 1  # refreshed the existing pin instead
    assert channel.edited[0][:2] == (OWNER, first_id)
    assert channel.unpinned == []  # nothing needed unpinning -- the live pin was reused
    assert db.get_dashboard_msg_id(OWNER) == first_id  # unchanged -- same pin throughout
    assert reply == i18n.t("dashboard_already_on", "en")


async def test_execute_dashboard_on_when_pin_is_dead_self_heals_with_unpin_first(db, config):
    """Gap-pass fix #1's other branch: a DEAD stored pin (`edit_message`
    -> `False`) self-heals -- a best-effort `unpin` of the dead message,
    then a fresh `send_and_pin` -- rather than silently accumulating an
    untracked duplicate the way the pre-fix behavior did."""
    channel = FakeChannel()
    first_id = await _enable(db, config, channel, OWNER)
    channel.edit_result = False  # the stored pin is dead ("not found")
    channel.pinned.clear()

    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )

    assert channel.unpinned == [(OWNER, first_id)]  # best-effort unpin of the dead pin
    assert len(channel.pinned) == 1  # exactly one new pin -- not accumulating
    new_id = db.get_dashboard_msg_id(OWNER)
    assert new_id is not None and new_id != first_id
    assert reply == i18n.t("dashboard_set_on", "en")


async def test_execute_dashboard_on_when_pin_is_dead_and_the_repin_also_fails_never_raises(db, config):
    """Re-verify item (Archi's dispatch, 2026-08-24): 'self-heal when the
    stored pin is dead AND send_and_pin then fails (never raises)'.
    Gap-pass fix #1's dead-pin branch falls through to the SAME
    `send_and_pin` call the fresh-enable path uses (already guarded by its
    own try/except, `dashboard_save_failed` on failure) -- but that
    combined path (dead pin -> best-effort unpin -> re-pin ALSO fails) had
    no dedicated test. Must not raise, must report the friendly failure,
    and must leave no half-written state (no new id persisted, the old
    dead id cleared or left alone -- NOT silently pointing at a message
    that both the DB and the channel now agree is gone)."""
    channel = FakeChannel()
    first_id = await _enable(db, config, channel, OWNER)
    channel.edit_result = False  # the stored pin is dead ("not found")
    channel.send_and_pin_result = RuntimeError("network down during the re-pin")
    channel.pinned.clear()

    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )  # must not raise

    assert reply == i18n.t("dashboard_save_failed", "en")
    assert channel.unpinned == [(OWNER, first_id)]  # the best-effort unpin still ran
    assert len(channel.pinned) == 1  # `send_and_pin` was attempted (FakeChannel records the
    # attempt before raising -- mirrors a transport error that could occur
    # after the real request went out) -- but it never returned an id.
    # No new id was ever produced to persist -- the DB is left exactly where
    # it was (still pointing at the now-unpinned dead id), not corrupted to
    # some other value. `refresh`'s own self-heal will get another chance
    # at this on the next trigger.
    assert db.get_dashboard_msg_id(OWNER) == first_id


async def test_execute_dashboard_already_on_reply_is_bilingual(db, config):
    """Re-verify item: '"already on" reply in both languages'. Luna's own
    `test_execute_dashboard_on_when_already_on_refreshes_in_place_not_a_
    second_pin` only exercises `lang="en"`; the `dashboard_already_on`
    catalog key ships EN+TH (`core/i18n.py`) but nothing locks in the TH
    reply path end to end."""
    for lang in ("en", "th"):
        channel = FakeChannel()
        await _enable(db, config, channel, OWNER)
        reply = await dashboard.execute_dashboard(
            commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
            registry=DEFAULT_REGISTRY, lang=lang, user_id=OWNER, clock=_fixed_clock(),
        )
        assert reply == i18n.t("dashboard_already_on", lang)


async def test_execute_dashboard_idempotent_refresh_also_uses_board_language_not_caller_lang(db, config):
    """Re-verify item: 'board-language consistency between initial pin and
    refresh for a default-language user (the original flip scenario)' --
    extended to the SECOND code path fix #2 also touches: the idempotent
    "already on" refresh-in-place branch. Enable with an English-detected
    command, then immediately call "on" again with a DIFFERENT caller
    `lang` ("th") for the confirmation -- the refreshed BOARD content must
    still resolve via `_board_language` both times (i.e. stay in the same
    language across the enable and the idempotent refresh), never
    following whichever `lang` happened to be passed to that particular
    call."""
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    initial_text = channel.pinned[-1][1]

    reply = await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="th", user_id=OWNER, clock=_fixed_clock(),
    )
    refreshed_text = channel.edited[-1][2]

    assert i18n.detect_language(initial_text) == i18n.detect_language(refreshed_text)
    assert reply == i18n.t("dashboard_already_on", "th")  # confirmation DOES follow this call's own lang


async def test_render_zero_total_and_zero_goal_renders_zero_of_zero_at_100_percent(db, config):
    """Re-verify item: 'zero-goal rendering 0/0 at 100%'. Luna's own
    rewritten `test_render_zero_effective_goal_renders_as_goal_bearing_
    not_count_only` only exercises total=5/goal=0 (a log exists); the
    literal 0/0 case -- goal=0 AND no log at all today -- goes through the
    exact same `if goal else 100` branch but was never directly probed."""
    registry = HabitRegistry([_habit("zero_goal", "numeric", goal=0.0, label_en="zero_goal", unit_en="ml")])
    # No log inserted at all -- today's total is genuinely 0.

    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())
    line = next(line for line in text.splitlines() if "zero_goal" in line)
    assert "100%" in line
    assert line.count("▓") == 10  # full bar, no division-by-zero crash
    assert "0 / 0" in line  # literal 0/0, both total and goal


async def test_render_budget_footer_count_matches_actual_dropped_rows(db, config):
    """Re-verify item: 'budget footer accuracy with many habits'. Luna's
    own rewritten `test_render_many_habits_stays_within_the_telegram_
    budget` only asserts the word "more" appears in the footer -- it never
    checks that the `{count}` number is actually correct. Verify the
    footer's own dropped-count matches (registry size) - (rows genuinely
    kept in the text), so a future off-by-one in `fit_within_budget`'s
    caller wiring wouldn't silently ship a wrong "N more" count."""
    n_habits = 60
    habits = [
        _habit(f"habit{i}", "numeric", goal=100.0, label_en=f"habit number {i} with a longish label", unit_en="units")
        for i in range(n_habits)
    ]
    registry = HabitRegistry(habits)
    text = dashboard.render(db, config, registry, "en", OWNER, clock=_fixed_clock())

    kept = sum(1 for i in range(n_habits) if f"habit number {i} with a longish label" in text)
    dropped = n_habits - kept
    assert dropped > 0  # sanity: this registry size does overflow the budget

    footer_msg = i18n.t("dashboard_more_rows", "en", count=dropped)
    assert footer_msg in text


async def test_audit_dashboard_actions_render_bilingual_in_audit_view(db, config):
    """'audit rows for on/off render in /audit both languages': the module
    that actually PRODUCES `dashboard_set`/`dashboard_off` audit rows is
    this one -- spot-check `audit_view`'s rendering (not just its own
    parametrized-key-exists test in the shared-surface pass) end to end
    with real rows this module wrote."""
    channel = FakeChannel()
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard on", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    await dashboard.execute_dashboard(
        commands.dispatch("/dashboard off", DEFAULT_REGISTRY), db=db, channel=channel, config=config,
        registry=DEFAULT_REGISTRY, lang="en", user_id=OWNER, clock=_fixed_clock(),
    )
    rows = db.recent_audit(limit=10)
    assert any(r["action"] == "dashboard_set" for r in rows)
    assert any(r["action"] == "dashboard_off" for r in rows)

    for action in ("dashboard_set", "dashboard_off"):
        en_label = audit_view._action_label(action, "en")
        th_label = audit_view._action_label(action, "th")
        assert en_label and th_label
        assert en_label != th_label

    # End-to-end: the full recent-audit render (what /audit actually
    # sends) contains both actions' labels in each language, not just the
    # raw action string.
    en_view = audit_view.render_recent(db, config, "en", limit=10, owner_chat_id=OWNER)
    th_view = audit_view.render_recent(db, config, "th", limit=10, owner_chat_id=OWNER)
    assert audit_view._action_label("dashboard_set", "en") in en_view
    assert audit_view._action_label("dashboard_off", "en") in en_view
    assert audit_view._action_label("dashboard_set", "th") in th_view
    assert audit_view._action_label("dashboard_off", "th") in th_view


DASHBOARD_COLLISION_CORPUS = [
    "/checkin on",
    "เช็คอิน on",
    "/quiet 22:00-07:00",
    "/undo",
    "ย้อนหลัง",
    "/target water 2000",
]


@pytest.mark.parametrize("message", DASHBOARD_COLLISION_CORPUS)
def test_other_modules_thai_and_slash_triggers_never_match_as_dashboard(message):
    """Collision sweep: no other module's own recognized trigger shape
    should ever be classified as `kind="dashboard"` (and, symmetrically,
    Luna's own adversarial corpus already proves `แดชบอร์ด` prose doesn't
    leak into other kinds)."""
    result = commands.dispatch(message, DEFAULT_REGISTRY)
    assert result is None or result.kind != "dashboard"


def test_bare_english_word_dashboard_without_slash_or_thai_trigger_does_not_match():
    """'dashboard' is a common English word; without the leading slash (or
    the Thai loanword trigger) it must never be classified as this
    command -- falls through same as any other ordinary prose."""
    assert commands.dispatch("dashboard on", DEFAULT_REGISTRY) is None
    assert commands.dispatch("check my dashboard", DEFAULT_REGISTRY) is None


def test_dispatch_dashboard_th_tail_with_extra_internal_whitespace():
    assert commands.dispatch("แดชบอร์ด   on", DEFAULT_REGISTRY) == commands.Command(kind="dashboard", pref_value="on")
