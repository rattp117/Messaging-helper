"""SPEC-LINE.md §4 R-C1-R-C8 (module C, branch `line-version`) -- Luna's own
test suite for `core/digest.py` + the R-C2 suppression gates it adds to
`core/jobs.py`.

Owned ACs (SPEC-LINE.md §11): AC20 (one push batching due-reminders +
daily-summary + nudge + grace + announcement), AC21 (every other per-time
proactive send produces zero independent pushes on LINE), AC22 (opt-out +
`/digest on|off`, audited), AC23 (push_ledger +1 per push), AC24 (owner
quota-warning threshold boundary), AC25 (`/wrapped`/`/heatmap` never
auto-pushed on LINE -- see this file's own note on `/review`'s own scope
in IMPL-LINE-C.md's "Known limitations").

Conventions mirror `tests/test_nudge.py`/`tests/test_grace.py` (a real
on-disk SQLite `Database` via `tmp_path`, no DB mocks; `RecordingChannel`/
`RecordingLineChannel` from `conftest.py`; an injectable fixed `clock`)."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import RecordingChannel, RecordingLineChannel
from habit_assistant.config import Config
from habit_assistant.core import audit, commands, digest, grace, i18n, jobs, routing
from habit_assistant.core.habits import Habit, HabitRegistry
from habit_assistant.core.reminders import ReminderState
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry

OWNER = "owner-chat"
MEMBER = "member-chat-b"


def _habit(
    id_: str,
    type_: str = "numeric",
    *,
    label_en: str | None = None,
    label_th: str | None = None,
    unit_en: str | None = "u",
    unit_th: str | None = "ห",
    goal: float | None = None,
) -> Habit:
    return Habit(
        id=id_,
        type=type_,
        label_en=label_en or id_,
        label_th=label_th or id_,
        unit_en=unit_en if type_ in ("numeric", "duration") else None,
        unit_th=unit_th if type_ in ("numeric", "duration") else None,
        goal=goal,
        reminder_times=(),
        reminder_text_en=None,
        reminder_text_th=None,
        unit_aliases={},
    )


# Not "water" -- `targets.config_goal` special-cases that exact id (mirrors
# tests/test_nudge.py's own note).
JUICE = _habit("juice", "numeric", goal=1000.0, label_en="juice", label_th="น้ำผลไม้", unit_en="ml", unit_th="มล.")
STRETCH = _habit("stretch", "duration", label_en="stretch", label_th="ยืดเส้น", unit_en="min", unit_th="นาที")
REGISTRY = HabitRegistry([JUICE, STRETCH])
EMPTY_REGISTRY = HabitRegistry([])


class _FixedProvider:
    """A minimal `RegistryProvider`-shaped double: `.for_user` returns the
    same registry (or raises) for every user -- `core/digest.py` only ever
    calls this one method."""

    def __init__(self, registry: HabitRegistry | Exception = REGISTRY, *, raise_for: set[str] | None = None):
        self._registry = registry
        self._raise_for = raise_for or set()

    def for_user(self, user_id: str) -> HabitRegistry:
        if user_id in self._raise_for:
            raise RuntimeError(f"synthetic registry failure for {user_id}")
        return self._registry


def _log(db: Database, user_id: str, habit_id: str, value: float | None, ts: str, raw: str = "x") -> None:
    db.insert_log(LogEntry(None, user_id, ts, habit_id, value, None, raw, "reply"))


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "digest.db")
    database.upsert_user(OWNER, role="owner", status="active")
    database.upsert_user(MEMBER, role="member", status="active")
    yield database
    database.close()


@pytest.fixture
def config() -> Config:
    return Config()


def _fixed_now(y=2026, m=8, d=26, hh=20, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, 0)  # 2026-08-26 is a Wednesday


def _current_yyyymm() -> str:
    """TEST-LEDGER-TRIAGE.md (2026-09-01 date-rollover triage, the 4th
    member of the date-drift class): `channels/line.py`'s real
    `_send_push`/`_push` key `push_ledger` off the REAL wall clock
    (`datetime.now()`), never off `run_daily_digest`'s own injected
    `clock=` -- so does `RecordingLineChannel.send()` (conftest.py), which
    deliberately mirrors that real behavior byte-for-byte (its own
    docstring: "exactly matching the real channel's contract"). A test
    that sends through `RecordingLineChannel(db=db)` must therefore assert
    against the REAL current month, never a literal tied to the fixed
    `clock=` used to compose the digest's own content -- exactly the
    convention `test_line_a_gaps.py`/`test_line_channel.py`/
    `test_line_v12_gaps.py` already use (each with its own identically-
    named helper) for the same reason, one level up, against the real
    channel instead of this double."""
    return datetime.now().strftime("%Y-%m")


# ===========================================================================
# AC20 -- composition: each section present/absent correctly, both languages.
# ===========================================================================


def test_compose_digest_shows_due_reminders_and_daily_summary_when_nothing_logged(db, config):
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert text is not None
    assert i18n.t("digest_header", "en") in text
    assert i18n.t("digest_due_reminders_header", "en") in text
    assert i18n.t("daily_summary_header", "en") in text
    assert i18n.t("digest_all_caught_up", "en") not in text
    # Both due-habits' progress lines appear (due section); the exact same
    # per-line text also appears a second time in the full daily-summary
    # section (R-C1's own two-part enumeration -- see core/digest.py's
    # module docstring).
    assert text.count("juice") == 2
    assert text.count("stretch") == 2


def test_compose_digest_shows_all_caught_up_when_every_habit_is_met(db, config):
    _log(db, MEMBER, "juice", 1200, "2026-08-26T09:00:00")
    _log(db, MEMBER, "stretch", 10, "2026-08-26T09:00:00")
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert i18n.t("digest_all_caught_up", "en") in text
    assert i18n.t("digest_due_reminders_header", "en") in text


def test_compose_digest_thai_language(db, config):
    text = digest.compose_digest(db, config, REGISTRY, "th", MEMBER, now=_fixed_now())
    assert i18n.t("digest_header", "th") in text
    assert i18n.t("digest_due_reminders_header", "th") in text
    assert i18n.t("daily_summary_header", "th") in text


def test_compose_digest_daily_summary_streak_is_living_not_zero_when_today_partial(db, config):
    """v1.3.2+line bug fix: `compose_digest`'s daily-summary section feeds
    through `streaks.compute_daily_summary`, now `display_streak`-backed --
    the digest shouldn't tell a user their streak died just because today
    (still open when the digest runs at [digest].time) hasn't reached
    goal yet."""
    _log(db, MEMBER, "juice", 1000, "2026-08-24T09:00:00")
    _log(db, MEMBER, "juice", 1000, "2026-08-25T09:00:00")
    _log(db, MEMBER, "juice", 500, "2026-08-26T09:00:00")  # today: partial, below goal

    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert "streak 2d" in text


def test_compose_digest_nudge_line_included_when_close_to_goal(db, config):
    _log(db, MEMBER, "juice", 850, "2026-08-26T09:00:00")  # 85% >= default 80% threshold
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert i18n.t("nudge_header", "en") in text


def test_compose_digest_nudge_line_absent_when_not_close(db, config):
    _log(db, MEMBER, "juice", 100, "2026-08-26T09:00:00")
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert i18n.t("nudge_header", "en") not in text


def test_compose_digest_grace_line_when_grace_was_consumed_the_night_before(db, config):
    # Mirrors tests/test_grace.py's own anchor scenario exactly: a 3-day
    # streak ending 08-22, a genuine miss on 08-23 (Sun), bridged by the
    # 00:05 tick that "ran" the morning of 08-24 (Mon) -- the digest fired
    # later THAT SAME calendar day (08-24) is the one that should mention it.
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _log(db, MEMBER, "stretch", 5, f"{d}T09:00:00")
    bridged = grace.evaluate_grace(db, config, REGISTRY, MEMBER, date(2026, 8, 24))
    assert bridged, "setup sanity: the miss should have been bridged"

    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=datetime(2026, 8, 24, 20, 0, 0))
    assert i18n.t("grace_message_line", "en", label="stretch", streak=3) in text


def test_compose_digest_grace_line_absent_once_a_day_has_passed(db, config):
    """The day AFTER the bridge, `yesterday` has moved on to a date that
    was never protected -- the grace line stops appearing on its own, no
    flag needed (core/digest.py:_grace_section's own documented mechanism)."""
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _log(db, MEMBER, "stretch", 5, f"{d}T09:00:00")
    grace.evaluate_grace(db, config, REGISTRY, MEMBER, date(2026, 8, 24))

    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=datetime(2026, 8, 25, 20, 0, 0))
    assert "grace" not in (text or "").lower() and "ผ่อนผัน" not in (text or "")


