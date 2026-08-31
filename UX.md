# UX Design — LINE edition: Admin Web Portal

> Consumes `SPEC-LINE-PORTAL.md` (32 ACs, MUST/SHOULD/COULD tiers, tailnet-only security model, zero-new-dependency server-rendered HTML). Produces the structure Iris hangs the visual layer on. Target baseline `1.2.0+line`.
>
> **Wireframe convention.** All wireframes are labeled in **English** so the ASCII boxes stay aligned in a terminal — Thai glyphs break monospace alignment. **Thai is the shipped primary language.** Every string in the wireframes maps to a row in §7 Microcopy, which carries the real TH + EN copy.

---

## 1. Users & jobs-to-be-done

There is exactly one user. Designing for a second one would be waste.

- **The bot owner** — primary job: *answer "is my bot healthy, and does anything need me?" in under ten seconds, and clear the one thing that does.* Frequency: **daily glance** (morning, phone), **event-driven** (a join request push arrives), **rare deep-dive** (a quota anomaly, once a month or less). Skill level: **power user** — runs a VPS, reads Python tracebacks, knows what `push_cap` means. Constraints: **phone-first** (the join-request notification lands in LINE on their phone, so the portal is opened on the phone in the same minute), desktop for the deep-dives; both devices are already on the tailnet; no login step exists, so the portal is one tap from a home-screen bookmark.

**What this single-user reality changes:**

| Normal admin-tool assumption | Here |
|---|---|
| Onboarding, tours, empty-state tutorials | None. The owner built this. Empty states are *status reports*, not lessons. |
| Role/permission UI, user pickers | None. The only actor is "you". |
| Confirmation everywhere to protect coworkers | Confirmation calibrated to **blast radius**, not to politeness (§5). |
| Dense information hiding behind filters | Show everything; the datasets are tiny (a handful of users, ~50 audit rows a page). |
| Session/auth chrome | None. No login, no logout, no avatar menu. The network is the auth. |

---

## 2. Information architecture

Five flat pages plus one footer-level page. Flat because there are only six destinations and any nesting would cost a tap for no gain. No hamburger — a hamburger needs JS or a `<details>` hack, and six items fit in a wrapping row.

```
Portal root (tailnet-only, https://<magicdns-name>:8081 via `tailscale serve`)
│
├── /            Status            ← DEFAULT / bookmark target. "Is everything healthy?"
│                                    verdict · needs-you · tiles · quota gauge ·
│                                    scheduler · storage · recent errors
├── /users       Users             ← "Who's waiting? Who's active?"
│                                    pending (approve/block) · active (stats, block) · invite
│   └── POST /users/approve | /users/block | /users/invite   → 303 back to /users
│       └── /users/invite (unconfirmed POST) renders a full-page confirm interstitial
│
├── /quota       Quota & digest    ← "Why did my quota jump?" / "Send digest now"
│                                    month history · current-month per-user · caps &
│                                    thresholds · digest roster · [Send digest now]
│   └── POST /quota/digest-run (unconfirmed) → full-page confirm interstitial
│       POST /quota/digest-run (confirm=yes) → 303 → /quota?ran=… result banner
│
├── /audit       Audit             ← "Who changed what?" (admin actions, paginated)
├── /activity    Activity          ← "What have users been logging?" (metadata only)
│
└── /config      Config (footer)   ← read-only effective config, secrets redacted [C]
```

**Nav order is frequency order**, not spec order: Status → Users → Quota → Audit → Activity. `/config` is a *could*-tier reference page visited maybe twice a year; it lives in the footer, not the nav, so it doesn't cost a slot in the phone nav row.

**Nav carries one live signal: the pending-approvals count.** `Users (2)` when two people are waiting, plain `Users` when none. This is the single highest-value glanceability feature in the portal — it means *every* page answers "does anyone need me?" without navigating. See §8 Q1 for the shared-surface implication.

**Audit vs Activity are deliberately separate destinations, not tabs.** They answer different questions from different tables (`audit_log` = *who changed what*; `logs` = *what users recorded*) and are never compared side by side. Tabs would imply a relationship that isn't there.

---

## 3. User flows

### Flow A — Morning glance: "is everything healthy?"

The dominant flow. Runs daily, on a phone, in under ten seconds. Success = the owner closes the tab without tapping anything.

1. Owner taps the home-screen bookmark → `GET /` (no login, no interstitial; the tailnet and the identity header resolve before a pixel renders).
2. System renders the status page. **The first screenful, above the fold on a 375pt phone, is the entire answer:**
   - **Health verdict** — one line, one of three states (§4 Screen 1).
   - **Needs-you line** — rendered *only if* something needs action (pending approvals ≥ 1). Absent when there is nothing to do; an explicit "0 waiting" is noise.
   - **Quota gauge** — used / cap / percent / mode, in its normal, warn, or stopped state.
3. Owner reads the verdict. **Happy path ends here** — verdict is "All good", no needs-you line, gauge is normal. Owner closes the tab. Zero taps.
4. If the verdict is not "All good", the verdict line names *which* panel is unhappy and links to it (an in-page anchor for errors/scheduler, a cross-page link for quota).
5. Owner scrolls to the named panel, reads the detail, decides whether to act.
6. End state: either the tab is closed, or the owner is on `/users` (Flow B) or `/quota` (Flow C).

**What deliberately does NOT drive the verdict:**
- **"Last webhook event" staleness.** LINE webhooks arrive only when a human messages the bot. At 06:00 the last event is legitimately 9 hours old. Making this red would train the owner to ignore the verdict — the single worst outcome for a glance-first dashboard. Last-event is **informational, never an alarm**.
- **Zero pending approvals.** That is the normal, good state.

**What does drive it** (in escalating order): a `WARNING+` record in the ring buffer → *Needs a look*; a panel that failed to read → *Needs a look*; quota ≥ 80% → *Needs a look*; a scheduler job with a `next_run_time` of `None` (a dead job — the bot has silently stopped doing something) → *Needs attention*; quota ≥ 100% → *Needs attention*.

**Error branches:**
- If one panel's data read raises → that panel renders its "unavailable" placeholder, the rest of the page renders normally, and the verdict degrades to *Needs a look* naming that panel. Per spec §3.3; one broken panel never blanks the page.
- If the identity header is missing/wrong → `403` before any of this (§4 Screen 8). The owner will only ever see this if `tailscale serve` is misconfigured, so the 403 body says nothing that would help an attacker and nothing that would help the owner debug — deliberately (§4 Screen 8).

---

### Flow B — "Someone new asked to join" → approve

Event-driven, phone, time-pressured (a real person is waiting). This flow must be **short**, because the alternative — typing `/approve U4af4980…` into LINE from memory — is the thing the portal exists to replace.

**The notification path that starts it (today, unchanged):**

1. A stranger messages the bot. `core/access.py` creates a `pending` row and pushes `access_request` to the owner's LINE: *"🔔 {name} (แชท {chat_id}) ขอสิทธิ์เข้าใช้งาน อนุมัติด้วย: /approve {chat_id}"*.
2. Owner sees the LINE notification on their phone. **This push does not currently link to the portal** — it offers only the chat command. The owner's route to the portal is their own home-screen bookmark. See §8 Q2: adding the portal URL to this push is a small, high-value change that needs one config key and one i18n key, and it is the difference between "two taps" and "remember the bookmark exists".
3. Owner opens the portal → any page shows `Users (1)` in the nav.

**The approve itself:**

4. Owner taps `Users (1)` → `GET /users`. The **Pending section is first on the page**, above Active, above Invite. Ordering is by urgency, not by table.
5. The pending row shows: display name (large), raw chat_id (small, monospace, selectable), and how long they have been waiting. Two controls: **Approve** (primary) and **Block** (secondary).
6. Owner taps **Approve**. The control is a `<details>` disclosure — it expands **in place**, no page load, no JS, revealing one sentence of consequence and the real submit button:
   > *Approve {name} to use the bot? They'll get a message right away.*
   > `[ Confirm approve ]`  `[ Cancel ]`
7. Owner taps **Confirm approve** → `POST /users/approve` with `chat_id`.
8. System calls `access.approve_user(..., source="portal")`: row → `active`, audit row written, `access_granted` push sent to the new user.
9. `303 See Other` → `/users?ok=approve&chat=<id>#flash`.
10. The re-rendered `/users` shows a success banner at the top (focused via the `#flash` fragment, `role="status"`, `tabindex="-1"`), the pending section is now one row shorter or empty, and the new user has appeared in Active. The nav badge has decremented.
11. End state: owner is on `/users` looking at a correct page. Total taps from notification: **bookmark → Users → Approve → Confirm = 4**, no typing, no id copying.

