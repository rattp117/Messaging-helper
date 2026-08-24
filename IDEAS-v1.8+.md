# Ideas — v1.8+ "wow" candidates, round 2 (research, not a spec)

Second ideation pass, now that v1.6 shipped the first wow-wave (live dashboard, heatmap, records, trends,
nudge) and **v1.7 per-user custom habits** is landing. These are **NEW** candidates only — nothing already
shipped (v1.0–v1.7) and nothing already **on the shelf** (quick-log keyboard, emoji reactions, streak
freeze/grace day, family shared goals, photo journaling; riders: silent sends, celebration stickers, native
polls; housekeeping: `/edithabit`, owner-scoped menus, `/audit` language fix, Thai font for chart PNGs) or
**deferred** (LINE, Teams, Garmin CSV, voice transcription).

Same grain as always: **Telegram-native, mostly zero-LLM** (remote Ollama only, minimize calls),
**gentle/non-nagging, bilingual TH/EN, strict per-user isolation, small family/colleague scale, local-first,
single long-running Windows process, minimal deps.**

**v1.7 lens.** The registry is now **per-user** (base catalog + a user's own `user_habits`). Every idea below
is tagged for how it meets custom habits: **"free"** = registry-generic, so a user's custom habit inherits it
automatically the moment it exists; **"synergy"** = the idea gets materially *better* because custom habits
exist; **"touches the registry rewiring"** = it must respect the per-user `RegistryProvider` (built in v1.7).

**Effort scale** (unchanged): **S** = one track, no/trivial migration, reuses infra. **M** = a release's
headline: a module + migration + some new channel/db surface. **L** = spans a release-plus or needs a new
data/consent model.

**Grounding notes from the code** (so effort is honest):
- Channel surface in use: `send`, `send_image` (sendPhoto), `send_actionable` + `callback_query`,
  `answer_callback_query`, `set_my_commands`, `send_and_pin`/`edit_message`/`unpin`. **No `sendDocument`
  yet** — export/backup-to-chat needs one new channel method (same concrete-default degradation pattern as
  `send_image`, so LINE stub + test fakes are unaffected).
- Extraction returns **exactly one** habit per message (`llm/prompts.py`: "a single JSON object"). So
  "multi-habit in one message" is a genuine change, not a free lunch.
- Scheduler fan-outs already loop `db.active_user_ids()` per user: `run_due_reminders`, `run_due_checkins`,
  `run_due_nudges`, weekly-review/daily-summary, 00:00 dashboard rollover. Any new proactive tick slots into
  the same machinery (and, post-v1.7, resolves each user's registry via the provider).
- Audit log (`core/audit.py`) already records actor/action/entity/old→new/source; new mutations should add a
  vocab entry + fail-open capture, same as every prior release.

---

## The candidates

### 1. Backfill / retroactive logging  ·  *friction-killer*
**Pitch:** Log for a past day — "500ml **yesterday**", "stretched 20 min **on Monday**", "diary **2 days
ago**: …". Today a log always lands on today; there's no way to fix a day you forgot.
**Why it wows:** It removes the one quietly infuriating limitation of every habit tracker — you remember at
breakfast that you hit your goal *last night*, and now you can actually record it instead of losing the day
(and the streak). It makes the log *trustworthy as history*, which everything else (records, trends, heatmap,
streaks) then reflects correctly.
**Effort: S–M.** A deterministic relative-date prefix/suffix parser (yesterday / วันนี้-วานนี้ / "N days ago" /
weekday name, bounded to a small window, e.g. ≤14 days back) that resolves a target date and threads it into
the existing insert; the LLM path can also return an optional `date` offset. The real work is auditing that
streak/records/heatmap/trends recompute correctly when a *back-dated* row appears (they read by date already,
so mostly free) and blocking future dates.
**Deps/risks:** must not let backfill silently rewrite a *goal-met* day's celebration retroactively (decide:
celebrate on backfill or stay quiet — recommend quiet). Bounded look-back prevents abuse. Zero-LLM for the
common relative-date words.
**Custom habits:** free (registry-generic; the date logic is orthogonal to which habit).

### 2. Routines / habit stacks  ·  *friction-killer / delight*
**Pitch:** A user bundles several habits into a named routine — "**morning** = water 500 + stretch 10 +
meditate 10" — and logs the whole stack in one shot: `/routine morning`, or one tap on a routine button.
**Why it wows:** One tap logs your entire morning ritual and you watch the pinned dashboard tick up three
bars at once. It turns the bot from a per-item logger into something that understands *how you actually live*
— habits come in clusters, and this is the first feature that models that.
**Effort: M.** A per-user `routines` store (additive migration: name → ordered list of {habit, value}),
`/routine` create/list/run commands, and a runner that replays each item through the existing log path
(reusing confirmation + dashboard update + audit). Deterministic; no LLM.
**Deps/risks:** value-less items (text/boolean habits) and partial failure (one habit archived) must degrade
gracefully — log what's valid, note what was skipped. Keep it deterministic to stay outage-proof.
**Custom habits:** **synergy** — a routine of a user's *own* customs ("evening = journaling + pushups 20 +
no-phone ✓") is exactly the payoff of v1.7; and a routine is the natural **one-tap button** for the tentative
v1.8 quick-log keyboard (a routine *is* a stack behind a single button).

### 3. Multi-habit in one message  ·  *friction-killer*
**Pitch:** "drank 500ml and stretched 15 min" → **two** logs from one message, each confirmed.
**Why it wows:** People narrate their day in one breath; today the bot only catches the first habit. Catching
all of them makes it feel like it's actually *listening*, not pattern-matching one token.
**Effort: M.** Two honest paths: (a) a zero-LLM **preparse splitter** that finds multiple `NUMBER+UNIT`
clauses over the per-user unit lookup (covers most numeric/duration cases, stays outage-proof), and (b) widen
the extraction schema from one object to a small **list** for the LLM fallback. Both must fan the existing
single-log confirmation + dashboard + audit per extracted item, and reactions (if shipped) fire per item.
**Deps/risks:** ambiguity ("500" belongs to which habit?) — keep the splitter conservative and fall through
to the LLM; cap the number of items to bound cost. A schema change to the extraction contract touches the
prompt/parser, so it's more than a rider.
**Custom habits:** free, but **touches the registry rewiring** — the splitter runs over the *acting user's*
per-user unit lookup (v1.7's `RegistryProvider`), so a user's custom-unit clause splits for them and not for
anyone else.

### 4. Weekly-cadence goals ("3× per week")  ·  *structure / insight*
**Pitch:** Some habits aren't daily. Let a goal be a **frequency per week** — "meditate 3×/week", "gym 4×/
week" — with a weekly progress ring ("2 of 3 this week ✅") and a **weeks-met** streak instead of a daily one.
**Why it wows:** It unlocks a whole class of real habits that a daily streak actively *punishes* (a 4×/week
runner "breaks" their streak every rest day today). Modelling cadence honestly is the difference between the
bot fitting your life and your life fitting the bot — and it's uniquely gentle (rest days stop being failures).
**Effort: M–L.** A per-habit `cadence` target type (additive to the targets model), a weekly-window
aggregation, and — the real cost — a **second flavor in the shared streak engine** (`core/streaks.py`, used by
review *and* milestones), so it carries the same regression surface as the shelf's grace-day idea: daily
habits must stay byte-identical, hard AC-gated.
**Deps/risks:** touching the shared streak definition is the risk; the weekly window + timezone edge cases
need care. Zero-LLM (cadence is set via `/target`-style command; NL parse optional).
**Custom habits:** **synergy** — a user defining a custom habit could pick cadence at creation
(`/addhabit … | cadence=3w`), which is the most-requested shape for gym/hobby habits. Touches the v1.7
`HabitConfig`/validation surface.

### 5. Cross-habit correlations  ·  *insight*
**Pitch:** A gentle, true observation about how your habits move *together*: "📎 On days you stretch, you hit
your water goal **4× more often**." Surfaced in the weekly review and via `/insights`.
**Why it wows:** The bot notices something about *you* that you didn't — and it's deterministic and
explainable, not an AI hunch. It's the "huh, I didn't realize that" moment that makes an app feel insightful
rather than just a ledger.
**Effort: M.** Deterministic co-occurrence math over the per-user `logs` (goal-met-day overlap, run-length,
simple conditional rates) — no inference, no new storage. A small `/insights` view + an optional review block.
The discipline is **statistical honesty**: only surface a pairing with enough days behind it, phrased as an
observation ("more often") never a causal claim.
**Deps/risks:** small-data false patterns — gate on a minimum sample and suppress weak signals (mirrors the
v1.6 "small-data honesty" rule for trends). Zero-LLM.
**Custom habits:** **synergy** — correlations get richer the more habits a user tracks, so heavy custom-habit
users get the most out of it; registry-generic over whatever pairs exist.

### 6. Adaptive goal suggestions  ·  *insight / proactivity*
**Pitch:** When you've cleared (or consistently missed) a goal for weeks, the bot *offers* — never forces — a
tweak: "You've beaten 2500 ml for 3 weeks 💪 — bump to **2800**? [Yes] [Keep 2500]". One tap applies it.
**Why it wows:** The bot grows with you. A static goal eventually feels stale or discouraging; a bot that
notices you've outgrown it (or that it's set too high and quietly demoralizing) feels like a coach paying
attention — and it hands you the decision, which keeps it gentle.
**Effort: S–M.** Deterministic over existing aggregations (sustained over/under-performance detection); the
"apply" is a `callback_data` tap that reuses the existing `/target` override write + audit. A once-per-period,
opt-in-by-nature suggestion (only when the signal is strong), honoring quiet hours.
**Deps/risks:** must be rare and never nagging — a suggestion, dismissable, at most once per habit per few
weeks. Downward suggestions need especially kind framing (never "you're failing"). Zero-LLM.
**Custom habits:** free (registry-generic goal logic; applies to custom numeric/duration habits identically).

### 7. Gentle comeback / dropout rescue  ·  *proactivity (gentle)*
**Pitch:** If a habit that used to be active goes **quiet for N days**, one warm, no-pressure note — "no
rush — want to pick 🧘 meditation back up? even 5 minutes counts" — then it backs off (won't repeat).
**Why it wows:** This is the moment every other tracker gets *wrong* — they either nag or go silent and let
you drift away. A single kind, well-timed "still here for you" at the exact point of quiet fade is the most
on-brand proactivity we could ship: it's the bot being in your corner when you're slipping, without guilt.
**Effort: S.** Rides the existing per-user nudge/check-in tick (`run_due_nudges` machinery): detect
"previously-logged habit, now silent ≥N days, not archived", fire once, mark it so it won't re-fire until the
habit is active again. Honors DND/quiet hours; opt-in via the existing check-in enablement.
**Deps/risks:** the fine line between "gentle rescue" and "nag" — strictly once per lapse, only for
established habits (not brand-new ones), never for archived ones. Zero-LLM.
**Custom habits:** free (registry-generic), and **touches the registry rewiring** (must read the per-user
active registry to know what "went quiet" means for that user).

### 8. Planned pause / vacation mode  ·  *resilience / gentle*
**Pitch:** `/pause 5d` (or `/pause until Monday`, `/pause water`) suspends reminders, check-ins and nudges for
a **planned** absence and shields streaks from the gap, auto-resuming after. Distinct from a single grace day
— this is a deliberate, multi-day "I'm travelling / sick / off this week."
**Why it wows:** You tell the bot you'll be away and it just… respects that — no reminders buzzing on the
beach, no "streak broken 💔" waiting when you get back. A habit tool that gracefully handles *real life
interrupting the habit* is rare and deeply trust-building.
**Effort: S–M.** A per-user (optionally per-habit) `pause_until` store (additive migration); every proactive
fan-out checks "is this user/habit paused?" before sending; the streak engine treats paused days as neutral
(not misses). Deterministic; a small `/pause` / `/resume` command pair + audit.
**Deps/risks:** the streak-neutrality rule touches the shared streak engine (regression-gate it), and "paused
days are neither met nor missed" must be consistent across review/records/heatmap. Zero-LLM.
**Custom habits:** free (registry-generic pause check); per-habit pause naturally covers customs.

### 9. Self-serve data export  ·  *ops / trust (local-first)*
**Pitch:** `/export [habit] [range]` returns **your own** logs as a CSV (or JSON) file — a real Telegram
document you can keep, open in Excel, or archive. Your data, in your hands.
**Why it wows:** For a local-first product whose whole promise is "your data never leaves your machines,"
letting a user *walk away with their history in one command* is the ultimate expression of that value — and
it's the thing a spreadsheet-minded family/colleague base will genuinely love.
**Effort: S.** One new channel method (`send_document`/sendPhoto-sibling, concrete-default degradation so LINE
stub + fakes are unaffected), a per-user CSV builder over `logs` (respecting isolation — only the caller's
rows, including archived habits' history), and an `/export` command + audit entry.
**Deps/risks:** strict per-user scoping is load-bearing (never leak another user's rows); CSV must handle the
Thai/quoted `raw_message` safely (reuse the `/history` sanitization). Zero-LLM.
**Custom habits:** free — export is registry-generic and should include a user's custom-habit history (and
archived customs), which is exactly where a personal export is most valuable.

### 10. Isolation-safe cheer relay  ·  *social (the safe half of "family")*
**Pitch:** Send a wordless cheer to another **approved** user — `/cheer mum 👏` — and they get "👏 someone's
rooting for you!" **without either of you seeing the other's data.** A drop of social warmth that fully
respects strict isolation, unlike the full family-goals idea on the shelf.
**Why it wows:** It's the emotional 20% of shared goals at 5% of the risk — the warmth of "my daughter noticed
I'm doing well" with **zero** data crossing the isolation boundary. For a family base, a bot that lets people
encourage each other but never surveil each other is a lovely, trust-preserving surprise.
**Effort: S–M.** A consent-gated directory of who-can-cheer-whom (opt-in, revocable — reuses the owner
allowlist as the trust root), a `/cheer` command, and a relayed send. **No aggregate data, no values, no
✓/✗** — just the cheer itself, so it sidesteps the entire privacy-design cost that makes shelf-#7 an L.
**Deps/risks:** consent + revocation must be explicit (mirrors the isolation discipline); rate-limit to
prevent cheer-spam. Optionally let a cheer name a habit ("👏 on your reading!") — generic over the registry,
still no data shared. Zero-LLM.
**Custom habits:** free — a cheer can *name* a habit label (including a custom one) without exposing any value.

### 11. Recap "wrapped" card  ·  *insight / delight (shareable)*
**Pitch:** A single beautiful PNG that sums up a period — this month / this year — in one shareable card:
total logged, best day, longest streak, biggest trend, a mini heatmap strip. `/recap [month|year]`, plus an
auto-drop at month-end.
**Why it wows:** The "Spotify Wrapped" beat — one image that makes a month of quiet effort feel like an
*achievement worth showing off*. It's the artifact people screenshot and send to the family chat, and it
compounds everything we already compute (records + trends + heatmap) into one delightful moment.
**Effort: M.** A composite matplotlib renderer that assembles already-computed pieces (records/trends/heatmap
data) into one card; a `/recap` command + optional month-end auto-send. Reuses `send_image`.
**Deps/risks:** the known **Thai-glyph-in-PNG** issue (shelf housekeeping) bites harder here since a recap card
*wants* words — either bundle a Thai font first (pairs naturally with that shelf item) or keep in-image text
to numbers/labels with a bilingual caption. Zero-LLM.
**Custom habits:** free (registry-generic; a heavy custom-habit user's recap is the richest).

*(Cheap complements worth bundling, not standalone headliners: **off-box backup to owner's chat** — the
scheduled DB backup delivered to the owner as a Telegram document for an automatic off-machine copy, XS once
`send_document` exists (shares the surface with #9 Export); **streak-at-risk quiet ping** — a single silent
`disable_notification` reminder late in the day only when an active streak is about to lapse, S, rides the
nudge tick; **`/routine` as the default quick-log button** — see #2, the natural pairing with the v1.8
keyboard.)*

---

## Recommendation

### Top 3 (ranked)
1. **Routines / habit stacks (#2)** — the strongest wow-per-effort *and* the one that **multiplies the
   tentative v1.8 headline**: a routine is exactly what belongs behind a one-tap quick-log button, and tapping
   it lights up the pinned dashboard three bars at once. It's the first feature that models habits as the
   clusters people actually live in, and it's pure payoff for v1.7 custom habits (stack your own). M,
   deterministic, zero-LLM.
2. **Backfill / retroactive logging (#1)** — kills the single most real, most-felt friction on the board
   (you forgot to log last night and lose the day). It makes the entire history layer — records, trends,
   heatmap, streaks — *trustworthy*, which quietly raises the value of everything already shipped. S–M,
   registry-generic, mostly zero-LLM.
3. **Recap "wrapped" card (#11)** — the shareable delight artifact; it compounds records + trends + heatmap
   into one screenshot-and-send moment and gives the family/colleague base something to show off. M, zero-LLM
   (nudges us to finally fix the Thai-font-in-PNG housekeeping item, which pays off everywhere).

All three are **gentle, bilingual-ready, per-user isolated, and registry-generic/synergistic** (so v1.7 custom
habits come along for free), and all three are **zero-LLM**.

### Dark horse
**Weekly-cadence goals (#4).** Not flashy, but structurally it's the biggest unlock here: it lets the bot
honestly track the huge class of habits that are *N-times-a-week, not daily* — gym, hobbies, deep-cleaning —
which today's daily-streak model actively punishes with a "broken streak" on every rest day. Shipping cadence
turns rest days from failures into part of the plan, the purest expression of gentle tracking. The catch (and
why it's the dark horse, not a top pick): like the shelf's grace-day idea, it edits the **shared streak
engine**, so it carries the highest regression risk relative to its surface area — worth it only behind a hard
byte-identical gate for daily habits. If the goal is "wow via *capability the product couldn't model before*,"
this is the sleeper.

### What bundles with the tentative v1.8 (quick-log keyboard + reactions)
- **Best bundle: Routines (#2)** — a routine is the highest-value *content* for the quick-log keyboard (one
  button = a whole stack), and every logged item in the stack can fire an emoji reaction. Ship
  keyboard + reactions + routines together and you get a genuine "1 + 1 + 1 = 5."
- **Multi-habit message (#3)** pairs directly with **reactions** — one narrated sentence, several logs, a
  reaction per habit — and with the keyboard it means typing *and* tapping both scale to multiple habits.
- **Backfill (#1)** rides the keyboard cheaply too (a "yesterday" modifier button / long-press), and is
  independent enough to ship alongside without coupling risk.
- Cheapest riders to tuck into whichever ships first: **adaptive goal suggestions (#6)** and the
  **streak-at-risk quiet ping** — both reuse existing callback/nudge plumbing for a small hit of "this bot is
  paying attention."