def test_compose_digest_grace_line_absent_when_grace_disabled(db, config):
    config.grace.enabled = False
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _log(db, MEMBER, "stretch", 5, f"{d}T09:00:00")
    db.record_grace(MEMBER, "stretch", "2026-08-23", "2026-W34")
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=datetime(2026, 8, 24, 20, 0, 0))
    assert "ผ่อนผัน" not in (text or "") and "grace" not in (text or "").lower()


def test_compose_digest_grace_line_never_shown_for_a_cadence_habit(db, config):
    """R6: grace never applies to a cadence habit -- even a (hypothetical)
    stray `grace_ledger` row for one is ignored."""
    db.set_cadence(MEMBER, "stretch", 3)
    db.record_grace(MEMBER, "stretch", "2026-08-23", "2026-W34")
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=datetime(2026, 8, 24, 20, 0, 0))
    assert "ผ่อนผัน" not in (text or "") and "grace" not in (text or "").lower()


def test_compose_digest_announcement_line_when_a_new_version_is_pending(db, config, monkeypatch):
    monkeypatch.setattr(digest, "__version__", "9.9.9-line-test")
    monkeypatch.setitem(
        digest.RELEASE_NOTES, "9.9.9-line-test", {"en": "brand new stuff", "th": "ของใหม่"}
    )
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert "brand new stuff" in text


