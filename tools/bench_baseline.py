"""Behavior-preserving-refactor perf benchmark (SPEC-REFACTOR.md).

Scratch DB only -- NEVER touches data/habits.db. Seeds 3 active users with
~1 year of realistic logs, then measures per-tick DB round-trips + wall time
for the scheduler fan-outs and the typed-message pipeline. Re-runnable
before/after each refactor stage to report honest deltas against the
measured baseline embedded in SPEC-REFACTOR.md section 8.

Sections H and I are written to be implementation-independent (they issue
their own raw SQL / explicit PRAGMA toggles rather than relying on whatever
`Database.sum_value`/the connection's ambient `synchronous` setting
currently do) so this same script keeps producing a fair FULL-vs-NORMAL and
LIKE-vs-range A/B comparison even after Stage 1 lands (once `sum_value` is
range-bound and the connection opens with `synchronous=NORMAL` by default).

Run:  .venv\\Scripts\\python.exe  (with PYTHONPATH=src)  tools/bench_baseline.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from habit_assistant.config import Config
from habit_assistant.core import checkins, nudge
from habit_assistant.core.habits import HabitRegistry
from habit_assistant.core.registry_provider import RegistryProvider
from habit_assistant.core.reminders import ReminderState, run_due_reminders
from habit_assistant.storage.db import Database
from habit_assistant.storage.models import LogEntry
import habit_assistant.main as main_mod

SCRATCH = Path(tempfile.gettempdir()) / "habit_bench_scratch.db"
for ext in ("", "-wal", "-shm"):
    p = Path(str(SCRATCH) + ext)
    if p.exists():
        p.unlink()

USERS = ["1001", "1002", "1003"]
YEAR_DAYS = 365

# ---- query counter via set_trace_callback -------------------------------
_qcount = {"n": 0}
def _tracer(sql):
    _qcount["n"] += 1

def reset_q():
    _qcount["n"] = 0

def qn():
    return _qcount["n"]


class FakeChannel:
    """No-op channel covering every method the hot paths may call."""
    def __init__(self):
        self.sends = 0
    async def send(self, chat_id, text, *, disable_notification=False):
        self.sends += 1
    async def send_actionable(self, chat_id, text, buttons, *, disable_notification=False):
        self.sends += 1
    async def send_image(self, chat_id, image, caption=None, *, disable_notification=False):
        self.sends += 1
    async def pin_message(self, *a, **k):
        return None
    async def edit_message(self, *a, **k):
        return None
    async def unpin_message(self, *a, **k):
        return None
    async def set_message_reaction(self, *a, **k):
        return None
    async def send_and_pin(self, *a, **k):
        return None
    async def aclose(self):
        return None
    async def run(self, *a, **k):
        raise NotImplementedError


class StubLLM:
    async def chat_text(self, *a, **k):
        return ""
    async def aclose(self):
        return None


def seed():
    db = Database(SCRATCH)
    for i, u in enumerate(USERS):
        db.upsert_user(u, role=("owner" if i == 0 else "member"), status="active")
    # ~1 year of logs: water 4/day, stretch 1/day, diary 1/day per user.
    base = datetime.now() - timedelta(days=YEAR_DAYS)
    rows = []
    for u in USERS:
        for d in range(YEAR_DAYS):
            day = base + timedelta(days=d)
            for h in range(4):
                ts = day.replace(hour=8 + h * 3, minute=0, second=0, microsecond=0)
                rows.append(LogEntry(None, u, ts.isoformat(timespec="seconds"), "water", 500.0, None, "500ml", "reply", None, "numeric"))
            ts = day.replace(hour=11, minute=0, second=0, microsecond=0)
            rows.append(LogEntry(None, u, ts.isoformat(timespec="seconds"), "stretch", 10.0, None, "10 min", "reply", None, "duration"))
            ts = day.replace(hour=21, minute=30, second=0, microsecond=0)
            rows.append(LogEntry(None, u, ts.isoformat(timespec="seconds"), "diary", None, "note", "note", "reply", None, "text"))
    # bulk insert without per-row commit for seeding speed
    conn = db._conn
    conn.executemany(
        "INSERT INTO logs (user_id, ts, category, value_num, value_text, raw_message, source, habit_type) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(r.user_id, r.ts, r.category, r.value_num, r.value_text, r.raw_message, r.source, r.habit_type) for r in rows],
    )
    conn.commit()
    return db, len(rows)


def fixed_clock(hhmm):
    h, m = hhmm.split(":")
    def _c():
        return datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return _c


def bench(label, fn, iters=200):
    # warmup
    fn()
    reset_q()
    q0 = qn()
    fn()
    q_per = qn() - q0
    ts = []
    for _ in range(iters):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000.0)
    print(f"{label:52s} queries/call={q_per:5d}  median={statistics.median(ts):8.3f} ms  p95={sorted(ts)[int(iters*0.95)]:8.3f} ms")
    return q_per, statistics.median(ts)


def main():
    db, nlogs = seed()
    db._conn.set_trace_callback(_tracer)
    config = Config()
    provider = RegistryProvider(config, db)
    registry = HabitRegistry.from_config(config)
    state = ReminderState()
    ch = FakeChannel()
    llm = StubLLM()

    print(f"seeded {nlogs} logs across {len(USERS)} users, ~{YEAR_DAYS} days")
    print(f"gamification.enabled={config.gamification.enabled}  checkin.enabled={config.checkin.enabled}")
    print(f"base registry habits={len(registry)}  db file size={SCRATCH.stat().st_size//1024} KB")
    print("-" * 110)

    # warm the registry cache once (production keeps it warm after first tick)
    for u in USERS:
        provider.for_user(u)

    # A. reminder tick, nothing due (the every-minute dominant cost)
    def rem_idle():
        asyncio.run(run_due_reminders(ch, config, registry, db, state, clock=fixed_clock("09:17"), registry_for=provider.for_user))
    bench("A. run_due_reminders  NOTHING DUE (09:17)", rem_idle)

    # B. reminder tick, water due (08:00) -> sends fire
    def rem_due():
        asyncio.run(run_due_reminders(ch, config, registry, db, state, clock=fixed_clock("08:00"), registry_for=provider.for_user))
    bench("B. run_due_reminders  WATER DUE (08:00)", rem_due)

    # C. checkin tick at a non-:00 minute (guard should short-circuit)
    def chk_idle():
        asyncio.run(checkins.run_due_checkins(ch, config, registry, db, clock=fixed_clock("09:17"), registry_for=provider.for_user))
    bench("C. run_due_checkins  OFF-HOUR (09:17, guard)", chk_idle)

    # C2. checkin tick on the hour, default-disabled users
    def chk_hour():
        asyncio.run(checkins.run_due_checkins(ch, config, registry, db, clock=fixed_clock("09:00"), registry_for=provider.for_user))
    bench("C2. run_due_checkins ON-HOUR (09:00, disabled)", chk_hour)

    # D. nudge tick off its fixed minute (guard short-circuits)
    def nud_idle():
        asyncio.run(nudge.run_due_nudges(ch, config, registry, db, clock=fixed_clock("09:17"), registry_for=provider.for_user))
    bench("D. run_due_nudges    OFF-MINUTE (09:17, guard)", nud_idle)

    # E. combined per-minute cost at an ordinary minute (all 3 ticks)
    def all_idle():
        asyncio.run(run_due_reminders(ch, config, registry, db, state, clock=fixed_clock("09:17"), registry_for=provider.for_user))
        asyncio.run(checkins.run_due_checkins(ch, config, registry, db, clock=fixed_clock("09:17"), registry_for=provider.for_user))
        asyncio.run(nudge.run_due_nudges(ch, config, registry, db, clock=fixed_clock("09:17"), registry_for=provider.for_user))
    bench("E. ALL THREE TICKS   ordinary minute (09:17)", all_idle)

    # F. typed-log message pipeline "500ml" (zero-LLM preparse hit)
    def msg():
        asyncio.run(main_mod.handle_inbound_message(
            "500ml", db=db, llm=llm, channel=ch, config=config, user_id="1001",
            registry=provider.for_user("1001"), provider=provider, health_monitor=None,
        ))
    bench("F. handle_inbound_message  '500ml' typed log", msg, iters=100)

    # G. provider.for_user COLD (cache miss -> list_user_habits read)
    def cold():
        provider.invalidate("1001")
        provider.for_user("1001")
    bench("G. provider.for_user COLD (invalidate+build)", cold, iters=100)

    print("-" * 110)

    # H. sum_value cost: LIKE-prefix vs explicit range, over 1yr of water
    # rows. Issued as RAW SQL on both sides (not via `db.sum_value`) so this
    # comparison stays meaningful regardless of which form `sum_value`
    # itself currently implements (LIKE pre-Stage-1, range post-Stage-1).
    def like_sum():
        row = db._conn.execute(
            "SELECT COALESCE(SUM(value_num),0) AS total FROM logs "
            "WHERE user_id=? AND category=? AND deleted_at IS NULL AND ts LIKE ?",
            ("1001", "water", "2026-06-15%"),
        ).fetchone()
        return row["total"]
    t = time.perf_counter()
    for _ in range(500):
        like_sum()
    like_ms = (time.perf_counter() - t) / 500 * 1000
    def range_sum():
        row = db._conn.execute(
            "SELECT COALESCE(SUM(value_num),0) AS total FROM logs "
            "WHERE user_id=? AND category=? AND deleted_at IS NULL AND ts >= ? AND ts < ?",
            ("1001", "water", "2026-06-15", "2026-06-16"),
        ).fetchone()
        return row["total"]
    t = time.perf_counter()
    for _ in range(500):
        range_sum()
    range_ms = (time.perf_counter() - t) / 500 * 1000
    print(f"H. sum_value  LIKE-prefix={like_ms:.4f} ms  vs  range-bound={range_ms:.4f} ms  (1yr water rows)")
    # Cross-check: the actual db.sum_value call (whatever it implements
    # today) must agree numerically with both raw forms above -- a
    # byte-identity smoke check baked into the benchmark itself.
    live_value = db.sum_value("1001", "water", "2026-06-15")
    assert live_value == like_sum() == range_sum(), "db.sum_value diverged from both raw LIKE/range forms"

    # I. synchronous FULL vs NORMAL insert throughput in WAL. Explicitly
    # forces each mode right before its own timing loop (rather than
    # relying on the connection's ambient/default setting) so this stays a
    # fair A/B whether the connection was opened with synchronous=FULL
    # (pre-Stage-1 default) or synchronous=NORMAL (post-Stage-1 default).
    def insert_batch(n):
        t = time.perf_counter()
        for i in range(n):
            db.insert_log(LogEntry(None, "1001", datetime.now().isoformat(timespec="seconds"), "water", 1.0, None, "x", "reply", None, "numeric"))
        return (time.perf_counter() - t) / n * 1000
    db._conn.execute("PRAGMA synchronous=FULL;")
    full_ms = insert_batch(300)
    db._conn.execute("PRAGMA synchronous=NORMAL;")
    norm_ms = insert_batch(300)
    print(f"I. insert_log commit  synchronous=FULL={full_ms:.4f} ms/write  vs  NORMAL={norm_ms:.4f} ms/write")

    # J. EXPLAIN QUERY PLAN for the dominant reminder-times read + sum_value
    print("-" * 110)
    for sql, params in [
        ("SELECT time FROM user_reminder_times WHERE user_id=? AND habit_id=? ORDER BY time", ("1001", "water")),
        ("SELECT COALESCE(SUM(value_num),0) FROM logs WHERE user_id=? AND category=? AND deleted_at IS NULL AND ts >= ? AND ts < ?", ("1001", "water", "2026-06-15", "2026-06-16")),
        ("SELECT chat_id FROM users WHERE status='active' ORDER BY created_at", ()),
    ]:
        plan = db._conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
        print("QPLAN:", sql[:60])
        for r in plan:
            print("      ", r["detail"])

    db.close()
    print("\nDONE. scratch db:", SCRATCH)


if __name__ == "__main__":
    main()
