# Spec — v1.9.0 "Life happens" (streak-engine rework) + Recap wrapped card

## 1. Problem statement

v1.9.0 makes the bot fit real life instead of punishing it, and gives users a
screenshot-and-send moment. **Theme A** reworks the one shared streak engine
(`core/streaks.py`) so three gentle capabilities compose coherently: (1)
**weekly-cadence goals** ("gym 3×/week") so rest days stop breaking streaks;
(2) an automatic **grace day** that quietly protects a streak from a single
weekly miss; (3) **pause / vacation mode** that mutes proactive sends and holds
streaks for a planned absence. All three edit the same backward-walk that feeds
the weekly review, milestones, records, dashboard, daily summary and heatmap —
so they are designed as **one** engine rework behind a **hard byte-identical
regression gate**: with no cadence set, no grace consumed and no pause active,
every streak/milestone/review/record/summary/dashboard/heatmap output is
byte-identical to v1.8.1 and the full 3799-test suite stays green (the same gate
discipline as v1.7 AC-5 / v1.8 AC-9). **Theme B** ships a single shareable
**recap "wrapped" PNG** (`/wrapped`) that compounds records + trends + heatmap
into one card, a small **celebration emoji-burst** on milestones/records, and —
the prerequisite that unblocks Thai in every chart — **bundling Noto Sans Thai**
and registering it with matplotlib so the heatmap and the new card render Thai
as glyphs, not tofu boxes. Everything is registry-generic (custom habits from
v1.7 included), per-user isolated, bilingual EN/TH, zero-LLM, additive-migration
only (012), and audit-captured for the new mutations.

Success: a 4×/week runner keeps an unbroken streak across rest days; a single
missed day is silently forgiven once a week with a kind note; "pause water till
Monday" stops the buzzing and keeps the streak; `/wrapped` returns a beautiful
card with correct Thai text; and a user with none of these enabled sees zero
behavior change.

## 2. Inputs