**Error branches:**
- If the identity header is missing/invalid on the POST → `403`, **no DB write** (AC20). The owner cannot hit this from a normal browser session; a mis-Funneled port can.
- If `chat_id` is missing or unresolvable (a stale form — the row was already actioned from LINE chat in the meantime) → `303` back to `/users?err=chat_unknown`, inline error banner, **no write, no audit row** (AC21). The page the owner lands on already shows the real current state, so the recovery is "read the page you're on".
- If the `access_granted` push fails (LINE API down, quota stopped) → the approve **still succeeded** (DB + audit are the source of truth). The flash must say so honestly rather than claiming a message was delivered. See §7 `portal_flash_approve_nopush`.

---

### Flow C — "Why did my quota jump?"

Rare, desktop, investigative. The owner has seen the gauge in warn or stopped state and wants a cause.

1. Owner is on `/` and the quota gauge reads warn or stopped (or the `push_quota_warn` LINE push arrived).
2. Owner taps the gauge → `GET /quota`.
3. System renders three blocks **stacked in diagnostic order**, so the answer is found top-down:
   - **Month history** (last 12 months, one row each, with a bar). *Is this month anomalous, or is this just growth?* The jump is visible as a shape, in one glance.
   - **Current month, by user, sorted descending by push count.** *Who is consuming it?* The culprit is row 1 by construction — no sorting UI needed, and sorting UI would need JS.
   - **Caps & thresholds** — active cap, the 80% warn line, the 100% stop line, and whether warn/stop have already fired this month.
4. Owner reads down. Three outcomes:
   - One user dominates → the answer is "that user is very active"; owner can cross-check *what* they logged at `/activity` (linked from the block).
   - Every user is up uniformly and the **mode** reads `realtime` → the answer is the mode. **A `digest` → `realtime` flip multiplies push volume by roughly the number of daily interactions.** The page states the active mode prominently next to the history *for exactly this reason*.
   - Neither → the owner checks `/audit` for a settings change and `/`'s errors panel for a retry storm.
5. End state: owner knows the cause, and either accepts it, flips `digest.mode` back in `config.toml` (outside the portal — config editing is out of scope), or waits for the month to roll.

**Known limit, stated on the page rather than hidden:** `config.toml` changes are **not** in the audit log, so if the cause was a mode or cap change the portal can show *that the mode is now realtime* but not *when it changed or by whom*. The month-history block therefore carries a one-line note pointing at the current mode as the first thing to check. See §8 Q4.

**Error branches:**
- If `monthly_push_history()` fails → that block renders "unavailable"; the per-user block and thresholds still render. The owner is not left with a blank page during the one investigation they actually needed it for.
- If the current month has zero pushes → an empty-state row reading "no pushes recorded this month yet", not an empty table.

---

### Flow D — "Send today's digest now" (confirm-gated)

The only irreversible, fan-out, quota-spending action in the portal. Ceremony is highest here and is *earned*: one careless tap messages every user and burns real quota.

1. Owner is on `/quota`, in the **Digest** block, which lists each active user's opt-out state and the scheduled send time/mode.
2. Owner taps **Send digest now**. This is a form submit (`POST /quota/digest-run`) **without** `confirm=yes`.
3. System does **not** send. It renders a **full-page confirm interstitial** (`200`, not a redirect) — a whole page, not an inline disclosure, because this action deserves a page of its own attention. The interstitial states the blast radius using data the `/quota` page already loaded:
   - **who**: *"Will send to N users who have digest on."*
   - **cost**: *"Uses about N pushes. This month: {used}/{cap}."*
   - **irreversibility**: *"Can't be undone."*
   - **duplication**: *"If today's scheduled digest already went out, people will get it twice."*
   - **duration**: *"This can take a while. Don't close or refresh this page."*
4. Owner taps **Yes, send now** → `POST /quota/digest-run` with `confirm=yes`, carrying a **one-time token** minted on the interstitial.
5. System validates the token, invokes `digest.run_daily_digest(...)`, then `303` → `/quota?ran=<sent>.<skipped>#flash`.
6. Result banner: *"Sent to {sent}. Skipped {skipped}."*
7. End state: owner is back on `/quota` with a refreshed quota number reflecting what they just spent.

**The zero-JS problem this flow creates, and its fix.** With no client JS there is no spinner, no disabled-on-submit, and no way to stop a browser refresh. A fan-out to every user can run for tens of seconds against a blank white tab. Two consequences, both handled in structure, not decoration:
- **Perceived hang** → the interstitial *pre-announces* the wait in step 3 ("this can take a while, don't refresh"). This is the only honest mitigation available and it must not be dropped as "just copy" — it is the entire loading state for this action.
- **Double-send** → **requirement: a second submission of the same confirm MUST NOT send a second time.** The one-time token in step 4 is the recommended mechanism (mint on interstitial render, invalidate on use); an in-memory "last run at" throttle is an acceptable simpler substitute. Either way, replaying the POST renders *"Already sent at {time} — nothing sent again."* Mechanism is Sophia/Irine's call (§8 Q3); the behavior is not optional.

**Error branches:**
- If quota is already in the **stopped** state → the **Send digest now** control is not rendered at all; in its place, a line explaining that proactive pushes are paused until next month. Rendering a button whose only outcome is failure is worse than rendering no button.
- If the token is missing/spent → the "already sent" page above, `no` send.
- If `run_daily_digest` raises partway → the result banner reports what actually went out (`sent`/`skipped` from the run), and the error lands in the ring buffer, visible on `/`. Never claim a clean run after a partial one.

---

### Flow E — Invite someone who hasn't messaged the bot yet

Low frequency, but the only flow where the owner **types** an opaque identifier — so it is the only flow with a real typo risk, and it gets a full interstitial rather than an inline disclosure.

1. Owner is on `/users`, at the **Invite** block (last on the page — it is the least urgent thing there).
2. Owner pastes a LINE user id into a single text field and submits.
3. System validates the shape against `access._CHAT_ID_RE`.
   - **Invalid shape** → `303` → `/users?err=chat_invalid&val=<echoed>#flash`. The field is **re-populated with what they typed** (escaped, truncated to 64 chars for layout safety) so the fix is a character edit, not a re-paste. No write, no audit row (AC21).
   - **Valid shape** → a full-page confirm interstitial echoing the id back in large monospace: *"Add {chat_id} as an approved user?"* plus the warning that a wrong id creates a user row for someone who does not exist. `[ Yes, add ]` `[ Cancel ]`.
4. Owner confirms → `POST /users/invite` with `confirm=yes` → `approve_user(..., source="portal")` → `303` → `/users?ok=invite&chat=<id>#flash`.
5. End state: the id appears in the Active list; the flash explains that they'll have access the moment they first message the bot.

**Why an interstitial here but a disclosure on Approve:** Approve acts on a row the *system* produced and displays by name — the owner is confirming a person they can see. Invite acts on a string the *owner* produced — the failure mode is a silent typo creating a phantom user, and the only defense is showing the typed characters back at a size where a transposition is visible.

---

## 4. Screen inventory & wireframes

Every screen below is a full server-rendered page load. **There is no loading state anywhere in this portal** — no skeletons, no spinners, no optimistic UI — because there is no client JS and no async fetch. The browser's own progress indicator is the loading state. This is stated once here and not repeated per screen; the one exception is Flow D's long POST, handled in §3.

Every page carries the same shell:

```
┌─────────────────────────────────────────────────────────────────┐
│ Habit Assistant · Admin                                          │  header
│ [Status] [Users (2)] [Quota] [Audit] [Activity]                  │  nav (wraps on phone)
├─────────────────────────────────────────────────────────────────┤
│ (page content)                                                   │
├─────────────────────────────────────────────────────────────────┤
│ As of 06:14:03  ·  [Refresh]  ·  All times in Asia/Bangkok       │  footer
│ Config (read-only)                                               │
└─────────────────────────────────────────────────────────────────┘
```

- **"As of {time}"** is mandatory on every page. These pages are snapshots with no live updates; without a rendered timestamp the owner cannot tell a fresh page from a tab left open since yesterday. **[Refresh]** is a plain link to the current URL.
- **No `<meta http-equiv="refresh">`.** An auto-refresh that the user cannot turn off fails WCAG 2.2.1 / 2.2.4, resets scroll position mid-read, and fights the owner during exactly the deep-dive flows where they need the page to hold still. If auto-refresh is wanted, ship it as an **opt-in** `?refresh=60` query param (user-initiated, therefore compliant) — see §8 Q5.
- **Timezone is stated once, in the footer.** Every timestamp on every page is `config.app.timezone` wall-clock; saying so once removes the ambiguity from all of them.

---

### Screen 1 — Status (`GET /`)

**Purpose:** answer "is everything healthy?" above the fold, on a phone, with zero taps.
**Primary action:** none — this page's success condition is that the owner *doesn't* act.

**Phone (≤599px) — the layout that matters most:**

