# Ideas — v1.6+ "wow" candidates (research, not a spec)

Ideation for the next headline feature. Optimizing for a genuine "wow the first time" moment that fits the
grain of what we've built: **Telegram-native, mostly zero-LLM, gentle (non-nagging), bilingual, per-user
isolated, small family/colleague scale.** v1.6.0 **custom habits** is treated as shipping — where a feature
is *registry-generic*, it automatically covers custom habits for free; I call that out per idea.

**Velocity anchor** (from v1.1–v1.5): one MINOR release = a small shared surface + 2–3 parallel modules +
an additive migration + tests, shipped regularly.
- **S** = one track, no/trivial migration, reuses existing infra.
- **M** = a full release's headline: a module + migration + some new channel/db surface.
- **L** = spans a release-plus, or needs a new data/consent model (e.g. cross-user sharing).

**Bot API surface we already use:** `sendMessage`, `sendPhoto`, inline keyboards + `callback_query`,
`answerCallbackQuery`, `setMyCommands`. **Surface we DON'T use yet** (all real, all available): message
**reactions** (`setMessageReaction`, Bot API 7.0), **pin/unpin** (`pinChatMessage`), **edit-in-place**
(`editMessageText`/`editMessageReplyMarkup`), **native polls** (`sendPoll`), **silent sends**
(`disable_notification`), inbound **photos/voice** (`getFile`). These unlock most of the ideas below.

---

## The candidates