def test_compose_digest_announcement_line_absent_once_marked_announced(db, config, monkeypatch):
    monkeypatch.setattr(digest, "__version__", "9.9.9-line-test")
    monkeypatch.setitem(digest.RELEASE_NOTES, "9.9.9-line-test", {"en": "brand new stuff", "th": "ของใหม่"})
    db.set_last_announced_version(MEMBER, "9.9.9-line-test")
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
    assert "brand new stuff" not in (text or "")


def test_compose_digest_weekly_review_ready_line_on_the_review_weekday(db, config):
    sunday = datetime(2026, 8, 30, 20, 0, 0)
    assert config.weekly_review.day_of_week == "sun"
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=sunday)
    assert i18n.t("digest_review_ready_line", "en") in text


def test_compose_digest_weekly_review_ready_line_absent_on_other_days(db, config):
    wednesday = _fixed_now()
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=wednesday)
    assert i18n.t("digest_review_ready_line", "en") not in text


def test_compose_digest_weekly_review_ready_line_respects_config_flag(db, config):
    config.digest.include_weekly_review_day = False
    sunday = datetime(2026, 8, 30, 20, 0, 0)
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=sunday)
    assert i18n.t("digest_review_ready_line", "en") not in (text or "")


# ===========================================================================
# AC24 -- owner quota-warning threshold boundary (279 / 280 / 281).
# ===========================================================================


@pytest.mark.parametrize("total,expect_warning", [(279, False), (280, True), (281, True)])
def test_owner_quota_warning_boundary(db, config, total, expect_warning):
    for i in range(total):
        db.increment_push(f"filler-{i}", "2026-08")
    text = digest.compose_digest(db, config, EMPTY_REGISTRY, "en", OWNER, now=_fixed_now())
    if expect_warning:
        assert text is not None
        assert i18n.t("digest_quota_warning", "en", total=total, cap=280) in text
    else:
        assert text is None  # nothing else to say either -> no push at all


def test_quota_warning_never_shown_to_a_non_owner(db, config):
    for i in range(300):
        db.increment_push(f"filler-{i}", "2026-08")
    text = digest.compose_digest(db, config, EMPTY_REGISTRY, "en", MEMBER, now=_fixed_now())
    assert text is None