```
┌────────────────────────────────┐
│ Habit Assistant · Admin        │
│ [Status] [Users (2)] [Quota]   │
│ [Audit] [Activity]             │
├────────────────────────────────┤
│ ✅  All good                   │  ← VERDICT. Icon + word, never colour alone.
├────────────────────────────────┤
│ 🔔  2 people are waiting for   │  ← NEEDS-YOU. Rendered only when count ≥ 1.
│     approval           Review →│     Whole block is one big tap target → /users
├────────────────────────────────┤
│ ┌─────────────┬──────────────┐ │
│ │ Version     │ Channel      │ │  ← 2-up tile grid on phone
│ │ 1.2.0+line  │ line         │ │
│ ├─────────────┼──────────────┤ │
│ │ Ollama      │ Uptime       │ │
│ │ off         │ 3d 4h 12m    │ │
│ ├─────────────┴──────────────┤ │
│ │ Last webhook event         │ │  ← full-width: needs the room
│ │ 4 minutes ago              │ │     relative primary…
│ │ 2026-08-31 14:03           │ │     …absolute secondary
│ └────────────────────────────┘ │
├────────────────────────────────┤
│ Push quota — Aug 2026          │
│ ▓▓░░░░░░░░░░░░░░░░░░░░  1.2%   │  ← bar is aria-hidden decoration
│ 182 / 15000 · realtime         │  ← THIS line is the accessible truth
│ Normal                  More → │
├────────────────────────────────┤
│ Scheduler                      │
│  minutely_tick    in 42s       │
│  daily_digest     today 20:00  │
│  backup_nightly   tomorrow 03:0│
├────────────────────────────────┤
│ Storage                        │
│  Database      4.2 MB          │
│   +wal 1.1 MB · +shm 32 KB     │
│  Media         18.4 MB         │
│  Last backup   today 03:00     │
│  [ 7 backups ▸ ]               │  ← <details>, collapsed by default
├────────────────────────────────┤
│ Recent errors                  │
│ ✅ No errors since the service │
│    started.                    │
│    This list clears on every   │
│    restart.                    │
└────────────────────────────────┘
```

**Desktop (≥960px):** identical order and identical content, two columns from the tile grid down — left column carries Quota + Scheduler, right column carries Storage + Recent errors. The verdict and needs-you blocks stay full-width at the top. **Nothing is desktop-only.** Anything worth hiding on a phone is not worth rendering on a desktop.

**The verdict, precisely** (composes only data AC8–AC14 already require; adds **no new data source**):

| State | Trigger | Renders |
|---|---|---|
| ✅ **All good** | none of the below | one line, nothing else |
| ⚠️ **Needs a look** | ring buffer non-empty · OR any panel read failed · OR quota ≥ 80% | one line **naming the panel**, linked to it |
| 🛑 **Needs attention** | any scheduler job has `next_run_time` of `None` · OR quota ≥ 100% | one line naming the cause, linked |

Multiple triggers → the highest severity wins and the line names the count: *"Needs attention — 2 things to check"*, with each linked below it. Icon **and** word carry the state; colour is Iris's reinforcement, never the sole signal.

**States:**
- **Empty (fresh restart, nothing has happened):** every panel renders with real content except Recent errors (empty state) and Last webhook event (*"No events since the service restarted"* — AC10). Uptime is small but valid. There is no page-level empty state; a status page always has status.
- **Loading:** none. Full page load.
- **Error (per panel, spec §3.3):** the panel keeps its heading and shows *"Can't read this right now."* plus *"Check the errors panel below."* It never collapses to nothing — a missing heading reads as "this feature doesn't exist", which is a worse lie than "this failed".
- **Success:** N/A — no mutations on this page.

**Recent errors panel — three distinct states, all required:**

```
EMPTY (the good state)         AT CAPACITY (the honest state)
┌────────────────────────┐     ┌─────────────────────────────────────┐
│ Recent errors          │     │ Recent errors                       │
│ ✅ No errors since the │     │ Showing the latest 200. Older       │
│    service started.    │     │ records have been dropped.          │
│    This list clears on │     ├─────────────────────────────────────┤
│    every restart.      │     │ 14:03  ERROR  channels.line         │
└────────────────────────┘     │ Push failed for U4af…: 429          │
                               │ 13:58  WARNING  core.digest         │
POPULATED: same as at-capacity │ Compose skipped for U9c1…           │
minus the "dropped" line.      └─────────────────────────────────────┘
```

The *"clears on every restart"* line in the empty state is **not decoration**. An empty ring buffer immediately after a crash-loop restart looks identical to genuine health. Saying so is the difference between a dashboard the owner can trust and one that quietly lies once a month.

**Quota gauge — three states, all required:**

```
NORMAL (<80%)                      WARN (≥80%)                          STOPPED (≥100%)
┌──────────────────────────┐  ┌──────────────────────────────┐  ┌──────────────────────────────┐
│ Push quota — Aug 2026    │  │ Push quota — Aug 2026        │  │ Push quota — Aug 2026        │
│ ▓▓░░░░░░░░░░░░░░  1.2%   │  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░  87%    │  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  100%   │
│ 182 / 15000 · realtime   │  │ 13050 / 15000 · realtime     │  │ 15000 / 15000 · realtime     │
│ Normal            More → │  │ ⚠️ Close to the cap.         │  │ 🛑 Cap reached.              │
└──────────────────────────┘  │ When it's reached, pushes to │  │ Proactive pushes to other    │
                              │ other users stop. Replies    │  │ users are paused until next  │
                              │ keep working.        More →  │  │ month. Replies keep working. │
                              └──────────────────────────────┘  └──────────────────────────────┘
```

Warn and stopped copy **deliberately mirrors the existing `push_quota_warn` / `push_quota_stop` LINE pushes**. The owner reads the same sentence in the notification and on the dashboard; two different phrasings of one condition would make them wonder if they are two different conditions.