All inbound is chat text (zero-LLM; deterministic regex recognizers mirroring
`/target`, `/checkin`, `/dashboard`). Every command is per-user (scoped by
Telegram `chat_id` = `user_id`) and resolves habits through the acting user's
`RegistryProvider.for_user` registry (base catalog + that user's `user_habits`).

**Cadence declaration (Theme A.1):**
- `/cadence <habit> <N>` — set habit to "N times per week" (N a positive int, 1–7). Thai alias `กี่ครั้งต่อสัปดาห์ <habit> <N>` / `ต่อสัปดาห์ <habit> <N>`.
  - Example: `/cadence gym 3` → gym becomes a 3×/week cadence habit.
- `/cadence <habit> off` (Thai tail `ปิด`/`ค่าเริ่มต้น`) — remove cadence (revert to daily streak).
- At creation: `/addhabit id=gym | type=boolean | en=gym | th=ยิม | cadence=3w` — the new optional `cadence=<N>w` pipe key.

**Grace (Theme A.2):** no user input to configure per-habit (fully automatic).
Global toggle `[grace] enabled` (default `true`). Balance is read-only, shown in
`/habits`.

**Pause (Theme A.3):**
- `/pause [<habit>] <duration>` where `<duration>` is `<N>d` (e.g. `5d`) or `until YYYY-MM-DD` or `until <weekday>`. No habit token = pause **all** habits. Thai aliases `พัก` / `หยุดพัก` (whole-message-anchored or registry-anchored, same discipline as `/remind`).
  - Example: `/pause water until 2026-09-01`; `/pause 5d`; `พัก น้ำ 3d`.
- `/resume [<habit>]` (Thai `กลับมา` / `ต่อ`) — end a pause early; no token resumes all.

**Wrapped card (Theme B):**
- `/wrapped [month]` (alias `/recap [month]`, Thai `สรุปเดือน` / `การ์ดสรุป`). No arg = rolling **last 4 weeks (28 days)**; `month` = current calendar month.

**Storage inputs (read by the reworked engine, written by modules), migration 012:**
```
habit_cadence(user_id TEXT, habit_id TEXT, per_week INTEGER NOT NULL,
              created_at TEXT, PRIMARY KEY(user_id, habit_id))
grace_ledger (user_id TEXT, habit_id TEXT, protected_date TEXT,
              period_key TEXT NOT NULL, created_at TEXT,
              PRIMARY KEY(user_id, habit_id, protected_date))
pauses       (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT,
              habit_id TEXT NULL,            -- NULL = all habits
              start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))
```
Every existing aggregation read stays exactly as today: `db.sum_value / count /
count_true(user_id, habit_id, day)`, `targets.effective_goal(db, habit, config,
user_id)`.

## 3. Outputs

All replies bilingual via `core/i18n.py`. Concrete examples (EN):

- **Cadence set:** `✅ gym is now 3×/week. This week: 1 of 3 ✅` (audit `cadence_set`).
- **Cadence in `/habits`:** `🏋 gym — 3×/week · this week 2 of 3 ✅ · weekly streak 4 weeks`.
- **Grace consumed (one-time, kind):** `🛟 No worries — I used your grace day for water, so your 20-day streak is safe. (one grace per week)` — never punitive; fired exactly once per protected date (audit `grace_consumed`).
- **Grace balance in `/habits`:** `🛟 grace: available this week` or `🛟 grace: used Tue (streak protected)`.
- **Pause set:** `⏸ Paused water until 2026-09-01. Reminders muted, streak held. /resume water to end early.` (audit `pause_set`).
- **Resume:** `▶ Resumed water. Welcome back!` (audit `pause_clear`).
- **Dashboard paused row:** `💧 water ⏸ paused until Mon — streak 12 (held)`.
- **Wrapped card:** a PNG via `channel.send_image(user_id, png, caption)`; caption `🎉 Your last 4 weeks — water, gym, diary. Nice work!` (bilingual). Text fallback on no-matplotlib/render-failure: a bilingual multi-line summary (mirrors heatmap R-H2).
- **Milestone with burst:** `🔥 7-day water streak — nice work, keep it going!\n🎉🎊🥳`.
- **Errors** (friendly, never raise): unknown habit → `cadence_invalid_habit` / `pause_invalid_habit` (lists valid ids, same shape as `target_invalid_habit`); bad N → `cadence_invalid_value`; over-cap duration → `pause_too_long` (states the max); past/invalid `until` date → `pause_invalid_date`; wrapped render failure → text fallback.

Release: `RELEASE_NOTES["1.9.0"]` (en+th) announced once per active user; `/help`
gains `/cadence`, `/pause`, `/resume`, `/wrapped` lines; Telegram menu updated.

## 4. Behavior rules

**Engine rework (shared surface — the heart of Theme A):**

1. `compute_streak(db, config, habit, end_date, user_id) -> int` keeps its **exact signature** (every existing caller — review.py:114, records.py:193, dashboard.py:237, main.py milestone — is unchanged). Internally it now: resolves `goal` once (as today); loads the lookback window's paused-date set and grace-protected-date set **once**; checks cadence once; then walks **daily** (no cadence) or **weekly** (cadence).
2. **Day classification** (`classify_day`): a date is **QUALIFIED** if `streaks.day_qualifies(...)` is true (unchanged rule: goal-met via `sum_value≥goal`, else `count_true>0` boolean / `count>0` otherwise); **NEUTRAL** if it is paused OR grace-protected; **MISSED** otherwise.
3. **Daily walk (no cadence row):** walk backward from `end_date`; QUALIFIED → `streak += 1`; NEUTRAL → skip (do **not** increment, do **not** break — "held"); MISSED → break. **Reduction guarantee:** with no pause rows and no grace rows every day is QUALIFIED-or-MISSED, so the walk is byte-identical to v1.8.1's `compute_streak` (count consecutive qualifying, break on first gap). This is the byte-identical gate's core (Rule 24 / AC3).
4. **Weekly walk (cadence row `per_week=N`):** the streak unit becomes **ISO weeks** (Mon–Sun, `config.app.timezone`). A completed week is **MET** if its qualifying-day count ≥ N; **NEUTRAL** (held) if the week is paused enough that fewer than N non-paused days remain (N was unreachable, not failed); else **MISSED**. Walk back over weeks: MET → `streak += 1`; NEUTRAL → skip; MISSED → break. The **current** (partial) week counts toward the streak only once it is already MET (qualifying-days-so-far ≥ N), so a mid-week streak can never be over-reported.
5. `streak_unit(db, habit, user_id) -> "day" | "week"` returns `"week"` iff the habit has a cadence row; every renderer (milestone message, records celebration, records view, dashboard row, weekly review, daily summary) selects the day/week i18n variant from it. A cadence habit's stored `longest_streak` record is therefore a **week count** and is celebrated/displayed with week wording.
6. **Grace applies only to daily habits.** A habit with a cadence row is inherently rest-tolerant, so grace is never consumed for it and no `grace_ledger` row is ever created for a cadence habit (avoids double tolerance).
7. **Cadence rest day never breaks the streak** (the payoff): a 3×/week habit logged Mon/Wed/Fri, empty Tue/Thu/Sat/Sun, has a MET current week and an unbroken weekly streak (Rule 4, 11).

**Grace (module M2, deterministic, persisted, once-per-week-per-habit):**

8. Grace is **auto-only**; there is no manual spend. At most **one** grace per `(user_id, habit_id)` per ISO week (`period_key = "<iso_year>-W<iso_week>"`). `[grace] enabled=false` disables the entire mechanism (no bridging, no message, no ledger writes → byte-identical to a world without grace).
9. **Consumption is decided and written at exactly one place** — a nightly per-user job (`core/grace.py:evaluate_grace`, run from a 00:05 tick, reusing the `active_user_ids()` fan-out) — never inside the read-only `compute_streak` (which must stay side-effect-free for its many callers). For each **daily** goal/any-entry habit whose **yesterday** is MISSED and whose streak **would break** at yesterday and which has an active streak ≥ 1 ending the day before yesterday and which has **not** used its grace this week: write a `grace_ledger` row for yesterday (`protected_date=yesterday`, `period_key`), audit `grace_consumed`, and send the one kind message (Rule 14). Thereafter `grace_protected_dates` returns that date and the engine treats it as NEUTRAL for every subsequent read — so the streak is preserved consistently across review/records/dashboard/summary/heatmap.
10. The kind message is sent **once** per protected date and never repeated. Wording is always gentle ("no worries", "your N-day streak is safe", "one grace per week"), never punitive, never "streak broken".
11. A **second** miss in the same ISO week is **not** bridged (grace already used) → the streak breaks normally at that day.

**Pause / vacation (module M3):**

12. `/pause [<habit>] <Nd | until DATE>` writes a `pauses` row (`habit_id=NULL` for all-habits). Duration is capped at `[pause] max_days` (default 30); over-cap → `pause_too_long`; a past or unparseable `until` date → `pause_invalid_date`; unknown habit token → `pause_invalid_habit`. Audit `pause_set`.
13. `/resume [<habit>]` deletes the user's active `pauses` rows for that habit (or all if no token); audit `pause_clear`. Idempotent (resuming when not paused → friendly `pause_none_active`).
14. **A paused date is NEUTRAL in the streak** (held, not broken; Rule 2–4). After resume the streak continues across the paused gap. `is_paused(db, config, user_id, habit_id, when)` is true when an active `pauses` row covers `when` and (`habit_id IS NULL` OR matches).
15. **While paused, proactive sends are suppressed** for that user/habit: reminders (`reminders.send_reminder`, added beside the quiet-hours short-circuit), check-ins (`checkins.run_due_checkins`), nudges (`nudge.run_due_nudges`), and the weekly-review / daily-summary inline jobs — each gains a `is_paused(... today)` early-return before its `channel.send`, exactly mirroring the existing quiet-hours suppression pattern.
16. **A voluntary log during a pause still logs, still confirms, and still fires a reactive milestone/record celebration.** Pause suppresses *proactive* nagging, not the user's own actions — crossing a milestone by choice during a pause is celebrated (the milestone check in `handle_inbound_message` is unaffected by pause). The paused day is still NEUTRAL for streak-continuity purposes, but a qualifying log on it counts as QUALIFIED (a real entry beats the neutral default), so genuine progress is never hidden.
17. `/dashboard` and `/habits` show a `⏸ paused until <date>` marker per paused habit and label the streak as held.

**Cadence declaration & display (module M1):**

18. `/cadence <habit> <N>` validates the habit is real (via the acting registry) and `1 ≤ N ≤ 7`; writes `habit_cadence`; audit `cadence_set`. `/cadence <habit> off` deletes the row; audit `cadence_clear`. `/addhabit ... | cadence=<N>w` sets cadence atomically at creation (validated in `habitdef.validate_and_normalize`; a bad value → `addhabit_invalid_cadence`).
19. `weekly_progress(db, config, habit, user_id, today) -> (qualifying_days_this_iso_week, N)` powers the "X of N this week ✅" indicator in `/habits` and the dashboard for cadence habits; a non-cadence habit shows the existing daily line unchanged.
20. Cadence is registry-generic: a custom habit may carry cadence (set at creation or later); all cadence reads/writes are per-user scoped.

**Wrapped card, stickers, font (module M4 + font shared surface):**

21. `/wrapped [month]` renders **one composite PNG** assembling already-computed pieces for the acting user's active registry over the window (default last 28 days; `month` = current calendar month): period totals per habit, best day, longest streak (day/week unit per `streak_unit`), the biggest week-over-week trend, and a mini heatmap strip. It reuses `records.period_total`, `trends`' delta math, and `heatmap`'s intensity grid — no new aggregation. Sent via `channel.send_image` with a bilingual caption.
22. The card is **cadence-aware** (shows a cadence habit's weeks-met streak with week wording), per-user isolated, and registry-generic. matplotlib-unavailable or any render exception → a bilingual **text fallback** (mirrors `heatmap.py` R-H2 exactly; never raises).
23. **Thai renders as glyphs, not tofu.** The font shared surface bundles **Noto Sans Thai** (OFL 1.1) and registers it via `core/fonts.py:register_thai_font()` (called under the `MATPLOTLIB_AVAILABLE` guard from `charts.py`, `heatmap.py`, `wrapped.py`). Registration is **additive**: it `addfont`s the bundled TTF and sets `rcParams["font.family"] = ["DejaVu Sans", "Noto Sans Thai"]` — DejaVu stays **primary** so Latin/digit/English-month rendering in existing charts/heatmap is unchanged (byte-identical, AC6), and matplotlib's per-glyph fallback (≥3.6; 3.11 installed) routes Thai codepoints to Noto.
24. **BYTE-IDENTICAL REGRESSION GATE (hard):** with no `habit_cadence` row, no `grace_ledger` row, no active `pauses` row for any user, and `[grace] enabled` at default, the full v1.8.1 suite (3799 tests) stays green and every `compute_streak` / `crossed_milestone` / `run_daily_summary` / weekly-review-stats / `records.update_on_log` / dashboard / heatmap-intensity output is byte-identical to v1.8.1. Font registration changes no existing non-Thai chart/heatmap byte output.
25. **Celebration "sticker" = emoji-burst, honestly scoped.** Native Telegram `sendSticker` needs a `file_id` from an account-bound sticker set created via @stickers — an external, non-repo-committable, rotation-fragile asset — so **native stickers are out of scope** (§10). The delivered "sticker" is a bundled **emoji-burst** string appended to the existing milestone/record celebration line (e.g. `🎉🎊🥳`), gated by `[wrapped] celebrate_burst` (default `true`), zero-asset and fail-safe.
26. Optional **month-end auto-send** of the wrapped card, gated by `[wrapped] auto_send` (default **false** — opt-in, consistent with this codebase's proactive-send discipline). When on: one **silent** card per active user on the last day of the month, **pause-aware and DND-aware** (skipped for a fully-paused user or inside DND).

**Cross-cutting (all modules):**

27. Zero-LLM everywhere (all recognizers are deterministic regex; no extraction-schema change). Every new mutation writes a fail-open `audit.record` row (`cadence_set/cadence_clear`, `pause_set/pause_clear`, `grace_consumed`) with an `audit_view` label. Migration 012 is additive and idempotent. Every new string lives in `core/i18n.py` with both `en` and `th`.

## 5. Interfaces (signatures)

```python
# ── core/streaks.py (SHARED SURFACE — engine rework; signatures below are NEW
#    or unchanged-signature-but-reworked-internals) ──────────────────────────
DayState = Literal["qualified", "neutral", "missed"]

def classify_day(db, config, habit, day: str, user_id: str, *, goal: float | None,
                 paused_dates: set[str], grace_dates: set[str]) -> DayState: ...

def compute_streak(db, config, habit, end_date: date, user_id: str) -> int:
    """UNCHANGED signature. Now cadence/pause/grace-aware; reduces byte-identical
    to v1.8.1 when the three stores are empty for (user_id, habit)."""

def streak_unit(db, habit, user_id: str) -> Literal["day", "week"]: ...
# day_qualifies / crossed_milestone / compute_daily_summary / run_daily_summary:
# signatures UNCHANGED; crossed_milestone + summary consult streak_unit only for
# wording (integration wiring), not for the numeric streak.

# ── storage/db.py — SHARED read accessors (engine depends on these) ──────────
def get_cadence(self, user_id: str, habit_id: str) -> int | None: ...
def paused_dates(self, user_id: str, habit_id: str, start: str, end: str) -> set[str]: ...
def grace_protected_dates(self, user_id: str, habit_id: str, start: str, end: str) -> set[str]: ...
def active_pauses(self, user_id: str) -> list[sqlite3.Row]: ...   # for dashboard/habits/gating

# ── storage/db.py — MODULE write accessors (disjoint regions) ────────────────
# M1: def set_cadence(self, user_id, habit_id, per_week: int) -> None  /  clear_cadence(...)
# M2: def record_grace(self, user_id, habit_id, protected_date: str, period_key: str) -> None
#     def grace_used_in_week(self, user_id, habit_id, period_key: str) -> bool
# M3: def insert_pause(self, user_id, habit_id: str | None, start: str, end: str) -> None
#     def clear_pauses(self, user_id, habit_id: str | None) -> int

# ── core/cadence.py (M1) ─────────────────────────────────────────────────────
async def execute_cadence(command, *, db, config, registry, lang, user_id, source="command") -> str: ...
def weekly_progress(db, config, habit, user_id, today: date) -> tuple[int, int]: ...  # (done_this_week, N)

# ── core/grace.py (M2) ───────────────────────────────────────────────────────
def evaluate_grace(db, config, registry, user_id, today: date, clock=datetime.now
                   ) -> list[tuple["Habit", int]]:  # [(habit, protected_streak_len)]; writes ledger+audit
    ...
def grace_status_line(db, config, habit, user_id, today, lang) -> str: ...   # /habits balance line
def format_grace_message(broken: list[tuple["Habit", int]], lang) -> str: ...

# ── core/pause.py (M3) ───────────────────────────────────────────────────────
def is_paused(db, config, user_id: str, habit_id: str, when: date) -> bool: ...
async def execute_pause(command, *, db, config, registry, lang, user_id, source="command") -> str: ...
async def execute_resume(command, *, db, config, registry, lang, user_id, source="command") -> str: ...

# ── core/wrapped.py (M4) ─────────────────────────────────────────────────────
def render(db, config, registry, lang, user_id, period: Literal["4w","month"], clock=datetime.now) -> bytes | None: ...
async def execute_wrapped(command, *, db, channel, config, registry, lang, user_id, clock=datetime.now) -> str: ...
def celebration_burst(config, lang) -> str: ...   # "" when celebrate_burst disabled

# ── core/fonts.py (FONT SHARED SURFACE) ──────────────────────────────────────
def register_thai_font() -> None:
    """Idempotent. addfont(bundled NotoSansThai-Regular.ttf); set rcParams
    font.family = ['DejaVu Sans','Noto Sans Thai']. No-op if already registered
    or matplotlib unavailable. Called from charts/heatmap/wrapped import guards."""

# ── core/commands.py — new CommandKind literals + recognizers ────────────────
# CommandKind += "cadence", "pause", "resume", "wrapped"
# _match_cadence(stripped, registry) / _match_pause(stripped, registry)
# _match_resume(stripped, registry) / _match_wrapped(stripped)  → Command(...)
# reserve stems: cadence, ต่อสัปดาห์, กี่ครั้งต่อสัปดาห์, pause, พัก, หยุดพัก,
#                resume, กลับมา, ต่อ, wrapped, recap, สรุปเดือน, การ์ดสรุป
# Reuses Command fields: category (habit), value_num (N / days), pref_value
# (until-token / "off"), limit (unused). No new Command fields required.

# ── config.py — new sections (mounted on Config) ─────────────────────────────
class CadenceConfig(BaseModel):  max_per_week: int = 7        # validator 1..7
class GraceConfig(BaseModel):    enabled: bool = True
class PauseConfig(BaseModel):    max_days: int = 30           # validator > 0
class WrappedConfig(BaseModel):  auto_send: bool = False; celebrate_burst: bool = True
```

## 6. Files to touch

**Shared surface — engine (sequential, first):**
- `storage/migrations.py` — add `_migration_012_lifecycle` (3 tables) + append to `MIGRATIONS`.
- `storage/db.py` — SHARED read accessors (`get_cadence`, `paused_dates`, `grace_protected_dates`, `active_pauses`) that `streaks.py` calls.
- `core/streaks.py` — **the central rework**: `classify_day`, cadence-aware/weekly walk, pause+grace NEUTRAL support, `streak_unit`; `compute_streak` signature unchanged.
- `config.py` — `CadenceConfig` / `GraceConfig` / `PauseConfig` / `WrappedConfig` classes + mount on `Config`.
- `config.toml` — document new `[cadence]`/`[grace]`/`[pause]`/`[wrapped]` sections (defaulted).
- `core/audit.py` — add `cadence_set/cadence_clear/pause_set/pause_clear/grace_consumed` to `Action` Literal **and** `ACTIONS` tuple.
- `core/audit_view.py` — add `_ACTION_LABEL_MSG_IDS` entries for the five new actions.
- `core/i18n.py` — new section-commented key blocks (cadence/grace/pause/wrapped/burst), en+th (skeletons at shared, filled by modules).
- `core/commands.py` — `CommandKind` literals + `reserved_trigger_words()` stems (skeletons at shared).
- `core/release_notes.py` — `RELEASE_NOTES["1.9.0"]` (en+th).

**Shared surface — font (sequential, first; independent of the engine):**
- `assets/fonts/NotoSansThai-Regular.ttf` — bundled font (NEW).
- `assets/fonts/OFL.txt` — the SIL OFL 1.1 license text that MUST accompany the font (NEW).
- `core/fonts.py` — `register_thai_font()` (NEW).
- `core/charts.py`, `core/heatmap.py` — call `register_thai_font()` inside the `MATPLOTLIB_AVAILABLE` block.
- `pyproject.toml` — include `assets/fonts/*` as package data (`[tool.hatch.build.targets.wheel] force-include` / `artifacts`). No new pip dependency (matplotlib already the `[charts]` extra; Noto is bundled data).

**Module M1 — cadence:** `core/cadence.py` (NEW); `storage/db.py` (set/clear_cadence region); `core/commands.py` (`_match_cadence` region); `core/i18n.py` (cadence keys); `core/habitdef.py` (`cadence=<N>w` key + validation); `tests/test_cadence.py` (NEW).

**Module M2 — grace:** `core/grace.py` (NEW); `storage/db.py` (grace region); `core/i18n.py` (grace keys); `tests/test_grace.py` (NEW).

**Module M3 — pause:** `core/pause.py` (NEW); `storage/db.py` (pause region); `core/commands.py` (`_match_pause`/`_match_resume` region); `core/i18n.py` (pause keys); `tests/test_pause.py` (NEW).

**Module M4 — wrapped + burst:** `core/wrapped.py` (NEW); `core/commands.py` (`_match_wrapped` region); `core/i18n.py` (wrapped/burst keys); `config.py` (`WrappedConfig`, if not done at shared); `tests/test_wrapped.py` (NEW).

**Integration (sequential, last) — `main.py`:** new `if command.kind ==` branches (`cadence`/`pause`/`resume`/`wrapped`); wire `is_paused` early-returns into `reminders.send_reminder`, `checkins.run_due_checkins`, `nudge.run_due_nudges`, and the weekly-review + daily-summary inline jobs; register the 00:05 `grace_tick` calling `grace.evaluate_grace` per user + send the kind message; append `grace`/pause/cadence display lines into `review.py` / `dashboard.py` / `discoverability.py` (each module delivers a pure formatter; integration calls it — mirrors v1.8's `/help` wiring); make the milestone + daily-summary + records wording unit-aware via `streaks.streak_unit`; append `wrapped.celebration_burst` to `confirmation_suffix`; register the optional month-end `wrapped_auto` job; update `/help` + `set_my_commands` menu.

## 7. External dependencies

- **matplotlib** — already the optional `[charts]` extra (3.11.1 installed on the host). No version bump; the wrapped card and font registration use only stable APIs (`font_manager.fontManager.addfont`, `rcParams["font.family"]`, per-glyph fallback ≥3.6).
- **Noto Sans Thai** (bundled `.ttf`) — **SIL Open Font License 1.1**. All Noto fonts ship under OFL with **no Reserved Font Name**, so no renaming is required. OFL only requires the license text to accompany the font on redistribution and forbids selling the font by itself; no in-app attribution is required. **Compliance action:** commit `assets/fonts/OFL.txt` alongside the TTF. Justification for the new bundled asset: it is the sole fix for the v1.0 known-issue (matplotlib default lacks Thai glyphs → tofu) and unblocks Thai in both the heatmap and the new card; a bundled OFL font is the standard, license-clean way and adds no runtime pip dependency.
- No other new libraries, APIs, or services. Zero LLM calls added.

## 8. Acceptance criteria

- **AC1** — Given a v1.8.1 DB, When migration 012 runs, Then `habit_cadence`/`grace_ledger`/`pauses` exist, a re-run is a no-op (`user_version` guard), and no existing table/column/row is altered (additive; only 006 was ever a sanctioned break).
- **AC2** — Given seeded logs and **no** cadence/grace/pause rows, When `compute_streak` runs at every call site (review.py:114, records.py:193, dashboard.py:237, main.py milestone), Then it returns byte-identical values to v1.8.1 for the same data.
- **AC3 (HARD BYTE-IDENTICAL GATE)** — Given no `habit_cadence`, no `grace_ledger`, no active `pauses` row for any user and `[grace] enabled` default, When the full suite runs, Then all 3799 v1.8.1 tests stay green and every streak/milestone/daily-summary/weekly-review/records/dashboard/heatmap output is byte-identical to v1.8.1.
- **AC4** — Given a synthetic paused date and a synthetic grace-protected date inside a streak window, When `compute_streak` walks, Then each such day is NEUTRAL (streak neither incremented nor broken), and a MISSED non-neutral day still breaks.
- **AC5** — Given absent `[cadence]`/`[grace]`/`[pause]`/`[wrapped]` sections, When config loads, Then class defaults apply (`grace.enabled=true`, `pause.max_days=30`, `wrapped.auto_send=false`, `wrapped.celebrate_burst=true`, `cadence.max_per_week=7`).
- **AC6** — Given `register_thai_font()` has run, When any existing chart/heatmap renders non-Thai content, Then its byte output is unchanged (DejaVu primary); and a render containing Thai produces glyphs, not tofu.
- **AC7** — Given `/cadence gym 3`, When executed, Then a `habit_cadence` row `per_week=3` is written, the reply confirms "3×/week", and audit `cadence_set` is recorded; `/cadence gym off` deletes it (audit `cadence_clear`); an unknown habit or N∉[1,7] returns the friendly error and writes nothing.
- **AC8** — Given `/addhabit id=gym | type=boolean | en=gym | th=ยิม | cadence=3w`, When executed, Then the habit is created **and** a `habit_cadence` row `per_week=3` is written atomically; a malformed `cadence=` value returns `addhabit_invalid_cadence` and creates neither.
- **AC9** — Given a cadence habit, When its streak is computed/shown, Then it is a weeks-met count and every renderer (milestone, records celebration, records view, dashboard, weekly review, daily summary) uses week wording (via `streak_unit`).
- **AC10** — Given a 3×/week habit with 2 qualifying days this ISO week, When `/habits` and the dashboard render, Then they show "2 of 3 this week"; a non-cadence habit's line is unchanged.
- **AC11** — Given a 3×/week habit logged Mon/Wed/Fri (Tue/Thu/Sat/Sun empty), When the weekly streak is computed, Then the current week is MET and the streak is unbroken (rest days do not break it).
- **AC12** — Given a cadence habit that reaches a new best weeks-met length, When it is logged, Then `records.update_on_log` stores `longest_streak` as that week count and (on a strict exceed of an already-stored record) celebrates with week wording; `best_day`/`best_week` are unaffected.
- **AC13** — Given a daily habit with an active streak and exactly one MISSED yesterday and no grace used this week, When the nightly `evaluate_grace` runs, Then a `grace_ledger` row for yesterday is written, the streak reads as preserved everywhere afterward, and audit `grace_consumed` is recorded.
- **AC14** — Given AC13, When the kind message is produced, Then it is sent exactly once for that protected date, is gentle/non-punitive, and never repeats on subsequent reads/logs.
- **AC15** — Given a daily habit that has already used its grace this ISO week, When a second miss occurs the same week, Then it is not bridged and the streak breaks normally.
- **AC16** — Given a cadence habit, When `evaluate_grace` runs, Then no grace is ever consumed and no `grace_ledger` row is created for it.
- **AC17** — Given `/habits`, When rendered, Then each daily habit shows its grace balance (available / used-on-date); and with `[grace] enabled=false` no bridging, no message, and no ledger writes occur (byte-identical to a graceless world).
- **AC18** — Given a grace consumption, When it is written, Then exactly one audit `grace_consumed` row exists for that `(user, habit, date)` and it renders with a bilingual label in `/audit`.
- **AC19** — Given `/pause water until 2026-09-01` (and separately `/pause 5d`), When executed, Then a `pauses` row is written (habit-scoped / all-habits), the reply confirms, and audit `pause_set` is recorded; `/resume water` deletes it (audit `pause_clear`); resuming when not paused returns `pause_none_active`.
- **AC20** — Given an active pause for a user/habit, When any proactive tick or inline job fires (reminders, check-ins, nudges, weekly review, daily summary), Then no proactive message is sent for the paused habit(s) (early-return before `channel.send`, mirroring quiet-hours).
- **AC21** — Given a pause spanning several days inside a streak, When `compute_streak` runs during and after the pause, Then the paused days are NEUTRAL and the streak continues across the gap (held, not broken).
- **AC22** — Given an active pause, When `/dashboard` and `/habits` render, Then the paused habit shows `⏸ paused until <date>` with the streak marked held; and a voluntary log during the pause still logs and confirms.
- **AC23** — Given a voluntary log during a pause that reaches a milestone, When it is processed, Then the reactive milestone celebration (and record celebration) still fires (pause suppresses proactive sends only).
- **AC24** — Given `/pause water 60d` with `max_days=30`, When executed, Then it is rejected with `pause_too_long` (stating the max) and no row is written; a past/unparseable `until` date returns `pause_invalid_date`.
- **AC25** — Given `/wrapped`, When executed with matplotlib available, Then one composite PNG over the last 28 days is sent via `send_image` with a bilingual caption; `/wrapped month` uses the current calendar month; `/recap` is an accepted alias.
- **AC26** — Given a user with several active habits (including a custom and a cadence habit), When `/wrapped` renders, Then the card is per-user isolated, registry-generic, cadence-aware (weeks-met shown), and reuses records/trends/heatmap computations.
- **AC27** — Given the card contains Thai labels, When rendered, Then the Thai text is glyphs (Noto Sans Thai), not tofu; and with matplotlib unavailable or a render exception, `/wrapped` returns a bilingual text fallback and never raises.
- **AC28** — Given `[wrapped] auto_send=true`, When the month-end job runs, Then one **silent** card is sent per active user, skipped for a fully-paused user and inside DND; with the default `auto_send=false`, no auto-send occurs.
- **AC29** — Given `[wrapped] celebrate_burst=true`, When a milestone or record is celebrated, Then an emoji-burst is appended to the celebration line; with it false, the celebration is unchanged. (No native Telegram sticker is sent — §10.)
- **AC30** — Given the release, When it ships, Then `RELEASE_NOTES["1.9.0"]` (en+th) exists and is announced once per active user, and `/help` + the Telegram menu list `/cadence`, `/pause`, `/resume`, `/wrapped`.

*(Behavior-rule coverage: R1–3,24→AC2,AC3; R4,7→AC11; R5→AC9,AC12; R6→AC16; R8–11→AC13–18; R12–17→AC19–24; R18–20→AC7,AC8,AC10; R21–23,26→AC25–28; R25→AC29; R27→AC1,AC18,AC30. Every rule has ≥1 AC.)*

## 9. Risks & open questions

- **Weekly-walk complexity is the top regression risk.** Cadence changes the streak *unit* (days→weeks), which ripples into records (`longest_streak` semantics), milestone wording, review, dashboard and summary. Mitigation: `streak_unit` is the single switch every renderer consults; the byte-identical gate (AC3) proves daily habits are untouched; cadence rendering is additive. **This is why Theme A is one shared-surface rework, not three independent edits.**
- **`compute_streak` must stay side-effect-free.** Grace *consumption* is a write and is confined to the nightly `evaluate_grace` job; the engine only *reads* the ledger. If a reviewer proposes consuming grace inside `compute_streak`, reject it (it is called read-only from review/records/dashboard/heatmap).
- **Heatmap byte-identical after font registration (load-bearing for AC6).** The heatmap currently draws digits + English month abbreviations. Registration keeps **DejaVu primary** so those glyphs are unchanged; only Thai codepoints fall through to Noto. *If any existing heatmap test does an exact-pixel/byte compare*, verify it still passes after registration; if per-glyph fallback subtly shifts metrics, the fix is to scope registration so DejaVu remains the selected face for all existing (non-Thai) content. **Vera must confirm this explicitly.**
- **OPEN (minor, has a default): wrapped default window.** I default `/wrapped` (no arg) to **last 4 weeks (28 days)** for consistency with heatmap/trends grain and to avoid partial-month awkwardness; `/wrapped month` gives the calendar month. If the user prefers "wrapped = calendar month" as the bare default, that is a one-line change. *Default if no answer: last 4 weeks.* **Who answers: user (product preference).**
- **OPEN (minor, has a default): month-end auto-send default.** I default `[wrapped] auto_send=false` (opt-in) to avoid surprise proactive image sends. If the user wants the card pushed automatically each month, flip the default. *Default: false.* **Who answers: user.**
- **RESOLVED — native stickers.** Genuine Telegram stickers require an account-bound sticker set (external asset, fragile `file_id`s); scoped to an emoji-burst fallback instead (R25/AC29, §10). Flagging honestly rather than pretending a repo-bundled sticker set is clean.
- **RESOLVED — grace vs cadence double-tolerance.** Grace applies to daily habits only (R6/AC16); cadence habits get tolerance from the cadence itself.
- **RESOLVED — milestones during pause.** Reactive milestones on the user's own log fire; proactive sends are suppressed (R16/AC23).

## 10. Out of scope

- **Native Telegram stickers** (`sendSticker` + a bundled/created sticker set) — external account-bound asset; delivered as emoji-burst instead (R25).
- **Manual/purchasable grace** (buy or gift a freeze), multi-day grace, or grace for cadence habits — one auto grace per week per daily habit only.
- **Cadence shapes other than "N times per ISO week"** — no "every other day", no per-month cadence, no rolling-7-day cadence window (ISO-week only, to keep the weekly-walk tractable).
- **Pause of the wrapped/records/history read commands** — pause mutes *proactive* sends only; the user can always query on demand.
- **Retroactive grace** for misses before v1.9.0 ships (grace evaluates from its first nightly run forward, mirroring records' "nothing to compare against yet" seed posture).
- **Animated / multi-page / video recap**, or sharing the card outside Telegram — one static PNG via `send_image`.
- **Auto-detecting cadence** from logging patterns — cadence is explicit (`/cadence` or `cadence=` at creation).
- **LINE/Teams** parity for any new surface (channel remains Telegram-first; new `send_image`/`send` calls degrade via existing concrete-default stubs).

## 11. Module split & parallel development

**Total functionals:** 6 — (1) weekly-cadence goals, (2) grace day, (3) pause/vacation, (4) recap wrapped card, (5) celebration emoji-burst, (6) Thai font bundling.

**Recommendation:** **TWO sequential shared-surface pieces, then 4 PARALLEL modules, then integration.** Above the 5-functional threshold, and the work separates cleanly **once the engine rework and font mechanism exist**. The defining constraint (per the architectural rule): all three Theme-A features edit the one shared streak engine, so the engine's read-side + cadence/pause/grace NEUTRAL logic + migration 012 + the byte-identical gate are built **first, sequentially, as one coherent rework**. After that, each module owns disjoint *write/command/render* surfaces and its own new `core/*.py` file, exactly like the v1.7 `habitdef` and v1.8 `routines` splits. `main.py` and the shared render files (`review.py`/`dashboard.py`/`discoverability.py`) are the integration seam (sequential, last), not parallel work.

**Shared surface (built first, sequentially):**

*Piece A — engine rework (the hard part):* migration 012 (3 tables); `storage/db.py` SHARED read accessors (`get_cadence`, `paused_dates`, `grace_protected_dates`, `active_pauses`); `core/streaks.py` rework (`classify_day`, weekly walk, NEUTRAL support, `streak_unit`; `compute_streak` signature unchanged); `config.py` four config classes; `config.toml` sections; `core/audit.py` + `core/audit_view.py` five new actions; `core/i18n.py` skeleton blocks; `core/commands.py` `CommandKind` literals + reserved stems; `core/release_notes.py` 1.9.0. **Gated by AC1–AC5 (esp. AC3, the byte-identical gate).**

*Piece B — font mechanism (independent of Piece A):* `assets/fonts/NotoSansThai-Regular.ttf` + `OFL.txt`; `core/fonts.py:register_thai_font()`; wire into `charts.py` + `heatmap.py`; `pyproject.toml` package data. **Gated by AC6.**

| Module | Owned ACs | Owned files | Depends on (shared) |
|---|---|---|---|
| `cadence` (M1) | AC7, AC8, AC9, AC10, AC11, AC12 | `core/cadence.py`; `storage/db.py` (set/clear_cadence region); `core/commands.py` (`_match_cadence` region); `core/habitdef.py` (`cadence=` key); `core/i18n.py` (cadence keys); `tests/test_cadence.py` | engine `streak_unit`/weekly walk; `cadence_*` audit vocab |
| `grace` (M2) | AC13, AC14, AC15, AC16, AC17, AC18 | `core/grace.py`; `storage/db.py` (grace region); `core/i18n.py` (grace keys); `tests/test_grace.py` | engine NEUTRAL/`grace_protected_dates`; `grace_consumed` audit vocab; nightly-tick seam |
| `pause` (M3) | AC19, AC20, AC21, AC22, AC23, AC24 | `core/pause.py`; `storage/db.py` (pause region); `core/commands.py` (`_match_pause`/`_match_resume` region); `core/i18n.py` (pause keys); `tests/test_pause.py` | engine NEUTRAL/`paused_dates`; `pause_*` audit vocab; send-gating seam |
| `wrapped` (M4) | AC25, AC26, AC27, AC28, AC29 | `core/wrapped.py`; `core/commands.py` (`_match_wrapped` region); `core/i18n.py` (wrapped/burst keys); `config.py` (`WrappedConfig`); `tests/test_wrapped.py` | font mechanism (Piece B); engine `streak_unit`; records/trends/heatmap read helpers |

ACs verified during the shared-surface / integration pass: **AC1–AC6, AC30**.
Every AC belongs to exactly one owner. **Total: 30 acceptance criteria**
(shared/integration 7, `cadence` 6, `grace` 6, `pause` 6, `wrapped` 5).

**Integration order (after all four modules complete):**
1. `main.py`: add `if command.kind ==` branches for `cadence`/`pause`/`resume` (state-change + dashboard refresh, like the `/target` branch) and `wrapped`/`recap` (read-only image, like `/heatmap`). Wire `pause.is_paused(... today)` early-returns into `reminders.send_reminder` (beside quiet-hours), `checkins.run_due_checkins`, `nudge.run_due_nudges`, and the weekly-review + daily-summary inline jobs. Register the 00:05 `grace_tick` calling `grace.evaluate_grace` per active user and sending the kind message. Make milestone / daily-summary / records wording unit-aware via `streaks.streak_unit`. Append each module's pure formatter (cadence "X of N", grace balance, pause ⏸ marker) into `review.py`/`dashboard.py`/`discoverability.py`. Append `wrapped.celebration_burst` to `confirmation_suffix`. Register the optional month-end `wrapped_auto` job. Update `/help` + `set_my_commands`.
2. Full suite; highest-value gates: **AC3** (byte-identical, 3799 green), **AC6** (font additive), **AC11** (cadence rest days), **AC21** (pause held), **AC13/AC15** (grace once-per-week), **AC23** (reactive milestone during pause), **AC27** (Thai glyphs).
3. Integration tests, two users end-to-end: user A sets `/cadence gym 3`, logs Mon/Wed/Fri, sees an unbroken weekly streak while B is unaffected; A misses one day on a daily habit and gets exactly one kind grace note, a second miss the same week breaks; A runs `/pause water 3d`, receives no water reminders/check-ins/nudges and the streak holds, then `/resume`; A runs `/wrapped` and gets a card with correct Thai; a milestone during A's pause still celebrates.