### 1. Live pinned "Today" dashboard  ·  *native / proactivity / delight*
**Pitch:** A single message pinned to the top of the chat that shows today's progress and **edits itself in
place** every time you log — a passive, always-visible scoreboard, not a new notification.
**Why it wows:** The moment you log "500ml" and *scroll up to the pin* and watch `💧 1000→1500 / 2500` tick
up on its own — it stops feeling like a chat and starts feeling like an app you happen to run inside
Telegram. Zero extra pings (it's an edit, not a send), so it's aggressively non-nagging.
**Effort: M (leaning L).** New channel methods (`pinChatMessage`, `editMessageText`), a per-user
`pinned_dashboard_msg_id` store (additive migration), and an "update the pin" hook on every state change
(log / undo / edit / reminder). Honest cost: many call sites touch the pin; must handle the user unpinning or
deleting it (re-create on next update) and Telegram's edit rate limits (debounce to once per change).
**Deps/risks:** edit-heavy; fail-open (a failed edit must never break the log). Zero-LLM.
**Custom habits:** free — the dashboard renders straight from the registry, so new habits appear automatically.

### 2. One-tap quick-log keyboard  ·  *friction-killer*
**Pitch:** `/log` (or a persistent button row) pops an inline keyboard — `[💧250][💧500][🧘10m][📔 diary]` —
so the most common entries are **one tap, no typing**.
**Why it wows:** On a phone, walking around, you tap `💧500` and it's logged with the usual confirmation —
the friction of typing "500ml" disappears. This is the feature people *use ten times a day* and quietly love.
**Effort: S–M.** Reuses `send_actionable` + the `callback_query` plumbing we built for the undo button;
`callback_data = "log:<habit>:<value>"`, handled like an undo tap. Buttons are generated from the registry
(a numeric habit's `unit_aliases`/goal suggest sensible amounts).
**Deps/risks:** callback ownership (already solved for undo — only the tapping chat's log). Zero-LLM.
**Custom habits:** strong synergy — a user who defines "pushups" instantly gets `[💪10][💪20]` buttons.

### 3. Instant emoji reactions on your log  ·  *delight / native (cheap)*
**Pitch:** The bot **reacts** to your log message with a fitting emoji (💧 / 💪 / 🔥 / ✅) the instant it lands
— on top of, or instead of, the text confirmation.
**Why it wows:** Sub-second, wordless acknowledgement. The bot feels *alive and responsive* rather than
transactional. Pairs perfectly with #2 (tap `💧500`, get a 💧 back).
**Effort: S.** One new channel method (`setMessageReaction`). The only real work is capturing the inbound
`message_id` (the loop currently keeps only `chat_id`+`text`) and threading it to the confirmation site.
**Deps/risks:** trivial; fail-open (a missing reaction is invisible). Zero-LLM, zero token cost.
**Custom habits:** free (a small habit→emoji map with a generic ✅ fallback).

### 4. Consistency heatmap  ·  *insight / visual (shareable)*
**Pitch:** A GitHub-style calendar heatmap PNG — last 8–12 weeks, one cell per day, colour = goal-met (or
logged) — via `/heatmap [habit]` and attached to the weekly review.
**Why it wows:** "Whoa." A wall of green squares makes consistency *visible and beautiful* in a way a number
never does — and it's the one artifact people screenshot and share ("look at my streak").
**Effort: M.** matplotlib (already a dependency) renders the grid; reuses `send_image`. A new formatter
module + `/heatmap` command; optional weekly-review attachment.
**Deps/risks:** the Thai-glyph-in-PNG issue we already noted for charts (caption is fine; keep in-image text
minimal). Zero-LLM.
**Custom habits:** free (registry-generic; heatmap for any habit).

### 5. Gentle streak freeze / grace day  ·  *gamification (on-brand)*
**Pitch:** Each period grants one automatic **grace day** that quietly protects a streak from a single
miss — Duolingo's "streak freeze," but forgiving by default and never punitive.
**Why it wows:** The emotional beat. You miss a day, brace for "streak broken 💔," and instead get "no worries
— I used your grace day 🛟, your 20-day streak is safe." A habit bot being *kind* is genuinely unexpected and
is the purest expression of our gentle-gamification philosophy.
**Effort: M.** A per-user grace ledger (additive migration) + a change to the **shared streak engine**
(`core/streaks.py`, used by the weekly review *and* milestones) — so this one carries a real regression
surface; must be byte-identical when no grace is in play, hard AC-gated.
**Deps/risks:** touching the shared streak definition is the risk; otherwise zero-LLM.
**Custom habits:** free (streaks are registry-generic).

### 6. Personal bests & gentle records  ·  *gamification*
**Pitch:** Track lifetime records — longest streak, best single day, most-consistent week — surfaced via
`/records` and celebrated spontaneously when you beat one.
**Why it wows:** "🏆 New personal best — longest stretch streak: 12 days!" arrives unprompted the moment you
earn it. It reframes the app from *nagging you toward a goal* to *celebrating your history*.
**Effort: S–M.** Fully deterministic from `logs` (no new inference); a records computation + a `/records`
view + a celebration hook alongside the existing milestone check.
**Deps/risks:** low; needs care that "records" stay gentle, not competitive. Zero-LLM.
**Custom habits:** free (registry-generic).

### 7. Family / shared gentle goals  ·  *social (the untapped superpower)*
**Pitch:** Opt-in shared goals across approved users — a family "we drank enough water together 5 days
running 🔥" note, or a two-person "streak buddy," with **explicit consent** and only aggregate/✓ visibility
(never each other's raw values).
**Why it wows:** This is the one thing our **multi-user** foundation can do that a solo habit app *cannot*.
The wow is social warmth — your mum sees you both hit your goal and sends a 👏. For a family/colleague base,
this is the highest ceiling on the board.
**Effort: L.** A new shared-goal/group data model, an explicit opt-in consent flow (our per-user **isolation
discipline is strict** — sharing must be deliberate, revocable, and privacy-safe), group membership, and an
aggregated view. This is a whole release, and the design-risk (privacy, consent UX) is the real cost.
**Deps/risks:** privacy is load-bearing; get consent/visibility wrong and it's a trust breach. Can be
zero-LLM.
**Custom habits:** strong — a shared *custom* habit ("family: no phone after 9pm") is a lovely combination,
but compounds the scope.

### 8. Deterministic trends & "you're trending up"  ·  *insight / proactivity*
**Pitch:** Week-over-week deltas and simple trend detection — a `/trends` view and a gentle callout in the
weekly review: "📈 Water's up 12% vs last week — three weeks rising."
**Why it wows:** The bot *notices your momentum* and says something encouraging and true about it. It feels
observant without being an AI black box (the math is transparent and deterministic).
**Effort: M.** Deterministic delta/trend math over existing aggregations + a small line/arrow chart; slots
into the weekly review and a `/trends` command.
**Deps/risks:** small-data honesty — avoid spurious "correlations"; stick to plain deltas + run-length.
Zero-LLM (a nice contrast to the existing LLM narrative, which can stay).
**Custom habits:** free (registry-generic). *Garmin tie-in (niche): if the user is a Garmin user, cross
sleep/steps vs habits here — but keep it optional; only ~1 user likely has Garmin.*

### 9. "Almost there" end-of-day nudge  ·  *proactivity (gentle)*
**Pitch:** If you're within reach of a goal late in your active window (e.g. ≥80%), one encouraging nudge:
"just 500 ml to hit your water goal 💧 — you've got this."
**Why it wows:** A timely, kind push at exactly the moment it helps — the difference between a broken and an
unbroken day. It feels like the bot is quietly in your corner.
**Effort: S.** Builds directly on the v1.5 check-in/DND machinery (per-user window, quiet-hours, goal-met);
just add a "close but not met, near window end, once/day" branch.
**Deps/risks:** nagging is the risk — must be strictly once/day and only when genuinely close; honors DND.
Zero-LLM.
**Custom habits:** free (registry-generic goal check).

### 10. Photo journaling  ·  *friction-killer*
**Pitch:** Send a photo with a caption and it's stored as that day's diary/text entry — a **visual diary**,
shown inline in `/history`.
**Why it wows:** A picture of your meal / your run / your view becomes the log. Journaling gets richer and
lower-friction than typing.
**Effort: M.** Inbound photo handling (`getFile`/store the `file_id`), attach to a text-habit log, render a
thumbnail/📷 marker in `/history` (re-send by `file_id`).
**Deps/risks:** storage of `file_id` vs downloading bytes on a Windows host; **photo *understanding*
(auto-logging from an image) is explicitly out — that needs a vision LLM** and violates minimize-dependence.
Keep it "attach, don't interpret." Zero-LLM as scoped.
**Custom habits:** free for any text habit.

*(Cheap complements worth bundling, not standalone headliners: **silent sends** — `disable_notification` on
check-ins/reminders for extra gentleness, XS; **celebration stickers** on a milestone, S; **native polls**
for one-tap boolean habits, S.)*

---

## Recommendation

### Top 3 (ranked)
1. **Live pinned "Today" dashboard (#1)** — the strongest *app-like* wow and the most on-brand kind of
   proactive (passive, zero extra pings). It changes the whole feel of the product. M effort, honest about
   the edit-everywhere cost; entirely zero-LLM; free for custom habits. **Headline candidate for v1.6/1.7.**
2. **One-tap quick-log keyboard (#2)** — the best wow-per-effort on the board and the feature used most
   often. It *pairs* with #1 (tap → the pin updates live), so shipping them together is a genuine "1+1=3."
   S–M, reuses the undo callback plumbing.
3. **Consistency heatmap (#4)** — the shareable visual wow; leverages matplotlib we already have, and it's
   the artifact that turns a private habit into something people show off. M, zero-LLM.

All three are **zero-LLM, gentle, bilingual-ready, per-user isolated, and registry-generic** (so v1.6 custom
habits come along for free). Add **instant reactions (#3, S)** as a cheap rider on whichever ships first —
it's the smallest possible effort for a real hit of delight.

### Dark horse
**Gentle streak freeze / grace day (#5).** It's not the flashiest line item, but it's the one that could
*over-deliver emotionally* — a habit tracker that forgives you is a story people tell their friends. It's the
truest expression of "gentle gamification," and nobody expects it. The catch (and why it's a dark horse, not
a top pick): it edits the **shared streak engine**, so it carries the highest regression risk relative to its
surface area — worth it only with a hard byte-identical gate. If the goal is *wow via feeling* rather than
*wow via capability*, this is the sleeper.

*(If instead the user wants the biggest possible swing and accepts an L-sized release plus a privacy/consent
design pass, **family shared goals (#7)** is the one feature our multi-user foundation makes uniquely
possible — the highest ceiling here, at the highest cost.)*