**Accessibility note on the bar:** the visual bar is `aria-hidden="true"`. The `{used} / {cap} · {mode}` line plus the state word is the accessible content. `<meter>` is tempting (it is in the spec's structural sketch) but screen-reader support for it is inconsistent, and its value is fully duplicated by text that has to exist anyway. Iris may render the bar however she likes; the text line is not optional.

**Responsive:**
- **Desktop ≥960px:** two columns below the tiles; 4-up tile grid.
- **Tablet 600–959px:** single column; 3-up tile grid; scheduler and storage stay as tables.
- **Phone ≤599px:** single column; 2-up tile grid; scheduler/storage/errors collapse to the card pattern (see §5); nav wraps to two rows.

---

### Screen 2 — Users (`GET /users`)

**Purpose:** clear pending requests, and see who is active and whether they are actually using the bot.
**Primary action:** Approve a pending user.

**Section order is urgency order: Pending → Active → Invite.** Not table order, not alphabetical.

**Phone (≤599px):**

```
┌────────────────────────────────┐
│ [Status] [Users (2)] [Quota]   │
│ [Audit] [Activity]             │
├────────────────────────────────┤
│ ✅ Approved Somchai. They've   │  ← FLASH: id="flash" tabindex="-1"
│    been messaged.              │     role="status". Focused via #flash.
├────────────────────────────────┤
│ Waiting for approval (2)       │
│ ┌────────────────────────────┐ │
│ │ Somchai                    │ │  ← name is the headline…
│ │ U4af4980a8f1b…c2  [copy]   │ │  ← …raw id small, monospace, selectable
│ │ Asked 12 minutes ago       │ │  ← waiting time = the urgency signal
│ │ [ Approve ▸ ]  [ Block ▸ ] │ │  ← <details> summaries, 44pt tall
│ └────────────────────────────┘ │
│ ┌────────────────────────────┐ │
│ │ U9c1e…7d                   │ │  ← no display name → id IS the headline
│ │ Asked 3 days ago           │ │
│ │ [ Approve ▾ ]              │ │  ← EXPANDED disclosure:
│ │ ┌────────────────────────┐ │ │
│ │ │ Approve U9c1e…7d to    │ │ │
│ │ │ use the bot? They'll   │ │ │
│ │ │ get a message right    │ │ │
│ │ │ away.                  │ │ │
│ │ │ [ Confirm approve ]    │ │ │  ← the real POST submit
│ │ │ Cancel                 │ │ │  ← plain link, closes the <details>
│ │ └────────────────────────┘ │ │
│ │ [ Block ▸ ]                │ │
│ └────────────────────────────┘ │
├────────────────────────────────┤
│ Active (4)                     │
│ ┌────────────────────────────┐ │
│ │ You (owner)                │ │  ← owner's own row: NO Block control
│ │ U1111…aa                   │ │
│ │ Last log 2 hours ago       │ │
│ │ Streak 14 · Digest on · TH │ │
│ └────────────────────────────┘ │
│ ┌────────────────────────────┐ │
│ │ Nok                        │ │
│ │ U2222…bb                   │ │
│ │ Last log 6 days ago        │ │  ← stale: worth seeing, not an alarm
│ │ Streak 0 · Digest off · EN │ │
│ │ [ Block ▸ ]                │ │
│ └────────────────────────────┘ │
├────────────────────────────────┤
│ Invite someone                 │
│ Add a LINE user ID before they │
│ ever message the bot.          │
│ ┌────────────────────────────┐ │
│ │ U…                         │ │  ← inputmode + autocapitalize off
│ └────────────────────────────┘ │
│ [ Add user ]                   │
└────────────────────────────────┘
```

**Desktop (≥960px):** Pending stays as cards (there are rarely more than two, and cards give the confirm disclosure room to open without reflowing a table). **Active becomes a real table**: Name · Chat ID · Last log · Streak · Digest · Language · (action). Invite is a single-row inline form.

**Two design decisions the spec doesn't state, both flagged in §8:**
1. **Active rows carry a Block control** (§8 Q6). R-USER-1 only mandates Approve/Block on *pending* rows, but AC17 accepts any `chat_id`, and revoking access from an active user is the obvious expectation of anyone looking at this list. Omitting it would send the owner back to LINE chat commands for a routine job.
2. **The owner's own row never renders a Block control** (§8 Q7). Blocking yourself is a foot-gun with no in-portal recovery path — the portal would keep serving (the gate is network + header, not user status), but the *bot* would stop obeying you. The row is labeled "You (owner)" and the action cell is empty.

**States:**
- **Empty — Pending (the good state, and the one that renders most days):**
  ```
  ┌────────────────────────────────────────┐
  │ Waiting for approval                   │
  │ ✅ Nobody's waiting right now.         │
  │    Want to add someone ahead of time?  │
  │    Use the invite box below. ↓         │
  └────────────────────────────────────────┘
  ```
  This is a **desirable** empty state, so the copy reads as a status report, not as a failure. The CTA points down the page at the Invite block rather than inventing a new destination — the way out of this empty state already exists on this screen.
- **Empty — Active:** cannot occur in practice (the owner is always active). If it somehow does: *"No active users yet."*
- **Loading:** none.
- **Error (inline, from `?err=`):** banner in the flash slot, above Pending. Never a modal — a modal needs JS or a separate page, and both are wrong for a field-level validation message. The Invite field is re-populated with the rejected value and marked `aria-invalid="true"` with `aria-describedby` pointing at the banner.
- **Success (from `?ok=`):** banner in the flash slot naming the person and stating what actually happened, including whether the notification push succeeded.

**Why `<details>` and not a confirm page for approve/block:** it is pure HTML (zero JS), `<summary>` is natively a button with correct keyboard semantics (Enter/Space toggles, it is in the tab order, it announces expanded/collapsed), it costs no page load, and it degrades safely — in a renderer without `<details>` support the confirm content is simply always visible and the form still submits correctly. It is the only zero-JS confirm that keeps the frequent action at two taps.

**Responsive:**
- **Desktop ≥960px:** Active as a table; Invite inline.
- **Tablet 600–959px:** as desktop, Active table drops the Chat ID column to a second line under the name.
- **Phone ≤599px:** everything is cards, as drawn. Every `<summary>` and button has a ≥44×44pt hit area.

---

### Screen 3 — Invite confirm interstitial (`POST /users/invite`, no `confirm`)

**Purpose:** make a typo in a 33-character opaque id visible before it creates a phantom user.
**Primary action:** confirm the id is right.

```
┌─────────────────────────────────────────────────┐
│ Habit Assistant · Admin                          │
├─────────────────────────────────────────────────┤
│ Add this user?                                   │
│                                                  │
│   U4af4980a8f1b7c3d2e5f6a7b8c9d0e1f              │  ← LARGE monospace,
│                                                  │     wrapped, generously
│ Check the ID character by character. An ID that  │     letter-spaced.
│ doesn't belong to anyone creates a user row for  │
│ someone who will never appear.                   │
│                                                  │
│ [ Yes, add this user ]     Cancel                │
└─────────────────────────────────────────────────┘
```

No nav on this page — it is a decision point, and nav offers exits that abandon the decision ambiguously. **Cancel** is an explicit link back to `/users`.

**States:** no empty/loading state (it is rendered synchronously from the submitted value). Error state cannot occur here — an invalid shape is rejected *before* this page renders (Flow E step 3).

**Responsive:** single column at every width; the id is the only thing that needs room, and it wraps rather than scrolls.

---

### Screen 4 — Quota & digest (`GET /quota`)

**Purpose:** explain quota consumption, and hold the one manual send action.
**Primary action:** read (diagnosis). Secondary, rare, high-ceremony: send digest now.

**Desktop (≥960px):**

```
┌───────────────────────────────────────────────────────────────────────┐
│ [Status] [Users] [Quota] [Audit] [Activity]                            │
├───────────────────────────────────────────────────────────────────────┤
│ ✅ Sent to 4. Skipped 1.                                               │  ← flash (after Flow D)
├───────────────────────────────────────────────────────────────────────┤
│ Push quota                                                             │
│ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░  87%     13050 / 15000 · realtime                │
│ ⚠️ Close to the cap. When it's reached, pushes to other users stop.    │
│    Replies keep working.                                               │
├───────────────────────────────────────────────────────────────────────┤
│ By month                                                               │
│ Mode is currently REALTIME. A digest→realtime change multiplies push   │  ← the first thing to
│ volume; config changes aren't in the audit log, so check this first.   │     check, said plainly
│                                                                        │
│  2026-08  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓  13050    ← this month                   │
│  2026-07  ▓▓                     412                                   │
│  2026-06  ▓▓                     388                                   │
│  2026-05  ▓▓                     401                                   │
│  … (12 months)                                                         │
├───────────────────────────────────────────────────────────────────────┤
│ This month, by user            (sorted by pushes, highest first)       │
│  User            Pushes    Share                                       │
│  Nok             11 900    91%   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓                    │
│  Somchai            740     6%   ▓                                     │
│  You (owner)        410     3%   ▓                                     │
│                                          See what they logged →        │  ← link to /activity
├───────────────────────────────────────────────────────────────────────┤
│ Caps & thresholds                                                      │
│  Active cap       15000  (digest.push_cap, realtime mode)              │
│  Warn at 80%      12000  ⚠️ fired this month                           │
│  Stop at 100%     15000  — not fired                                   │
├───────────────────────────────────────────────────────────────────────┤
│ Daily digest                                                           │
│  Scheduled 20:00 · mode realtime                                       │
│  You (owner)   on                                                      │
│  Nok           off                                                     │
│  Somchai       on                                                      │
│  Pim           on                                                      │
│                                                                        │
│  [ Send digest now ]                                                   │  ← POST, no confirm yet
│  Sends to the 3 users with digest on. You'll confirm on the next page. │
└───────────────────────────────────────────────────────────────────────┘
```

**Block order is diagnostic order** (Flow C): gauge → *is it anomalous?* → *who?* → *what are the limits?* → *what's scheduled?*. The owner reads top-down and stops when answered.

**States:**
- **Empty — By month (a brand-new deployment):** one row for the current month with `0`, plus *"No push history yet."* Not a blank table.
- **Empty — This month by user:** *"No pushes recorded this month yet."*
- **Empty — Daily digest roster:** cannot occur (the owner is always a user).
- **Loading:** none.
- **Error:** per block, independently. A failed `monthly_push_history()` shows "unavailable" in that block only; the per-user block and thresholds still render.
- **Success:** the `?ran=` flash after Flow D.
- **Quota stopped:** the **Send digest now** control is **replaced**, not disabled, by: *"Push cap reached — can't send a digest until next month."* A disabled button invites repeated tapping and explains nothing.

**Responsive:**
- **Desktop ≥960px:** as drawn.
- **Tablet 600–959px:** single column; the month bars shorten; tables keep their columns.
- **Phone ≤599px:** month history stays a **table** (three short columns fit, and the bar-shape comparison — the whole point of that block — is destroyed by card collapse). The by-user and digest-roster tables collapse to cards. This is a deliberate per-table decision, not a blanket rule (§5).

---

### Screen 5 — Digest-run confirm interstitial (`POST /quota/digest-run`, no `confirm`)

**Purpose:** state the blast radius of the only irreversible action in the portal.
**Primary action:** confirm, or leave.

```
┌─────────────────────────────────────────────────────────┐
│ Habit Assistant · Admin                                  │
├─────────────────────────────────────────────────────────┤
│ Send today's digest now?                                 │
│                                                          │
│  Goes to        3 users who have digest on               │
│  Uses about     3 pushes                                 │
│  This month     13050 / 15000 used                       │
│                                                          │
│ 🛑 This can't be undone.                                 │
│ If today's scheduled digest already went out, people     │
│ will get it twice.                                       │
│                                                          │
│ This can take a while. Don't close or refresh this page. │  ← the entire loading state
│                                                          │
│ [ Yes, send now ]          Cancel                        │
└─────────────────────────────────────────────────────────┘
```

No nav. A hidden one-time token rides in the form. **Cancel** returns to `/quota` and sends nothing.

**States:**
- **Token spent / replayed submission:** *"Already sent at 06:22 — nothing was sent again."* with a link back to `/quota`. This is the page a refresh-after-submit lands on, and it is why refreshing is safe.
- **Loading:** the interstitial's own warning line **is** the loading state, pre-announced. There is no other available mechanism without JS.
- **Error:** if the run raises partway, the result flash reports the actual `sent`/`skipped` counts, never a clean-run claim, and the exception surfaces in `/`'s errors panel.

**Responsive:** single column at every width. The three-row blast-radius block stays a definition list, not a table, so it never collapses awkwardly.

---

### Screen 6 — Audit (`GET /audit?page=N`)

**Purpose:** "who changed what, and when?"
**Primary action:** read; page backward through history.

**Desktop (≥960px):**

```
┌────────────────────────────────────────────────────────────────────────┐
│ [Status] [Users] [Quota] [Audit] [Activity]                             │
├────────────────────────────────────────────────────────────────────────┤
│ Change history                                                          │
│  When         Who       What            Detail              Source      │
│  08-31 14:03  you       approve user    U4af…c2              portal     │
│  08-31 13:58  Nok       edit            water · 2500 → 2000  command    │
│  08-31 11:20  Somchai   digest off      — → 0                button     │
│  08-30 22:14  you       block user      U9c1…7d              admin      │
│  …                                                                      │
├────────────────────────────────────────────────────────────────────────┤
│      [ ← Newer ]      Page 2 of 14      [ Older → ]                     │
└────────────────────────────────────────────────────────────────────────┘
```

**Pagination is labeled by meaning, not by direction.** Rows are newest-first, so "Previous" is genuinely ambiguous — previous in *list order* is newer in *time*. **Newer / Older** removes the ambiguity entirely, and costs nothing.

Field set and privacy shape are identical to the chat `/audit` (AC23), reusing `audit_view.py`'s `_ACTION_LABEL_MSG_IDS`. Actor renders as "you" for the owner (`audit_actor_you`), else display name, else raw chat id.

**States:**
- **Empty:** *"No changes recorded yet."* No CTA — there is no action that would create an audit row for its own sake, so inventing one would be dishonest.
- **Loading:** none.
- **Error:** whole-page — this page is one query. A failed read renders the heading plus "Can't read this right now" and the pager is suppressed.
- **Page out of range (AC25):** clamps to the last valid page and renders it. **No error message** — the owner asked for "as far back as possible" and got it; an error would be pedantry.

**Known behavior, accepted:** offset pagination drifts as new rows land at the top, so a row can appear twice or be skipped across a page turn. At this write volume (a handful of rows a day, one reader) that is not worth cursor pagination. Noted so nobody reports it as a bug.

**Responsive:**
- **Desktop ≥960px:** five-column table.
- **Tablet 600–959px:** same table; Detail truncates with the full value in `title`.
- **Phone ≤599px:** collapses to cards — each row becomes a block with When as the headline, Who + What as the second line, Detail full-width, Source as a small tag. Pager buttons become full-width, stacked, ≥44pt tall.

---

### Screen 7 — Activity (`GET /activity`)

**Purpose:** "what have users actually been logging?" — the cross-check for Flow C.
**Primary action:** read.

```
┌────────────────────────────────────────────────────────────────────────┐
│ [Status] [Users] [Quota] [Audit] [Activity]                             │
├────────────────────────────────────────────────────────────────────────┤
│ User activity                                                           │
│ ℹ️ This page shows summary data only — habit, value, and time.          │
│    It never shows anyone's message or diary text.                       │
│                                                                         │
│  When         User      Habit        Value        Source                │
│  08-31 14:02  Nok       water        500 ml       command               │
│  08-31 13:40  Somchai   stretch      10 min       button                │
│  08-31 09:15  Nok       journal      —            command               │
│  08-31 08:02  you       water        750 ml       nl                    │
│  …                                                                      │
└────────────────────────────────────────────────────────────────────────┘
```

**The privacy note is a rendered, visible element — not a comment in the HTML.** AC24 forbids diary text from appearing; the note turns that constraint into a stated guarantee the owner can see and trust. A `journal`-type row renders its Value as an em-dash: the row exists (the log happened, the timestamp and the habit are real) but the content is deliberately absent. **The em-dash must not read as "missing data"** — the privacy note above the table is what makes it read as "withheld on purpose", which is exactly why the note has to be on the page and not in a docstring.

**States:**
- **Empty:** *"No activity recorded yet."*
- **Loading:** none.
- **Error:** whole-page, as Screen 6.
- **No pagination in v1** — a single page of the most recent 50. `recent_logs_metadata` accepts an `offset`, so a pager is a later addition if the owner ever asks (§8 Q8).

**Responsive:** identical collapse rule to Screen 6 — table ≥600px, cards below.

---

### Screen 8 — Config (`GET /config`) *[COULD]*

**Purpose:** "what is this process actually running with?"
**Primary action:** read.

```
┌────────────────────────────────────────────────────────────────────────┐
│ Effective configuration — read only                                     │
│ 🔒 Secrets are shown as •••••• and are never rendered in full.          │
│                                                                         │
│ [app]                                                                   │
│   timezone            Asia/Bangkok                                      │
│   db_path             /var/lib/habitbot/habits.db                       │
│ [line]                                                                  │
│   bind_port           8080                                              │
│   channel_secret      ••••••  (hidden)                                  │
│   access_token        ••••••  (hidden)                                  │
│ [digest]                                                                │
│   mode                realtime                                          │
│   push_cap            15000                                             │
│ [portal]                                                                │
│   bind_port           8081                                              │
│   owner_login         (not set)                                         │
└────────────────────────────────────────────────────────────────────────┘
```

Grouped by config section, in `config.toml` order — the owner is comparing this against a file they have open in another window, and any other order makes that comparison manual.

**Redacted fields render `••••••` *and* the word "(hidden)".** Bullets alone are indistinguishable from an unset value at a glance, and "is my token actually configured?" is precisely the question this page gets opened to answer. An unset optional field renders *"(not set)"* — a different, unambiguous string.

**States:** no empty state (config always exists). Error → whole-page "unavailable". No mutations.
**Responsive:** two-column definition list ≥600px; stacked label-over-value below.

---

### Screen 9 — 403 Not authorized

**Purpose:** refuse, and reveal nothing.

```
┌──────────────────────────────┐
│ ไม่มีสิทธิ์เข้าถึง · Not authorized │
└──────────────────────────────┘
```

One line. **No nav, no header, no footer, no version string, no link, no branding, no explanation of which rule refused** (R-SEC-3 forbids "wrong user" enumeration). This is the page a mis-Funneled port serves to the public internet, and every byte of it is a byte an attacker gets for free.

**One deliberate, flagged exception to R-I18N-1 / AC31:** this string is a **hardcoded bilingual constant**, not an `i18n.t()` lookup. Two reasons: the requester's identity — and therefore their language — is by definition unknown at this point; and a config or catalog read that raised must not be able to turn a clean 403 into a 500 that leaks a traceback. **Luna and Vera both need to know this**, or an AC31 "no hardcoded literals" check will flag it as a defect.

---

### Screen 10 — 500 Something broke

```
┌────────────────────────────────────────┐
│ Habit Assistant · Admin                 │
├────────────────────────────────────────┤
│ Something went wrong on this page.      │
│ The details are in the log — check the  │
│ errors panel on the status page.        │
│                                         │
│ → Status                                │
└────────────────────────────────────────┘
```

Localized (this is post-gate, the reader is the owner). **No traceback in the response** (spec §3.3) — but the copy tells the owner exactly where the traceback *is*, which is the actionable half. Generic "Something went wrong" with no next step is the failure mode §5 forbids; this page is the counter-example.

---

## 5. Interaction patterns

Reusable rules. Each is one line, and each is a constraint Luna can check against.

- **Every page is a full load.** No JS is required for any flow. Optional trivial enhancements (a copy-to-clipboard button on a chat id) must be **additive only** — the page must be fully usable with the enhancement absent.
- **Every mutation is POST → 303 → GET.** Never render a mutation's result directly from the POST; a refresh must never re-fire a write.
- **Flash messages travel in the query string** (`?ok=…`, `?err=…`, `?val=…`), because there is no session store and cookies would be a new mechanism for one string. Consequence: refreshing a post-redirect URL re-shows the banner. Harmless (the GET is idempotent) and cheaper than a session.
- **Every flash lands in `#flash`** — a `role="status"`, `tabindex="-1"` region immediately below the nav. The redirect URL carries the `#flash` fragment so the browser moves focus and scroll to it on load. This is the zero-JS substitute for a live region, and it is the whole focus-management story for the portal.
- **Confirm ceremony scales with blast radius, and only with blast radius:**

  | Action | Reversible? | Blast radius | Confirm |
  |---|---|---|---|
  | Approve | yes (Block) | 1 person, 1 push | inline `<details>` disclosure, same page |
  | Block | yes (Approve) | 1 person, 0 pushes | inline `<details>` disclosure, same page |
  | Invite | yes (Block) | 1 typed id → phantom user | **full-page interstitial** (echoes the typed id) |
  | Send digest now | **no** | every user, real quota | **full-page interstitial** (states who/cost/duplication/duration) |

- **`<details>`/`<summary>` is the inline confirm primitive.** Zero JS, native button semantics, native keyboard support, and it degrades to "always visible" rather than to "broken".
- **Destructive/irreversible actions state their blast radius in numbers before confirming** — how many people, how much quota, what can't be undone. Never a bare "Are you sure?".
- **A control whose only possible outcome is failure is not rendered.** It is replaced by a sentence explaining why (e.g. Send digest now under a reached cap). Disabled buttons that don't say why are a dead end.
- **User-typed input is echoed back on validation failure**, escaped and truncated to 64 characters. Re-typing a 33-character opaque id because the form cleared is a self-inflicted wound.
- **Panel-level failure is isolated and named.** A failed panel keeps its heading and says "Can't read this right now" — it never silently disappears, because an absent heading reads as "this feature doesn't exist".
- **Empty states are status reports.** Where empty is the *good* state (no pending, no errors) the copy says so affirmatively. A CTA appears only where there is a genuinely useful next action; inventing one for an empty audit log would be noise.
- **Errors are plain-language and name the next step.** Never a bare "Something went wrong" — §4 Screen 10 is the model.
- **Tables collapse to cards below 600px via `td[data-label]`**, not by rendering the data twice. **Structural requirement on Luna:** every `<td>` in a collapsible table must carry `data-label="<its column heading>"`, so the CSS card layout can render `td::before { content: attr(data-label) }`. Without it the phone view is an unlabeled column of values. This is a markup contract, not a style choice.
  - **Exception, deliberate:** the Quota *By month* table stays a table on phones. Its value is the shape of the bars across rows, and card collapse destroys exactly that comparison.
- **Relative time is primary, absolute time is secondary.** "4 minutes ago" answers the question; "2026-08-31 14:03" is the audit trail. Show both where the space allows, relative alone where it doesn't. Exception: the Audit and Activity tables are chronological records — absolute timestamps lead there.
- **No auto-refresh.** Every page states "As of {time}" and offers a Refresh link. Opt-in `?refresh=N` only (§8 Q5).
- **Timezone is declared once per page, in the footer.** Every timestamp is `config.app.timezone` wall-clock.
- **Pagination is labeled by meaning** (Newer / Older), never by list direction (Prev / Next), on newest-first data.

---

## 6. Accessibility requirements

- **WCAG level: AA.** The rich-menu asset already holds this line (`assets/richmenu/README.md` documents 17.76:1 and 5.47:1 measured contrast, and refuses to build below its floors). The portal does not get to be the weaker surface.
- **Keyboard navigation:** every interactive element is a native `<a>`, `<button>`, `<input>`, or `<summary>` — there are no custom widgets, so the tab order is document order and needs no `tabindex` beyond the `-1` on `#flash`. **No custom tab order anywhere.** `<summary>` gives the inline confirms Enter/Space activation and expanded/collapsed announcement for free; this is a large part of why it was chosen over a scripted disclosure.
- **Focus management:** the only focus event in the portal is post-redirect. The `#flash` fragment moves focus to the `tabindex="-1"` banner, so a keyboard or screen-reader user lands on the outcome of what they just did rather than at the top of an unchanged-looking page. No modals exist, so there is no focus trap to manage — a direct benefit of the zero-JS constraint.
- **Screen reader:**
  - Flash region is `role="status"` (polite) — it is present at load, so the fragment-focus is what actually surfaces it.
  - The quota bar is `aria-hidden="true"`; the `{used} / {cap} · {mode}` text line is the accessible content.
  - Month/share bars in Quota are likewise `aria-hidden`; the numeric column beside each is the truth.
  - Icon-only controls: **there are none.** Every control is labeled in words. The status emoji (✅ ⚠️ 🛑 🔔) are *accompanied* by their state word, never used alone — so no `aria-label` patching is needed.
  - The Invite field on error gets `aria-invalid="true"` and `aria-describedby` pointing at the error banner.
  - Tables use `<th scope="col">`; the card-collapse `data-label` values are visual only (`::before` content is not reliably announced) — which is fine, because in card mode the underlying `<th>` association still carries for assistive tech reading the table semantically.
- **Color independence:** every state carries an **icon + a word**. The verdict is "✅ All good" / "⚠️ Needs a look" / "🛑 Needs attention"; the quota gauge is "Normal" / "Close to the cap" / "Cap reached"; the errors panel says "No errors" or lists levels as text (`ERROR`, `WARNING`). Iris's colour is reinforcement on top of a design that already works in greyscale.
- **Touch targets:** minimum 44×44pt for every link, button, and `<summary>` at phone widths. The pending-row Approve/Block summaries and the pagination controls are the two places this is most at risk — both are explicitly full-width and vertically padded on phone in §4.
- **Text sizing:** no fixed-height containers that clip at 200% zoom; every panel grows. Line lengths cap around 70 characters on desktop.
- **Thai rendering:** Thai has taller ascender/descender stacks than Latin (vowel + tone marks). Line-height must accommodate stacked marks without clipping — Iris owns the value, but the requirement is stated here because a Latin-tuned line-height visibly clips Thai tone marks and the owner reads Thai by default.
- **Language attribute:** the `<html lang>` must reflect the resolved render language (`th` or `en`), or screen readers pronounce Thai with an English voice.

---

## 7. Microcopy

**Thai is primary** (the owner's preference). Tone: the bot's voice is warm to *users* ("เก่งมาก", "ลองใหม่อีกครั้งนะ") but noticeably more clinical to the *owner* (`digest_quota_warning`, `push_quota_warn` open with "ข้อความถึงเจ้าของบอท"). The portal is entirely an owner surface, so it takes the **owner register: calm, plain, direct — no cheerleading, no apology, no "Oops!"** Softeners like "นะ" appear only where the message is genuinely reassuring (an empty pending list), never on an error.

Emoji follow the catalog's existing vocabulary (✅ ⚠️ 🛑 🔔 🧾 📋 🔒) and always sit **beside a word**, never instead of one.

All keys are prefixed `portal_*` per R-I18N-1 and carry both `en` and `th`.

| Where | TH (primary) | EN |
|---|---|---|
| Nav — Status | สถานะ | Status |
| Nav — Users (n waiting) | ผู้ใช้ ({n}) | Users ({n}) |
| Nav — Quota | โควตา | Quota |
| Nav — Audit | ประวัติการเปลี่ยนแปลง | Audit |
| Nav — Activity | กิจกรรมผู้ใช้ | Activity |
| Footer — Config | ค่าตั้งค่า (อ่านอย่างเดียว) | Config (read-only) |
| Footer — as-of | ข้อมูล ณ {time} | As of {time} |
| Footer — refresh | รีเฟรช | Refresh |
| Footer — timezone | เวลาทั้งหมดเป็นเวลา {tz} | All times in {tz} |
| **Verdict — ok** | ✅ ทุกอย่างปกติ | ✅ All good |
| **Verdict — warn** | ⚠️ มีบางอย่างต้องดู — {what} | ⚠️ Needs a look — {what} |
| **Verdict — alarm** | 🛑 มีเรื่องต้องจัดการ — {what} | 🛑 Needs attention — {what} |
| Verdict — multiple | มี {n} เรื่องต้องดู | {n} things to check |
| **Needs-you — pending** | 🔔 มี {n} คนรอการอนุมัติอยู่ | 🔔 {n} people are waiting for approval |
| Needs-you — link | ดูรายการ → | Review → |
| Tile — last event, none | ยังไม่มีข้อความเข้ามาตั้งแต่ระบบเริ่มทำงาน | No events since the service restarted |
| **Quota — normal** | ปกติ | Normal |
| **Quota — warn** | ⚠️ ใกล้ถึงเพดานแล้ว เมื่อถึงเพดาน การพุชถึงผู้ใช้อื่นจะหยุด การตอบกลับยังทำงานปกติ | ⚠️ Close to the cap. When it's reached, pushes to other users stop. Replies keep working. |
| **Quota — stopped** | 🛑 ถึงเพดานแล้ว การพุชเชิงรุกถึงผู้ใช้อื่นหยุดชั่วคราวจนถึงเดือนถัดไป การตอบกลับยังทำงานปกติ | 🛑 Cap reached. Proactive pushes to other users are paused until next month. Replies keep working. |
| Quota — line | ใช้ไป {used} จาก {cap} ({pct}%) · โหมด {mode} | {used} / {cap} ({pct}%) · {mode} |
| Quota — mode note | ตอนนี้ใช้โหมด {mode} · การเปลี่ยนโหมดไม่ได้ถูกบันทึกในประวัติ ถ้ายอดพุ่งขึ้นผิดปกติ ให้ดูตรงนี้ก่อน | Mode is currently {mode}. Config changes aren't in the audit log — if the total jumped, check this first. |
| Quota — month empty | ยังไม่มีประวัติการพุช | No push history yet |
| Quota — user empty | เดือนนี้ยังไม่มีการพุช | No pushes recorded this month yet |
| Quota — warn fired | ⚠️ แจ้งเตือนแล้วเดือนนี้ | ⚠️ fired this month |
| Quota — not fired | — ยังไม่แจ้งเตือน | — not fired |
| **Errors — empty** | ✅ ยังไม่มีข้อผิดพลาดตั้งแต่ระบบเริ่มทำงาน | ✅ No errors since the service started. |
| **Errors — empty, note** | รายการนี้จะล้างทุกครั้งที่ระบบรีสตาร์ต | This list clears on every restart. |
| **Errors — at capacity** | แสดง {n} รายการล่าสุด รายการที่เก่ากว่านี้ถูกทิ้งไปแล้ว | Showing the latest {n}. Older records have been dropped. |
| **Panel unavailable** | อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้ | Can't read this right now. |
| Panel unavailable — hint | ดูรายละเอียดได้ที่บันทึกข้อผิดพลาดด้านล่าง | Check the errors panel below. |
| **Pending — empty** | ✅ ตอนนี้ไม่มีใครรอการอนุมัติ | ✅ Nobody's waiting right now. |
| **Pending — empty, CTA** | อยากเพิ่มใครไว้ล่วงหน้า? ใช้ช่องเชิญด้านล่างได้เลย ↓ | Want to add someone ahead of time? Use the invite box below. ↓ |
| Pending — waiting since | ขอเข้าใช้เมื่อ {ago} | Asked {ago} |
| Users — owner row | คุณ (เจ้าของบอท) | You (owner) |
| Users — stats line | บันทึกล่าสุด {ago} · ต่อเนื่อง {streak} วัน · สรุปรายวัน {digest} · ภาษา {lang} | Last log {ago} · Streak {streak} · Digest {digest} · {lang} |
| Users — never logged | ยังไม่เคยบันทึก | Never logged |
| **Approve — summary** | อนุมัติ | Approve |
| **Approve — confirm body** | อนุมัติ {name} ให้ใช้บอทได้? ระบบจะส่งข้อความแจ้งให้ทันที | Approve {name} to use the bot? They'll get a message right away. |
| Approve — confirm button | ยืนยันอนุมัติ | Confirm approve |
| **Block — summary** | บล็อก | Block |
| **Block — confirm body** | บล็อก {name}? เขาจะใช้บอทไม่ได้ คุณอนุมัติใหม่ทีหลังได้ | Block {name}? They won't be able to use the bot. You can approve them again later. |
| Block — confirm button | ยืนยันบล็อก | Confirm block |
| Cancel (both) | ยกเลิก | Cancel |
| **Invite — heading** | เชิญผู้ใช้ | Invite someone |
| Invite — help | เพิ่ม LINE user ID ไว้ล่วงหน้า ก่อนที่เขาจะทักบอทครั้งแรก | Add a LINE user ID before they ever message the bot. |
| Invite — submit | เพิ่มผู้ใช้ | Add user |
| **Invite — confirm heading** | เพิ่มผู้ใช้คนนี้? | Add this user? |
| **Invite — confirm body** | ตรวจสอบ ID ทีละตัวอักษร ถ้า ID ไม่ตรงกับใคร ระบบจะสร้างผู้ใช้ที่ไม่มีตัวตนขึ้นมา | Check the ID character by character. An ID that doesn't belong to anyone creates a user row for someone who will never appear. |
| Invite — confirm button | ยืนยัน เพิ่มผู้ใช้ | Yes, add this user |
| **Flash — approved** | ✅ อนุมัติ {name} แล้ว และส่งข้อความแจ้งเรียบร้อย | ✅ Approved {name}. They've been messaged. |
| **Flash — approved, push failed** | ✅ อนุมัติ {name} แล้ว แต่ส่งข้อความแจ้งไม่สำเร็จ เขาใช้งานได้แล้วแต่ยังไม่รู้ตัว | ✅ Approved {name}, but the notification didn't send. They have access but don't know it yet. |
| **Flash — blocked** | 🚫 บล็อก {name} แล้ว | 🚫 Blocked {name}. |
| **Flash — invited** | ✅ เพิ่ม {chat_id} แล้ว เขาจะใช้งานได้ทันทีที่ทักบอทครั้งแรก | ✅ Added {chat_id}. They'll have access the first time they message the bot. |
| **Error — invalid id** | ID นี้ไม่ถูกต้อง LINE user ID ขึ้นต้นด้วย U ตามด้วยตัวอักษรและตัวเลข ยังไม่มีการบันทึกอะไร | That ID isn't valid. A LINE user ID starts with U followed by letters and numbers. Nothing was saved. |
| **Error — unknown user** | ไม่พบผู้ใช้ ID นี้ อาจถูกจัดการไปแล้วจากแชท ยังไม่มีการบันทึกอะไร | No user with that ID. It may have already been handled from chat. Nothing was saved. |
| **Digest — send button** | ส่งสรุปรายวันตอนนี้ | Send digest now |
| Digest — send help | จะส่งถึงผู้ใช้ {n} คนที่เปิดรับสรุป คุณจะได้ยืนยันอีกครั้งในหน้าถัดไป | Sends to the {n} users with digest on. You'll confirm on the next page. |
| **Digest — confirm heading** | ส่งสรุปของวันนี้เลยไหม? | Send today's digest now? |
| Digest — confirm rows | ส่งถึง {n} คนที่เปิดรับสรุป · ใช้โควตาประมาณ {n} ครั้ง · เดือนนี้ใช้ไป {used}/{cap} | Goes to {n} users who have digest on · Uses about {n} pushes · This month {used} / {cap} used |
| **Digest — irreversible** | 🛑 ส่งแล้วยกเลิกไม่ได้ ถ้าสรุปรอบปกติของวันนี้ส่งไปแล้ว ผู้ใช้จะได้รับซ้ำ | 🛑 This can't be undone. If today's scheduled digest already went out, people will get it twice. |
| **Digest — duration warning** | อาจใช้เวลาสักครู่ อย่าปิดหรือรีเฟรชหน้านี้ | This can take a while. Don't close or refresh this page. |
| Digest — confirm button | ยืนยัน ส่งเลย | Yes, send now |
| **Digest — result** | ✅ ส่งสรุปแล้ว {sent} คน · ข้าม {skipped} คน | ✅ Sent to {sent}. Skipped {skipped}. |
| **Digest — already run** | ส่งไปแล้วเมื่อ {time} ระบบไม่ได้ส่งซ้ำ | Already sent at {time} — nothing was sent again. |
| **Digest — blocked by cap** | ถึงเพดานพุชแล้ว ส่งสรุปไม่ได้จนกว่าจะถึงเดือนถัดไป | Push cap reached — can't send a digest until next month. |
| **Activity — privacy note** | ℹ️ หน้านี้แสดงเฉพาะข้อมูลสรุป — ประเภทกิจกรรม ค่าที่บันทึก และเวลา ไม่แสดงข้อความหรือไดอารี่ของผู้ใช้ | ℹ️ This page shows summary data only — habit, value, and time. It never shows anyone's message or diary text. |
| Activity — empty | ยังไม่มีการบันทึกกิจกรรม | No activity recorded yet. |
| Audit — empty | ยังไม่มีการเปลี่ยนแปลงที่บันทึกไว้ | No changes recorded yet. |
| **Pager** | ← ใหม่กว่า · หน้า {page} จาก {total} · เก่ากว่า → | ← Newer · Page {page} of {total} · Older → |
| **Config — secrets note** | 🔒 ข้อมูลลับแสดงเป็น •••••• และจะไม่ถูกแสดงเต็มไม่ว่ากรณีใด | 🔒 Secrets are shown as •••••• and are never rendered in full. |
| Config — hidden | (ซ่อนไว้) | (hidden) |
| Config — not set | (ยังไม่ได้ตั้งค่า) | (not set) |
| **403** | ไม่มีสิทธิ์เข้าถึง · Not authorized | *(same string — hardcoded bilingual, see Screen 9)* |
| **500** | เกิดข้อผิดพลาดในหน้านี้ รายละเอียดอยู่ในบันทึก ดูได้ที่แผงข้อผิดพลาดในหน้าสถานะ | Something went wrong on this page. The details are in the log — check the errors panel on the status page. |

**Relative-time strings** (`{ago}`) need their own small set — "เมื่อสักครู่ / {n} นาทีที่แล้ว / {n} ชั่วโมงที่แล้ว / {n} วันที่แล้ว" vs "just now / {n} minutes ago / {n} hours ago / {n} days ago". Thai does not pluralize, English does; the EN forms need singular variants. Flagging because this is the one place in the portal where a naive shared formatter produces "1 minutes ago".

---

## 8. Open questions for the user

Each carries who to ask and my default if unanswered.

- **Q1 — Pending count in the nav (shared-surface implication).** The `Users (2)` badge is the portal's highest-value glanceability feature, but it means `layout.py` — which every module renders through — needs a pending-user count on **every** page render, and must fail open (a DB hiccup renders a plain `Users`, never a blank nav or a 500). This is a small addition to the shared surface, not to any one module. **Default: ship it**, with the count computed in `layout.py` from `deps.db`, wrapped in the codebase's standard try/except-and-log posture. **Who:** Archi → Sophia/Irine (shared-surface scope).

- **Q2 — Put the portal URL in the `access_request` LINE push.** Today that push offers only `/approve {chat_id}`. Adding "…or approve in the portal: {url}" turns Flow B from "remember your bookmark" into two taps from the notification. Cost: one new config key (the portal's tailnet URL — the app cannot derive its own MagicDNS name) and one i18n key, both trivial; it is a **change to `SPEC-LINE-PORTAL.md`'s scope**, not a UX-only decision, which is why it is a question and not a design. **Default if unanswered: don't change the push in v1** — the owner bookmarks the portal, and the existing chat command still works. **Who:** user.

- **Q3 — Double-send guard for "Send digest now" (mechanism only).** The *behavior* is not optional: a replayed submission must not send twice (§3 Flow D). The *mechanism* is open — a one-time token minted on the confirm interstitial (recommended: correct, ~10 lines, stateless across the two requests via an in-memory set) or an in-memory "last run at" throttle (simpler, coarser, blocks a legitimate second send within the window). **Default: the one-time token.** **Who:** Sophia/Irine.

- **Q4 — Config changes are invisible to the audit log.** A `digest.mode` flip from `digest` to `realtime` is the single most likely cause of a quota jump, and it leaves no trace the portal can show — Flow C can therefore say *what* the mode is but never *when it changed*. Auditing config load would close the gap. **Default: don't add config auditing in v1**; instead the Quota page states the active mode prominently with the "check this first" note (already designed in §4 Screen 4 and §7). **Who:** user (is a real cause-and-effect gap acceptable in v1?).

- **Q5 — Auto-refresh on the status page.** A dashboard invites it, but an un-stoppable auto-refresh fails WCAG 2.2.1/2.2.4 and resets scroll mid-read. **Default: no auto-refresh; every page shows "As of {time}" plus a Refresh link, with an opt-in `?refresh=60` param for a wall-mounted screen.** **Who:** user (do you ever leave this open on a second monitor?).

- **Q6 — Block control on *active* users.** R-USER-1 mandates Approve/Block on pending rows only; AC17's route accepts any `chat_id`. Revoking an active user is a routine job with no other portal path. **Default: render a Block confirm on active rows too** — the route and AC already support it, and omitting it sends the owner back to chat commands for something the page is clearly about. **Who:** Archi (is this inside the approved scope, or a spec addition?).

- **Q7 — The owner's own row must not be blockable.** Blocking yourself has no in-portal recovery (the portal gate is network + header, so the page keeps serving while the *bot* stops obeying you). **Default: the owner's row renders "You (owner)" with no Block control**, and `POST /users/block` on the owner's own chat_id is refused server-side with a localized inline error — the UI omission alone is not a guard. **Who:** Archi (needs a line in the spec + an AC if it is to be tested).

- **Q8 — Pagination on `/activity`.** `recent_logs_metadata` takes an `offset` but no AC requires a pager. **Default: single page of the most recent 50, no pager in v1** — the feed is a spot-check for Flow C, not a browsable archive. **Who:** user.

- **Q9 — EN/TH toggle in the portal.** The owner reads Thai by default, but config keys, log levels, and tracebacks on this surface are all English. A `?lang=en` override would be useful. Cost is not the toggle — it is threading the param through **every** nav href, form action, and pagination link in `layout.py`. **Default: no toggle in v1**; the portal renders in the owner's `language_pref` per AC31. **Who:** user.

---

## 9. Hand-off to Iris

**Theme:** Modern & Clean, matching the design language the LINE edition has already established in `assets/richmenu/README.md` — near-white page, white cards, hairline borders, **one accent: teal `#0F766E`** (measured 5.47:1 on white; the README's own reasoning for choosing that specific teal over `#0D9488` — that it clears AA *for text*, not just for graphics — applies to the portal too, where the accent will carry link and button text). Flat, no gradients, no shadows. The portal is the same product as the rich menu and must not read as a different one.

**Visual priority, in order.** Spend the design budget where the daily flow lives:
1. **Screen 1 Status, phone width** — the verdict block, the needs-you block, and the quota gauge. This is the page opened every morning and it is the *only* one the owner may ever see. Its three verdict states and the gauge's three states are the highest-value visual work in the portal.
2. **Screen 2 Users, phone width** — specifically the pending card and its `<details>` confirm in both collapsed and expanded states.
3. **Screen 4 Quota, desktop** — the month-history bars, whose comparative shape is the whole diagnostic value of Flow C.
4. Everything else (Audit, Activity, Config, interstitials, 403/500) is table-and-text work that inherits the system.

**Components you need to define:**
- **Verdict banner** — 3 states (ok / warn / alarm), icon + word + optional linked detail. Full-bleed at the top of Status.
- **Needs-you banner** — a single, whole-block tap target; conditional (absent at zero).
- **Flash banner** — 2 states (ok / error). Must be visually distinct from the verdict banner even though both sit near the top of a page, or the owner will confuse "a thing you just did" with "the state of the system".
- **Stat tile** — label over value; 2-up / 3-up / 4-up grid.
- **Quota gauge** — bar plus text line plus state line; 3 states. Bar is decorative (`aria-hidden`).
- **Horizontal bar-in-row** — used in the month history and the by-user share tables. Same primitive, two uses.
- **Panel** — heading + body; must have an "unavailable" variant that keeps its heading.
- **Data table** — with `<th scope="col">` and the `td[data-label]` card-collapse rule below 600px.
- **Card list** — the phone form of the table, and the desktop form of the pending list.
- **Inline confirm disclosure** — a styled `<details>`/`<summary>` pair. The `<summary>` must **look and behave like a button** (44pt, clear affordance) while remaining a `<summary>`; the expanded panel must be visually contained so it reads as belonging to its row.
- **Interstitial page** — nav-less, single-column, one decision. Two instances (invite, digest-run).
- **Pager** — Newer / page-of / Older; full-width stacked on phone.
- **Definition list** — the Config page and the digest-run blast-radius block.
- **Monospace id treatment** — chat ids appear at three sizes: small and secondary in lists, large and letter-spaced on the invite interstitial, inline in audit detail cells.
- **Status tag** — `ERROR` / `WARNING` levels, `command` / `nl` / `button` / `portal` / `admin` sources. Text, not colour-only.

**UX constraints you must respect:**
- **Nothing may require JavaScript.** No modal dialogs, no dropdown menus, no tooltips that hide information, no hamburger nav, no tab components, no toasts that fade. If a visual pattern needs a script to work, it is the wrong pattern here.
- **Never encode state in colour alone.** Every state already ships with an icon and a word (§6); your colour is the third, reinforcing layer. The design must survive greyscale.
- **The `<summary>` styled as a button must stay a `<summary>`** — swapping it for a `<button>` would break the zero-JS confirm entirely.
- **The nav must wrap, not scroll or collapse.** Five items at phone width, two rows. A horizontally scrolling nav hides destinations; a hamburger needs JS.
- **Don't design a fixed-width sidebar.** The nav is a wrapping horizontal row at every breakpoint; there is no sidebar in this IA.
- **Line-height must clear Thai stacked vowel + tone marks.** A Latin-tuned value visibly clips them, and Thai is the default render language.
- **Bars are decoration; the number beside them is the content.** Style them freely, but never let a bar be the only carrier of a value.
- **The 403 page (Screen 9) gets no styling that reveals anything** — no logo, no font loading from an identifiable path, no version. Treat it as a system page, not a product page. It is the one page in this portal that a hostile stranger might see.