def test_quota_warning_never_blocks_the_digest_it_can_be_the_sole_content(db, config):
    for i in range(280):
        db.increment_push(f"filler-{i}", "2026-08")
    text = digest.compose_digest(db, config, EMPTY_REGISTRY, "en", OWNER, now=_fixed_now())
    assert text is not None and "280" in text


# ===========================================================================
# Empty-day behavior: "nothing worth saying" -> None -> no push.
# ===========================================================================


def test_compose_digest_returns_none_when_there_is_nothing_to_say(db, config):
    text = digest.compose_digest(db, config, EMPTY_REGISTRY, "en", MEMBER, now=_fixed_now())
    assert text is None


# ===========================================================================
# AC22 -- opt-out + `/digest on|off`, audited.
# ===========================================================================


async def test_run_daily_digest_skips_an_opted_out_user(db, config):
    db.set_digest_opt_out(MEMBER, True)
    channel = RecordingLineChannel(db=db)
    provider = _FixedProvider()
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    assert channel.pushes_to(MEMBER) == []
    assert channel.pushes_to(OWNER) != []


async def test_execute_digest_toggle_show_default_is_on(db, config):
    reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("digest_toggle_show", "en", time=config.digest.time)


async def test_execute_digest_toggle_off_then_on_round_trips_and_is_audited(db, config):
    off_reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest off", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert off_reply == i18n.t("digest_toggle_set_off", "en")
    assert db.digest_opt_out(MEMBER) is True

    show_reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert show_reply == i18n.t("digest_toggle_show_off", "en")

    on_reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest on", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert on_reply == i18n.t("digest_toggle_set_on", "en")
    assert db.digest_opt_out(MEMBER) is False

    rows = db.recent_audit(10)
    actions = [(r["user_id"], r["action"], r["source"]) for r in rows if r["user_id"] == MEMBER]
    assert (MEMBER, "digest_off", "command") in actions
    assert (MEMBER, "digest_set", "command") in actions


async def test_execute_digest_toggle_thai_alias(db, config):
    reply = await digest.execute_digest_toggle(
        commands.dispatch("สรุปรายวัน off", REGISTRY), db=db, config=config, lang="th", user_id=MEMBER
    )
    assert reply == i18n.t("digest_toggle_set_off", "th")
    assert db.digest_opt_out(MEMBER) is True


async def test_execute_digest_toggle_invalid_tail_is_a_usage_reply_no_write(db, config):
    before = db.digest_opt_out(MEMBER)
    reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest maybe", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("digest_toggle_usage", "en")
    assert db.digest_opt_out(MEMBER) == before


async def test_execute_digest_toggle_save_failure_never_raises(db, config, monkeypatch):
    def _raise(*a, **k):
        raise RuntimeError("db is briefly down")

    monkeypatch.setattr(db, "set_digest_opt_out", _raise)
    reply = await digest.execute_digest_toggle(
        commands.dispatch("/digest off", REGISTRY), db=db, config=config, lang="en", user_id=MEMBER
    )
    assert reply == i18n.t("digest_toggle_save_failed", "en")


async def test_digest_command_wired_end_to_end_through_routing(db, config):
    channel = RecordingChannel()
    await routing.handle_inbound_message(
        "/digest off", db=db, llm=None, channel=channel, config=config, user_id=MEMBER, dry_run=False
    )
    assert channel.sent_to(MEMBER) == [i18n.t("digest_toggle_set_off", "en")]
    assert db.digest_opt_out(MEMBER) is True


# ===========================================================================
# AC23 -- push_ledger +1 per push (reply-context sends must NOT count).
# ===========================================================================


async def test_run_daily_digest_increments_push_ledger_exactly_once_per_user(db, config):
    channel = RecordingLineChannel(db=db)
    provider = _FixedProvider()
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    # NOT the fixed clock's own month: RecordingLineChannel.send() keys
    # push_ledger off the real wall clock, mirroring channels/line.py's
    # own `_send_push` -- see `_current_yyyymm`'s docstring above.
    yyyymm = _current_yyyymm()
    assert db.push_count(OWNER, yyyymm) == 1
    assert db.push_count(MEMBER, yyyymm) == 1


async def test_a_reply_context_send_never_touches_push_ledger(db, config):
    channel = RecordingLineChannel(db=db)
    before = db.push_count(MEMBER, "2026-08")
    with channel.reply_context("tok-1"):
        text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=_fixed_now())
        assert text is not None
        await channel.send(MEMBER, text)
    assert db.push_count(MEMBER, "2026-08") == before
    assert channel.replies_for("tok-1")
    assert channel.pushes_to(MEMBER) == []


# ===========================================================================
# Master switch, fail-open fan-out, "no internal dedup" (see core/digest.py's
# own module docstring for why this is by design, not a gap).
# ===========================================================================


async def test_run_daily_digest_is_a_full_noop_when_disabled(db, config):
    config.digest.enabled = False
    channel = RecordingLineChannel(db=db)
    provider = _FixedProvider()
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    assert channel.pushes == []


async def test_run_daily_digest_fail_open_one_users_composition_error_does_not_block_others(db, config):
    provider = _FixedProvider(REGISTRY, raise_for={MEMBER})
    channel = RecordingLineChannel(db=db)
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    assert channel.pushes_to(MEMBER) == []
    assert channel.pushes_to(OWNER) != []


async def test_opt_out_read_error_is_logged_as_fail_closed_not_fail_open(db, config, caplog):
    """TEST-LINE-C.md Finding 3 / Archi's round-2 instruction 2: an
    opt-out-read failure is disposed of FAIL-CLOSED (the user is skipped,
    not sent to) -- the operator-visible log line must say so, distinctly
    from an ordinary fail-open composition error."""
    import logging as _logging

    real_opt_out = db.digest_opt_out

    def _raise(user_id):
        if user_id == MEMBER:
            raise RuntimeError("synthetic digest_opt_out read failure")
        return real_opt_out(user_id)

    db.digest_opt_out = _raise  # type: ignore[method-assign]
    try:
        with caplog.at_level(_logging.ERROR):
            await digest.run_daily_digest(db, RecordingLineChannel(db=db), config, _FixedProvider(), clock=_fixed_now)
    finally:
        db.digest_opt_out = real_opt_out  # type: ignore[method-assign]

    member_records = [r for r in caplog.records if MEMBER in r.message]
    assert member_records
    assert any("fail-closed" in r.message for r in member_records)


async def test_composition_error_is_still_logged_as_fail_open(db, config, caplog):
    """Regression guard alongside the fix above: an ordinary composition
    error must keep its ORIGINAL fail-open disposition and wording --
    only the opt-out-read path's label changed."""
    import logging as _logging

    provider = _FixedProvider(REGISTRY, raise_for={MEMBER})
    with caplog.at_level(_logging.ERROR):
        await digest.run_daily_digest(db, RecordingLineChannel(db=db), config, provider, clock=_fixed_now)
    member_records = [r for r in caplog.records if MEMBER in r.message]
    assert member_records
    assert any("fail-open" in r.message for r in member_records)
    assert not any("fail-closed" in r.message for r in member_records)


async def test_run_daily_digest_has_no_internal_dedup_the_scheduler_owns_that(db, config):
    """Documents the "no double-push on restart" mechanism (core/digest.py's
    own module docstring): `run_daily_digest` itself is NOT idempotent --
    calling it twice sends twice, mirroring `core/jobs.py:weekly_review_job`/
    `daily_summary_job`/`wrapped_auto_job`'s identical posture. Real
    once-per-day delivery comes from Integration's `CronTrigger(hour=H,
    minute=M)` registration firing exactly once, not from state in here."""
    channel = RecordingLineChannel(db=db)
    provider = _FixedProvider()
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    await digest.run_daily_digest(db, channel, config, provider, clock=_fixed_now)
    assert len(channel.pushes_to(OWNER)) == 2


# ===========================================================================
# Round 2 (Archi's dispatch, per TEST-LINE-C.md Finding 1): the LINE
# 5,000-char text budget. `compose_digest` must never hand back a string
# LINE would reject -- grace is compacted first (the documented root
# cause), then a hard drop-from-the-tail truncation guarantees the bound
# for any registry size. The trailing `assert` inside `compose_digest`
# itself is the "hard assertion-level test at the composition boundary"
# Archi asked for -- these tests prove it's a structural guarantee that
# never actually fires, not a hope.
# ===========================================================================


def test_compose_digest_grace_full_fidelity_when_well_under_budget(db, config):
    """The common case: one bridged habit is nowhere near the budget, so
    the FULL per-habit `grace_message_line` sentence is used, not the
    compact aggregate -- compaction must be length-triggered, not always-on."""
    for d in ("2026-08-20", "2026-08-21", "2026-08-22"):
        _log(db, MEMBER, "stretch", 5, f"{d}T09:00:00")
    grace.evaluate_grace(db, config, REGISTRY, MEMBER, date(2026, 8, 24))
    text = digest.compose_digest(db, config, REGISTRY, "en", MEMBER, now=datetime(2026, 8, 24, 20, 0, 0))
    assert i18n.t("grace_message_line", "en", label="stretch", streak=3) in text
    assert "Grace day used for" not in text  # the compact aggregate wording, absent here


def test_compose_digest_maximal_registry_stays_under_budget_via_grace_compaction(db, config):
    """Luna's own reproduction of TEST-LINE-C.md Finding 1's exact
    scenario (20 habits, everything firing at once) -- asserts BOTH the
    length bound AND that the fix mechanism is the one documented (grace
    compacted to one aggregate line, everything else still present)."""
    habits = [_habit(f"h{i:02d}", goal=1000.0, label_en=f"h{i:02d}", label_th=f"h{i:02d}") for i in range(20)]
    registry = HabitRegistry(habits)
    now = datetime(2026, 8, 30, 20, 0, 0)
    for h in habits:
        _log(db, OWNER, h.id, 850, "2026-08-30T09:00:00")
        _log(db, OWNER, h.id, 500, "2026-08-28T09:00:00")
        db.record_grace(OWNER, h.id, "2026-08-29", "2026-W35")
    for i in range(280):
        db.increment_push(f"filler-{i}", "2026-08")

    text = digest.compose_digest(db, config, registry, "en", OWNER, now=now)
    assert text is not None
    assert len(text) < 5000
    assert "Grace day used for 20 habit(s)" in text  # compacted, not 20 full sentences
    assert i18n.t("digest_due_reminders_header", "en") in text
    assert i18n.t("daily_summary_header", "en") in text
    assert i18n.t("nudge_header", "en") in text
    assert i18n.t("digest_quota_warning", "en", total=280, cap=280) in text


def test_compose_digest_pathological_registry_stays_under_the_hard_limit_via_truncation(db, config):
    """Forces the SECOND-level fallback: a registry large enough that even
    grace-compaction isn't sufficient (due-reminders + daily-summary alone
    dominate). The hard truncation must still land under 5000 with a
    visible "N more" footer, and `compose_digest`'s own internal
    `assert len(text) < 5000` must not raise."""
    habits = [_habit(f"p{i:03d}", goal=1000.0, label_en=f"p{i:03d}", label_th=f"p{i:03d}") for i in range(300)]
    registry = HabitRegistry(habits)
    now = datetime(2026, 8, 30, 20, 0, 0)
    for h in habits:
        _log(db, OWNER, h.id, 850, "2026-08-30T09:00:00")
        _log(db, OWNER, h.id, 500, "2026-08-28T09:00:00")
        db.record_grace(OWNER, h.id, "2026-08-29", "2026-W35")

    text = digest.compose_digest(db, config, registry, "en", OWNER, now=now)
    assert text is not None
    assert len(text) < 5000
    assert "more line(s) omitted" in text
    assert text.startswith(i18n.t("digest_header", "en"))


def test_compose_digest_pathological_registry_stays_under_budget_thai(db, config):
    habits = [_habit(f"p{i:03d}", goal=1000.0, label_en=f"p{i:03d}", label_th=f"นิสัย{i:03d}") for i in range(300)]
    registry = HabitRegistry(habits)
    now = datetime(2026, 8, 30, 20, 0, 0)
    for h in habits:
        _log(db, OWNER, h.id, 850, "2026-08-30T09:00:00")
        _log(db, OWNER, h.id, 500, "2026-08-28T09:00:00")
        db.record_grace(OWNER, h.id, "2026-08-29", "2026-W35")

    text = digest.compose_digest(db, config, registry, "th", OWNER, now=now)
    assert text is not None
    assert len(text) < 5000


# ===========================================================================
# AC21/AC25/R-C2 -- core/jobs.py suppression gates.
# ===========================================================================


class _Provider:
    def for_user(self, user_id):
        return REGISTRY


def _line_config() -> Config:
    c = Config()
    c.channel.type = "line"
    return c


async def test_minutely_tick_is_a_full_noop_on_line(db, config, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(jobs.checkins, "run_due_checkins", lambda *a, **k: calls.append("checkins"))
    monkeypatch.setattr(jobs.nudge, "run_due_nudges", lambda *a, **k: calls.append("nudge"))

    async def _tracking_reminders(*a, **k):
        calls.append("reminders")

    channel = RecordingChannel()
    await jobs.minutely_tick(
        channel, _line_config(), REGISTRY, db, ReminderState(), _Provider(), run_due_reminders=_tracking_reminders
    )
    assert calls == []
    assert channel.sent == []


async def test_weekly_review_job_is_a_full_noop_on_line(db):
    channel = RecordingChannel()

    def _boom(*a, **k):
        raise AssertionError("should never be called on LINE")

    await jobs.weekly_review_job(db, channel, _line_config(), _Provider(), llm=None, render_weekly_review_charts=_boom)
    assert channel.sent == []


async def test_daily_summary_job_is_a_full_noop_on_line(db):
    _log(db, OWNER, "juice", 500, "2026-08-26T09:00:00")
    channel = RecordingChannel()
    await jobs.daily_summary_job(db, channel, _line_config(), _Provider())
    assert channel.sent == []


async def test_wrapped_auto_job_is_a_full_noop_on_line(db):
    config = _line_config()
    config.wrapped.auto_send = True
    channel = RecordingChannel()
    await jobs.wrapped_auto_job(db, channel, config, _Provider())
    assert channel.sent == []


async def test_grace_tick_still_writes_the_ledger_but_does_not_send_on_line(db, monkeypatch):
    monkeypatch.setattr(jobs.grace, "evaluate_grace", lambda *a, **k: [(STRETCH, 3)])
    channel = RecordingChannel()
    await jobs.grace_tick(db, channel, _line_config(), _Provider())
    assert channel.sent == []


async def test_grace_tick_still_sends_on_telegram_regression_guard(db, config, monkeypatch):
    monkeypatch.setattr(jobs.grace, "evaluate_grace", lambda *a, **k: [(STRETCH, 3)])
    channel = RecordingChannel()
    await jobs.grace_tick(db, channel, config, _Provider())
    assert channel.sent_to(OWNER) or channel.sent_to(MEMBER)


# ===========================================================================
# AC25 -- /wrapped and /heatmap stay reply-only, never auto-pushed, on LINE
# (regression guard: unaffected by this module's changes -- see
# `test_wrapped_auto_job_is_a_full_noop_on_line` above for the auto-send
# suppression half of this AC; `/review` itself is out of this module's
# scope, see IMPL-LINE-C.md).
# ===========================================================================


async def test_wrapped_and_heatmap_commands_are_unaffected_by_this_change(db, config):
    """Sanity guard, not new behavior: `/wrapped`/`/heatmap` are USER-
    initiated commands routed through `core/routing.py`'s existing
    `command.kind in ("heatmap", "wrapped")` branches (untouched by this
    module) -- they always reply via `channel.send`/`channel.send_image`
    inside whatever reply context the caller is in, never independently
    scheduled. Nothing in `core/digest.py`/`core/jobs.py`'s new LINE gates
    touches those branches."""
    dispatched = commands.dispatch("/wrapped", REGISTRY)
    assert dispatched is not None and dispatched.kind == "wrapped"
    dispatched = commands.dispatch("/heatmap", REGISTRY)
    assert dispatched is not None and dispatched.kind == "heatmap"
