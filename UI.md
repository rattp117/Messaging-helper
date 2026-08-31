# UI Design — LINE edition: Admin Web Portal

> Consumes `SPEC-LINE-PORTAL.md` (32 ACs) and `UX.md` (10 screens, §9 hand-off).
> Produces the visual system Luna hard-codes into `core/portal/layout.py`.
> Target baseline `1.2.0+line`.
>
> **This portal does not get its own design language.** The LINE edition already has
> one — `assets/richmenu/README.md`, shipped, contrast-verified, enforced by a
> generator that refuses to build below its floors. Every token below is either
> lifted from that file unchanged or *derived from it by the same `_tint()` formula*.
> The two surfaces are the same product; a stranger looking at the rich menu and
> the portal must not be able to tell they were designed on different days.

---

## 1. Theme

**Style:** Modern & Clean — inherited, not chosen. Near-white ground, white cards,
hairline borders, **one accent (teal `#0F766E`)**, flat: no gradients, no shadows,
no glass, no neumorphism. Extended with exactly two semantic tiers the rich menu
never needed (warn, stop) because the portal has states the menu doesn't have.

**Mode: light only. Stated explicitly and deliberately.** No dark palette, no
`prefers-color-scheme` block. Reasons: (1) one user, on their own two devices,
who has never asked for dark; (2) the rich menu is a light-only asset and a dark
portal would visibly leave the product's design language; (3) every byte of a
second palette is a byte in the `<style>` block that ships on every page render
under an 8KB budget; (4) an unused theme is an untested theme — it would silently
rot. If the owner later wants dark, it is one `@media` block redefining 13 custom
properties, and nothing else in this document changes. That is the whole cost of
the decision, and it is why light-only is safe rather than lazy.

**Density:** Comfortable. 44px minimum on every interactive element (Maya §6 —
phone-first, one-handed, in the minute a join request lands). Table rows are
comfortable rather than compact; the datasets are ~50 rows, not 5,000.

**Motion: none.** Not "subtle" — **zero**. There are no transitions, no
animations, no `@keyframes` anywhere in the stylesheet. Every state change in this
portal is a full page load or a native `<details>` toggle, both of which the
browser owns. Consequently there is also **no `prefers-reduced-motion` block**: it
would be dead CSS guarding motion that does not exist. Hover feedback is an
instant colour swap. *(If anyone later adds a transition, they add the guard with
it — noted in §9 for Luna.)*

---

## 2. Design tokens

Two provenances, both stated per row:
**[RM]** = taken verbatim from `assets/richmenu/README.md`.
**[D]** = derived here by the rich menu's own `_tint(colour, amount)` /
shade functions, never hand-picked.
All contrast figures below were computed with the *identical* `_luminance()` /
`contrast()` functions from `assets/richmenu/generate_richmenu.py`, and the
rich-menu numbers reproduce exactly (INK on CARD = 17.76, ACCENT on CARD = 5.47),
which is the check that the calculator is the same one.

### Colour

| Token | Value | Src | Use | Contrast (measured) |
|---|---|---|---|---|
| `--bg` | `#FAFAFB` | [RM] | Page ground | — |
| `--card` | `#FFFFFF` | [RM] | Panels, cards, table body | 1.04:1 on `--bg` |
| `--s2` | `#F4F6F8` | [D] | Table headers, notes, id block, tags | 1.04:1 on `--bg` |
| `--line` | `#E3E7EC` | [RM] | Hairline. Decorative containers + bar track | 1.24:1 on `--card` |
| `--line2` | `#838B96` | [D] | **Interactive** boundaries (input fields) | **3.44:1** on `--card`, 3.30:1 on `--bg` |
| `--ink` | `#16181D` | [RM] | Headings, values, body | **17.76:1** on `--card`, 17.02:1 on `--bg` |
| `--ink2` | `#454D5A` | [D] | Labels, secondary body, nav rest state | **8.53:1** / 8.18:1 |
| `--ink3` | `#606A78` | [D] | Muted meta (timestamps, tile labels, footer) | **5.48:1** / 5.26:1 / 5.06:1 on `--s2` |
| `--teal` | `#0F766E` | [RM] | The one accent: links, primary fill, ok tier | **5.47:1** on `--card`, 5.25:1 on `--bg` |
| `--teal-d` | `#0C615A` | [D] shade .18 | Filled-button hover/active | 7.31:1 with white |
| `--teal-t` | `#E7F1F0` | [D] tint .90 | ok-tier fills (verdict, state strip, confirm) | teal on it **4.75:1**; ink 15.42:1 |
| `--warn` | `#9A5B00` | [D] | **Warn tier** — quota ≥ 80%, warn-fired flags | **5.43:1** on `--card`, 5.20:1 on `--bg` |
| `--warn-t` | `#F5EFE6` | [D] tint .90 | warn-tier fills | warn on it **4.75:1**; ink 15.54:1 |
| `--stop` | `#B3261E` | [D] | **Stop tier** — quota 100%, block, errors, 500 | **6.54:1** on `--card`, 6.27:1 on `--bg` |
| `--stop-t` | `#F7E9E8` | [D] tint .90 | stop-tier fills | stop on it **5.53:1**; ink 15.03:1 |

**Why these two new hues, and why not the obvious ones.**

- The warn tier had to be a hue that reads as *caution* next to a deep teal without
  fighting it. `#9A5B00` is a deep ochre — teal's rough complement, so it is
  unmistakably a different signal, but desaturated and dark enough to stay in the
  same "calm and clinical" register the rich-menu README chose the teal for.
  **Tailwind's amber-700 `#B45309` was tested and rejected**: it clears AA on white
  (5.02:1) but only reaches **4.39:1 on its own 90% tint**, so warn-coloured text
  inside a warn-tinted banner would fail. `#9A5B00` gives 4.75:1 there. This is the
  same failure mode the rich-menu README documents for `#0D9488` vs `#0F766E` — a
  colour that passes the easy check and fails the one that matters.
- The stop tier `#B3261E` is a slightly earthy red, not a fire-engine one, for the
  same reason: `#DC2626`-class reds are louder than anything in this product. At
  6.54:1 it is the highest-contrast of the three tiers, which is correct — it is
  the only tier that means *stop*.
- **The ok tier is the accent itself.** No fourth hue was introduced for "success".
  Total palette: one accent + two semantic tiers, which is the rich menu's
  one-accent discipline extended by the minimum the portal's states require.

**The tier system.** `--tier` / `--tier-t` are indirection variables. Four setter
classes (`.ok` `.warn` `.stop` `.mute`) rebind them; every tier-aware component
(`.verdict`, `.flash`, `.state`, `.bar i`, `.btn`, `.confirm`, `.empty`, `.tag`)
reads `var(--tier)` and needs no per-tier rules of its own. One class on the
component root changes its whole tier. This is why the stylesheet fits in 8KB.

### Typography

**No webfont. No `@font-face`. No `<link>` to any font host. Zero external
requests of any kind** — the portal is tailnet-only behind `tailscale serve`, it
must render completely on a laptop with no route to the public internet, and a
font request that hangs would block first paint on the one page the owner opens
every morning.

| Token | Value |
|---|---|
| `--sans` | `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans Thai", "Leelawadee UI", Thonburi, sans-serif` |
| `--mono` | `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` |

**How that stack covers Thai.** Font fallback in CSS is **per character**, not per
element: the browser walks the list for *each* glyph. So Latin resolves at the
platform UI font (San Francisco / Segoe UI / Roboto) while Thai glyphs — which
those faces on Windows do not carry — fall through to the first Thai-capable entry:

| Platform | Latin from | Thai from |
|---|---|---|
| iOS / macOS | `-apple-system` (SF) | `Thonburi` (system fallback also supplies it) |
| Windows | `Segoe UI` | `Leelawadee UI` (ships with Windows 8+) |
| Android | `Roboto` | `Noto Sans Thai` |
| Linux | `sans-serif` → fontconfig | `Noto Sans Thai` (Debian/Ubuntu `fonts-noto`) |

`--mono` needs **no Thai coverage by construction**: it is used only for LINE chat
ids, log logger names, and enum values, all ASCII. Never set Thai in `--mono`.

| Token | Value | Notes |
|---|---|---|
| Body | 16px / 1.6 | `<input>` is also 16px — below 16px iOS Safari zooms the viewport on focus, which on a phone-first tool is a visible bug |
| h1 | 24px / 1.35 | Page title |
| h2 | 18px / 1.35 | Panel/section heading |
| Card headline (h3) | 18px / 1.35 | Pending-user name |
| Verdict | 20px / 1.45, 600 | The one oversized element in the portal |
| Secondary / labels | 14px / 1.6 | `--ink2` or `--ink3` |
| Tag (enum) | 12px mono | **Latin-only by construction** — see rule below |
| Large id | 24px mono, `letter-spacing:.06em` | Invite interstitial only |
| Weights | 400 body · 500 labels/nav · 600 headings, buttons, values | System faces only; no 700, nothing needs it |

**Thai line-height rule (Maya §6, honored):**

- **Body line-height is `1.6`, and that is a floor, not a preference.** Thai stacks
  a vowel *and* a tone mark above the base consonant (สร้ำ, กี๋, ที่) and a
  descender below (ญ, ฐ). A Latin-tuned 1.4 clips the upper mark against the line
  above; the owner reads Thai by default, so this is a legibility bug, not polish.
- **Headings are `1.35`, never the Modern-&-Clean default of 1.2.** 1.2 clips
  stacked marks at 24px just as badly as at 16px. This is a deliberate change from
  my house default and the reason is Thai.
- The verdict is `1.45` — it is 20px semibold with an emoji on the line, and it
  wraps to two lines on a phone.
- **Consequences Luna must not undo:** no fixed-height containers on any text
  block, no `overflow:hidden` on a line box, no `line-height` in `px`. All three
  clip tone marks. Every panel in the stylesheet grows with its content.
- **Thai never renders below 14px.** 12px is reserved for `.tag`, whose content is
  always a Latin enum (`ERROR`, `WARNING`, `portal`, `command`, `nl`, `button`,
  `admin`). A tag holding a *localized* string uses `.tag.word` (14px, `--sans`).
- **No `text-transform:uppercase` and no letter-spacing on localized text.** Both
  are Latin-only devices: uppercase is a no-op on Thai and tracking pulls tone
  marks off their bases. This is why table headers here are 14px sentence-case
  rather than the usual 12px uppercase micro-caps.

### Spacing, radius, layout

8pt grid: **4 / 8 / 12 / 16 / 24 / 32 / 48 / 64**. Only 8, 12, 16, 24 and 32 are
actually used; a smaller vocabulary is easier to keep honest in string-built HTML.

| Token | Value | Derivation |
|---|---|---|
| `--r1` | `6px` | Rich menu `CARD_RADIUS` 32px renders at ~0.156× ≈ 5pt; 6px is the nearest step on the grid. Controls, tags, inputs, flash |
| `--r2` | `10px` | Cards, panels, verdict, disclosure body |
| pill | `999px` | Tags and the bar track only |
| `.wrap` max-width | `960px` | Five-column tables fit; prose is capped separately |
| prose max-width | `68ch` | Maya §6 "~70 characters" |
| Breakpoints | `599 / 600 / 960` | Exactly Maya's three bands. No others |
| Page padding | 16px phone → 24px ≥600px | |

**Elevation: none.** No `box-shadow` anywhere. Depth is carried the way the rich
menu carries it — a white surface on a near-white ground (1.04:1) plus a 1px
hairline. The only `outline` in the system is the focus ring.

**Boundary rule (WCAG 1.4.11), stated once:** a border that is *decorative
containment* uses `--line` (1.24:1 — the rich menu's own hairline, and it is not
carrying information). A border that is **the only thing telling you where a
control is** must clear 3:1 — that means `--line2` (3.44:1) on the invite input,
`--teal` (5.47:1) on the needs-you block and the `<summary>` buttons, `--stop`
(6.54:1) on an invalid field. Nav chips and table cells are text-labelled, so their
boxes are decorative and `--line` is correct there.

**Focus ring: `2px solid var(--ink)` with `2px` offset, one rule, every element.**
Ink rather than teal, deliberately: the offset puts the ring on the page ground, so
it is **17.02:1 against `--bg` on every surface in the portal** — it cannot be
defeated by whatever the control's own background is. Even in the worst case, ring
directly against a filled teal button, ink measures **3.24:1** — still past the 3:1
non-text floor. A teal ring on a teal button would be invisible. Mouse users get
`:focus:not(:focus-visible){outline:none}`; if a browser doesn't understand
`:focus-visible` that rule is dropped and the ring simply stays always-on, which is
the safe direction to fail.

---

## 3. Components

Every component Maya's §9 asks for, plus the five her wireframes draw but didn't
name (marked **[interpreted]** — full list in §10). Nothing here needs JavaScript.

### 3.1 Page shell

```html
<!doctype html>
<html lang="th">            <!-- or en — AC31 resolved language -->
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>…</title><style>/* §8 block, verbatim */</style></head>
<body>
<a class="vh" href="#main">ข้ามไปเนื้อหา</a>
<header><div class="wrap">
  <p class="brand">Habit Assistant · Admin</p>
  <nav>…</nav>
</div></header>
<main class="wrap" id="main"> … </main>
<footer><div class="wrap"> … </div></footer>
</body></html>
```

- **`<meta name="viewport">` is mandatory.** Without it a phone renders the page at
  980px CSS pixels and *every* phone layout in `UX.md` — the 2-up tiles, the
  wrapping nav, the card collapse — silently never happens.
- Landmarks `header / nav / main / footer` + one `<h1>` per page are the WCAG 2.4.1
  bypass mechanism; the `.vh` skip link is belt-and-braces and becomes visible on
  focus.
- Footer: `--ink3`, 14px, `border-top:1px solid var(--line)`, 24px above. Carries
  "As of {time} · Refresh · All times in {tz}" and the Config link.

### 3.2 Nav + pending signal

- `nav{display:flex;flex-wrap:wrap;gap:8px}` — **wraps to two rows at phone width,
  exactly as Maya requires. No sidebar, no hamburger, no horizontal scroll.**
- Chip: 44px min-height, 8px/14px padding, `--r1`, `--line` border, `--card`
  background, `--ink2` 500-weight label.
- Current page: `aria-current="page"` → teal fill, white label (5.47:1). The
  attribute is the state carrier; colour reinforces.
- **Pending signal:** `class="pending"` on the Users chip when count ≥ 1 → teal
  border + `--teal-t` fill + teal 600-weight label. **The count itself is in the
  label text** (`ผู้ใช้ (2)` / `Users (2)`, i18n key `portal_nav_users`), so the
  signal survives greyscale and screen readers with nothing extra. Colour is the
  third layer, per Maya §6. Hover on any chip: border → `--line2`, label → `--ink`.
- Fail-open (UX §8 Q1): if the count read raises, render the plain chip with no
  `.pending` class and no number. Never a blank nav.

> **Escalation E1 (§11):** if a *pill badge* is wanted instead of the parenthetical,
> `portal_nav_users` has to split into label + count. That is Maya's copy, not mine,
> so the default here works with her key exactly as written.

### 3.3 Verdict banner — 3 states (visual priority #1)

Full-width, first element in `<main>`, tier-tinted slab.

| State | Class | Fill | Content |
|---|---|---|---|
| All good | `verdict ok` | `--teal-t` | `✅ ทุกอย่างปกติ` |
| Needs a look | `verdict warn` | `--warn-t` | `⚠️ มีบางอย่างต้องดู — {what}` + link |
| Needs attention | `verdict stop` | `--stop-t` | `🛑 มีเรื่องต้องจัดการ — {what}` + link |

- 20px / 600 / line-height 1.45, ink text (15.0–15.4:1 on every tier tint), 16px
  padding, `--r2`, 1px `--line` hairline.
- Multi-cause: headline line, then `<ul>` at 16px/400 with one link per cause.
- Links inside the verdict are `--ink` + underline, not teal: the slab is already
  tinted and a second colour inside it reads as a third state.
- **Colour is never the carrier** — emoji + word + (for warn/stop) the named panel.
  Greyscale-safe: the three states differ in icon, wording, and tint value.

```html
<div class="verdict stop"><span>🛑 มีเรื่องต้องจัดการ — 2 เรื่อง</span>
  <ul><li><a href="#jobs">daily_digest ไม่ได้ตั้งเวลาไว้</a></li>
      <li><a href="/quota">โควตาถึงเพดานแล้ว</a></li></ul></div>
```

### 3.4 Needs-you banner

`<a class="needs" href="/users">` — **the whole block is one tap target**
(min-height 64px, far past 44). White fill, **1px `--teal` border** (5.47:1: this
border is the only thing marking the block's extent, so it must clear 3:1),
`--r2`, ink 600-weight text, teal `<b>` for `ดูรายการ →`. Hover fills `--teal-t`.

Rendered **only** when pending ≥ 1 (Maya Flow A step 2 — an explicit "0 waiting" is
noise). Visually distinct from both banners above and below it by being the only
white block on the page with a full teal outline.

### 3.5 Flash banner — 2 states

`<div id="flash" class="flash ok|stop" role="status" tabindex="-1">`

**White fill + 4px left rule in `--tier` + `--r1` + 16px text.** Maya requires it to
be unmistakable against the verdict banner sitting 40px away; the two share no
visual property: verdict is a *tinted slab with 20px type and no rule*, flash is a
*white card with a coloured left rule and body type*. That distinction survives
greyscale (fill vs no fill, rule vs no rule) and is the only left-rule element in
the entire system, so it is learnable in one exposure.

Placed immediately below the nav; the `#flash` fragment moves focus to it
(`tabindex="-1"`). Error flashes use `.stop`; success uses `.ok`. Never
auto-dismisses (that would need JS) and never blocks anything.

### 3.6 Stat tile + grid

`.tiles` = CSS grid, **2-up phone / 3-up ≥600 / 4-up ≥960** (Maya's exact bands).
`.tile` = white, hairline, `--r2`, 12px padding; label 14px `--ink3`, value
`<b>` 18px `--ink`, optional `<small>` 14px `--ink3` second line. `.wide` spans the
full row — used by "Last webhook event", which carries relative time as the value
and the absolute timestamp as the `<small>`.

### 3.7 Quota gauge — 3 states (visual priority #1)

```html
<section class="panel gauge warn">
  <h2>โควตาการพุช — ส.ค. 2026</h2>
  <div class="bar" aria-hidden="true"><i style="width:87%"></i></div>
  <p><b>13050 / 15000 (87%)</b> · realtime</p>
  <div class="state">⚠️ ใกล้ถึงเพดานแล้ว … <a href="/quota">ดูเพิ่ม →</a></div>
</section>
```

`.state` is a **`<div>`, not a `<p>`** — a `<p>` inherits the 68ch prose cap and
would stop the trailing `More →` link short of the strip's right edge.

| State | Class | Bar fill | `.state` strip |
|---|---|---|---|
| Normal < 80% | `gauge ok` | `--teal` | `--teal-t`, "ปกติ" + `ดูเพิ่ม →` |
| Warn ≥ 80% | `gauge warn` | `--warn` | `--warn-t`, the ⚠️ two-sentence copy |
| Stopped ≥ 100% | `gauge stop` | `--stop` | `--stop-t`, the 🛑 copy |

- **Text is truth, bar is decoration** (Maya §6, non-negotiable): `.bar` carries
  `aria-hidden="true"`; the `{used} / {cap} ({pct}%) · {mode}` line is the accessible
  content, at 18px semibold with `font-variant-numeric:tabular-nums`. **No
  `<meter>`** — inconsistent SR support and it duplicates text that must exist anyway.
- Bar: 10px tall, `--line` track, pill radius, tier fill. Fill vs track measures
  4.41 (teal) / 4.37 (warn) / 5.26 (stop) — all past the 3:1 graphics floor even
  though, being `aria-hidden`, they are not required to.
- `.state` is a **tier-tinted strip with ink text**, not tier-coloured text. The
  warn/stop copy is two full sentences; two sentences set in ochre or red reads as
  shouting, and the tint + emoji + words already carry the tier three times over.
- The `More →` link sits at the strip's right end (`justify-content:space-between`),
  wrapping under on narrow phones.

### 3.8 Bar-in-row — one primitive, two uses

The same `.bar` inside a `<td>` (`width:100%;min-width:56px;margin:0`). Used by the
Quota month history and the by-user share table. The numeric column beside it is
always present and always the truth; the bar is `aria-hidden`.

### 3.9 Panel + unavailable variant

`.panel` = white, hairline, `--r2`, 16px padding, 16px bottom margin, `<h2>` first.

**Unavailable variant keeps the heading** and replaces only the body:

```html
<section class="panel"><h2>ที่เก็บข้อมูล</h2>
  <p class="empty warn">⚠️ อ่านข้อมูลส่วนนี้ไม่ได้ตอนนี้</p>
  <p class="meta">ดูรายละเอียดได้ที่แผงข้อผิดพลาดด้านล่าง</p></section>
```

The panel never collapses to nothing — Maya's rule: an absent heading reads as
"this feature doesn't exist", a worse lie than "this failed".

### 3.10 Data table + card collapse

`<table class="collapse">` with `<th scope="col">`. Header row: `--s2` fill, 14px
600 `--ink2`, `nowrap`. Cells: 10px/12px padding, 1px `--line` bottom rule,
top-aligned. `.num` = right-aligned tabular numerals. `tbody tr:hover` tints `--s2`
**only ≥600px** (there are no hover states on a phone, and it would fight the card
background).

**Below 600px** the `.collapse` table becomes cards: `thead` hidden, each `<tr>` a
bordered `--r2` card, each `<td>` a flex row printing `attr(data-label)` on the left
in 14px `--ink3` and the value on the right.

> **Markup contract (Maya §5, verbatim):** every `<td>` in a `.collapse` table
> **must** carry `data-label="<its column heading>"`. Without it the phone view is
> an unlabelled column of values. Two per-cell modifiers give Maya's exact card
> shape: `td.head` (headline — label suppressed, 18px 600) and `td.full`
> (label above, value below, full width).

**The deliberate exception:** the Quota *By month* table is `<table>` **without**
`.collapse`. Its value is the shape of the bars down the column, and card collapse
destroys exactly that comparison. Three short columns fit at 375px.

### 3.11 Card list

`.card` = white, hairline, `--r2`, 16px padding, 12px gap. `<h3>` 18px headline
(display name, or the chat id when there is no name), `.mono` id line (14px
`--ink2`, `word-break:break-all`), `.meta` 14px `--ink3` for "asked {ago}" or the
stats line, then `.actions` (flex, wrap, 8px gap). The desktop form of the Pending
list and the phone form of the Active table.

### 3.12 Inline confirm disclosure (visual priority #2)

```html
<form method="post" action="/users/approve">
<input type="hidden" name="chat_id" value="U9c1e…7d">
<details class="confirm"><summary>อนุมัติ</summary>
  <div><p>อนุมัติ U9c1e…7d ให้ใช้บอทได้? ระบบจะส่งข้อความแจ้งให้ทันที</p>
  <div class="actions"><button class="btn">ยืนยันอนุมัติ</button>
  <a class="cancel" href="/users">ยกเลิก</a></div></div>
</details></form>
```

- **It stays a native `<summary>`** (Maya's hard constraint). It is *styled* into a
  button by sharing one selector list with `.btn` — literally
  `.btn,.confirm>summary{…}` — so they cannot drift apart: same 44px height, same
  padding, same radius, same 600 weight, same 1px tier border. `.btn` then adds the
  fill; `.confirm>summary` stays white with tier-coloured text (outline style), so
  the *primary* action inside the expanded panel is visibly stronger than the
  trigger that opened it.
- Native marker removed (`list-style:none` + `::-webkit-details-marker`), replaced
  by an explicit caret `▸` / `▾` via `::after`, keeping Maya's wireframe glyphs.
  `<summary>` already announces expanded/collapsed natively — the caret is a
  sighted-user affordance (see escalation E4).
- **Collapsed → expanded containment:** open state squares the summary's bottom
  corners and fills it `--tier-t`; the body `<div>` continues the same tier border
  (no top border) and the same tint, closing with `--r2`. Summary and body read as
  one block belonging to that row — Maya's requirement.
- **Layout:** `.confirm{flex:1 1 auto;min-width:150px}` sits Approve and Block side
  by side in `.actions`; `.confirm[open]{flex-basis:100%}` makes the open one take
  the full card width and pushes the other below — Maya's expanded phone wireframe,
  achieved with two declarations and no JS.
- **Tier:** Approve is default (teal). Block is `<details class="confirm stop">` —
  outlined in `--stop` with stop-coloured text. **Destructive actions are outlined,
  never filled**; a filled red button next to a filled teal one turns a reversible
  one-person action into an alarm. See §11 E3.
- **Degradation:** in a renderer without `<details>`, the body is simply always
  visible and the form still posts. Nothing breaks.

**Markup contract:** `<details class="confirm">` contains **exactly** a `<summary>`
and **one** `<div>`. The stylesheet targets `.confirm>div`.

### 3.13 Quiet disclosure `.more` **[interpreted]**

Maya's Storage panel draws `[ 7 backups ▸ ]` — a `<details>` that is an *expander*,
not a confirm. Same caret rules, but the summary is a 44px teal link-weight row
with no border or fill, so it cannot be mistaken for a confirm button. Its body is
a `.collapse` table (filename / size / time).

### 3.14 Buttons

| Variant | Class | Fill | Text | Border | Used by |
|---|---|---|---|---|---|
| Primary | `btn` | `--tier` | white | `--tier` | Confirm approve, Add user, Yes send now |
| Quiet | `btn quiet` | white | `--tier` | `--tier` | Pager, secondary actions |
| Destructive | `confirm stop` summary / `btn quiet stop` | white | `--stop` | `--stop` | Block, Confirm block |
| Cancel | `cancel` | none | `--ink2` | none | Every interstitial and disclosure |

44px min-height, 10px/18px padding, `--r1`, 600 weight, `font:inherit`. Hover on a
filled button → `--teal-d` (7.31:1 with white). Quiet hover → `--tier-t`.
White-on-tier measures 5.47 (teal) / 6.54 (stop).

**Filled buttons are teal-only, by design.** There is no filled warn or stop
button anywhere in this portal, which is why `.btn:hover` can hard-code `--teal-d`
and no per-tier shade token exists. If Luna ever needs one, that is a token
addition and a note back to me — not an inline colour.

**`<button class="btn">`, never `<input type="submit">`** — the bare `input`
selector in §8 styles text fields.

### 3.15 Form input

Full width (max 420px), 44px min-height, 10px/12px padding, **`--line2` 1px border
(3.44:1 — this box is the control's only boundary)**, `--r1`, `--mono` at **16px**
(below 16px, iOS Safari zooms the viewport on focus). Label above: 14px 500
`--ink2`, 4px gap. Focus: border → `--teal`, plus the global ink ring.
Error: `aria-invalid="true"` → **2px `--stop` border** *and* the flash banner above
carries the message *and* `aria-describedby` points at it — three carriers, none of
them colour alone. The field is re-populated with the rejected value (Maya §5).

Invite field also: `inputmode="latin" autocapitalize="off" autocorrect="off"
spellcheck="false"`.

### 3.16 Status tag

`.tag` = 12px `--mono` pill, `--s2` fill, `--line` border, `--ink2` text — for
`command` / `nl` / `button` / `portal` / `admin` sources and `WARNING` levels.
`.tag.stop` (stop tint + stop border + stop text, 5.53:1) for `ERROR`.
`.tag.warn` (4.75:1) for `WARNING` where it should stand out.
`.tag.word` = 14px `--sans` for any tag holding a **localized** string — e.g. the
dead-scheduler-job marker (§3.20). Tags never carry meaning by colour: the level or
source word *is* the content.

### 3.17 Monospace id — three sizes

| Where | Treatment |
|---|---|
| In lists / cards | `.mono` — 14px, `--ink2`, `word-break:break-all`, selectable |
| In audit Detail cells | `.mono` inline, same size, truncation only via `title` |
| Invite interstitial | `.id` — **24px**, `letter-spacing:.06em`, `--s2` block with hairline + `--r1`, 12px padding, wraps (never scrolls) |

`.id` exists so a transposed character in a 33-character opaque string is visible
(Maya Flow E) — the letter-spacing is doing real work, not decoration.

### 3.18 Note / privacy callout **[interpreted]**

`.note` = `--s2` fill, hairline, `--r1`, 12px padding, 14px `--ink2` (7.87:1).
Deliberately **neutral, not tinted**: the Activity privacy note (ℹ️), the Config
secrets note (🔒) and the Quota mode note are *standing facts*, not states. Tinting
them would spend the tier vocabulary on things that never change.

### 3.19 Empty state

`.empty` = tier-tinted block, `--r1`, 12px padding, `--ink2` text (7.2–7.9:1 on
every tint). `.empty.ok` where empty is the **good** state (no pending, no errors) —
teal tint, ✅, affirmative copy. `.empty.mute` (neutral `--s2`) where empty is
merely a fact (no audit rows, no activity, no push history). `.empty.warn` for a
panel's "can't read this right now". One class, three meanings, no new CSS.

### 3.20 Definition list

`<dl class="dl"><div><dt>…</dt><dd>…</dd></div>…</dl>` — label left, value right,
hairline between rows; **stacks to label-over-value below 600px**. Used by:
Scheduler (job id → next run), Storage (DB / media / last backup), Caps &
thresholds, the digest-run blast-radius block, and the whole Config page.

**Dead scheduler job** (`next_run_time is None` — the state that drives Maya's
"Needs attention" verdict) renders `<dd><span class="tag word stop">ยังไม่ได้ตั้งเวลา</span></dd>`.
Her wireframe never draws this row, but her verdict table requires it to exist.

### 3.21 Pager

`.pager` — centred flex row: `[← ใหม่กว่า] หน้า 2 จาก 14 [เก่ากว่า →]`, links as
`.btn quiet`, the page count as a 14px `--ink2` `<span>`. **Below 600px it stacks
vertically and each control goes full width at 44px.**
Labelled by meaning (Newer / Older), never Prev / Next.
On page 1 the "Newer" control is **not rendered at all** — Maya §5: a control whose
only possible outcome is failure is not rendered. The row stays centred. **[interpreted]**

### 3.22 Interstitial page

`<main class="wrap decide">` — 560px max, 32px top margin, single column at every
width, **brand header, no nav** (nav offers exits that abandon the decision
ambiguously). `<h1>` question, the evidence (`.id` or `.dl`), a `.state stop` strip
for the irreversibility line, then `.actions` with one `.btn` and one `.cancel`.

### 3.23 Log row **[interpreted]**

Maya draws the Recent-errors panel as timestamp / level / logger / message rows.
That is **not a new component**: it is a `<table class="collapse">` with columns
When · Level · Logger · Message, the level in a `.tag`, the message in
`<td class="full">`. On a phone it collapses to exactly the stacked shape her
wireframe draws. Zero new CSS, and it gets `<th scope=col>` semantics for free.

---

## 4. Iconography & imagery

**No icon set. No SVG sprite. No icon font. No images at all.**

The icons in this portal are the **emoji already written into Maya's i18n strings**
— ✅ ⚠️ 🛑 🔔 ℹ️ 🔒 🚫 — which are the same vocabulary the bot already uses in its
LINE messages (`push_quota_warn`, `digest_quota_warning`). They cost zero requests,
zero bytes of CSS, and they are already part of the copy Luna is emitting, so they
cannot drift out of sync with the strings they annotate. They inherit font-size and
the 1.6 line-height and sit on the text baseline.

Per Maya §6 every one of them **sits beside a word, never instead of one**, so no
`aria-label` patching is needed and nothing is lost if a platform renders an emoji
as a monochrome glyph.

*(This is the one place the portal legitimately diverges from the rich menu, which
bans emoji. That ban is about PIL rendering a PNG on a server with no colour-emoji
font installed — a build-time constraint that does not exist for HTML rendered on
the owner's own phone.)*

Imagery: none. There is no photography, illustration, logo, or decorative graphic
anywhere in the portal. The only non-text graphics are the CSS bars, and they are
`aria-hidden` decoration over a number.

---

## 5. Per-screen visual spec

### Screen 1 — Status (`GET /`) — the page that gets the budget

**Phone ≤599px** (the layout that matters — Maya Flow A, daily, one-handed):

```html
<main class="wrap" id="main">
  <div class="verdict ok">✅ ทุกอย่างปกติ</div>

  <a class="needs" href="/users">🔔 มี 2 คนรอการอนุมัติอยู่ <b>ดูรายการ →</b></a>

  <div class="tiles">
    <div class="tile">เวอร์ชัน<b>1.2.0+line</b></div>
    <div class="tile">ช่องทาง<b>line</b></div>
    <div class="tile">Ollama<b>off</b></div>
    <div class="tile">ทำงานมาแล้ว<b>3d 4h 12m</b></div>
    <div class="tile wide">ข้อความล่าสุด<b>4 นาทีที่แล้ว</b>
      <small>2026-08-31 14:03</small></div>
  </div>

  <div class="cols">
   <div>
    <section class="panel gauge ok"> … §3.7 … </section>
    <section class="panel"><h2>ตัวจับเวลา</h2><dl class="dl"> … </dl></section>
   </div>
   <div>
    <section class="panel"><h2>ที่เก็บข้อมูล</h2><dl class="dl"> … </dl>
      <details class="more"><summary>สำรองข้อมูล 7 ชุด</summary>
        <table class="collapse"> … </table></details></section>
    <section class="panel"><h2>ข้อผิดพลาดล่าสุด</h2>
      <p class="empty ok">✅ ยังไม่มีข้อผิดพลาดตั้งแต่ระบบเริ่มทำงาน</p>
      <p class="meta">รายการนี้จะล้างทุกครั้งที่ระบบรีสตาร์ต</p></section>
   </div>
  </div>
</main>
```

- Layout: single column; `.cols` is inert below 960px, so the same markup is a flat
  stack on a phone and a two-column grid on desktop — **no desktop-only content and
  no duplicated markup** (Maya: "anything worth hiding on a phone is not worth
  rendering on a desktop").
- **Above the fold on a 375×667 phone:** verdict (≈78px) + needs-you (≈88px) +
  two tile rows (≈180px) + the gauge panel's first three lines. The verdict, the
  needs-you line and the gauge number all land in the first screenful — the entire
  answer to Flow A, with zero taps, which is this screen's success condition.
- Tiles: 2-up, with "Last webhook event" `.wide`. Relative time is the `<b>` value,
  absolute is the `<small>` — Maya's relative-primary rule.
- Recent errors, three states: empty → `.empty.ok` + the "clears on every restart"
  line in `.meta` (Maya: not decoration — the difference between a dashboard the
  owner trusts and one that lies after a crash-loop); populated → `.collapse`
  table; at capacity → the same table preceded by a `.note` carrying "Showing the
  latest 200…".
- Per-panel failure → §3.9 unavailable variant, and the verdict degrades to
  `verdict warn` naming that panel.
- **Desktop ≥960px:** 4-up tiles; `.cols` splits Quota+Scheduler | Storage+Errors.
  **Tablet 600–959px:** 3-up tiles, single column, tables stay tables.

### Screen 2 — Users (`GET /users`) — priority #2

Order: flash → **Waiting for approval** → Active → Invite.

- **Pending card** (`.card`): `<h3>` name at 18px (or the id when nameless),
  `.mono` id line, `.meta` "ขอเข้าใช้เมื่อ 12 นาทีที่แล้ว" — the waiting time is the
  urgency signal and it stays `--ink3`; making it red would make every request an
  alarm. Then `.actions` with `<details class="confirm">` (Approve, teal) and
  `<details class="confirm stop">` (Block, outlined stop). Collapsed they sit
  side-by-side; opening one takes the full width and pushes the other below (§3.12).
- **Pending empty** — the state that renders most days: `.empty.ok` with
  "✅ ตอนนี้ไม่มีใครรอการอนุมัติ" and the CTA sentence pointing **down** at the
  invite box. Teal tint, because empty is the good state here.
- **Active:** `<table class="collapse">` ≥600px (Name · Chat ID · Last log · Streak
  · Digest · Language · action), cards below. Owner's row: name cell reads
  "คุณ (เจ้าของบอท)" and **the action cell is rendered empty** — no Block control,
  ever (UX §8 Q7). Stale "last log 6 days ago" stays `--ink3`: worth seeing, not an
  alarm.
- **Invite:** `.panel` with a `.meta` help line, `<label>` + input (§3.15), and a
  `.btn` submit. Last on the page.
- Error state: `.flash stop` in the flash slot + `aria-invalid` on the re-populated
  field.

### Screen 3 — Invite confirm interstitial

`.wrap.decide`, no nav. `<h1>` "เพิ่มผู้ใช้คนนี้?" → `.id` block (24px mono,
letter-spaced, wrapping) → body paragraph → `.actions`:
`<button class="btn">ยืนยัน เพิ่มผู้ใช้</button>` + `<a class="cancel">ยกเลิก</a>`.
The `.id` block is the whole point of the page and gets the vertical space.

### Screen 4 — Quota & digest (`GET /quota`) — priority #3

Blocks in diagnostic order, each a `.panel`:

1. **Gauge** — §3.7, full width, tier-driven.
2. **By month** — a `.note` first (the "mode is currently {mode}, config changes
   aren't audited, check this first" line), then a **plain `<table>` — no
   `.collapse`, at any width**:

```html
<tr class="now"><th scope="row">2026-08</th>
  <td><div class="bar" aria-hidden="true"><i style="width:100%"></i></div></td>
  <td class="num">13050</td><td>← เดือนนี้</td></tr>
<tr><th scope="row">2026-07</th>
  <td><div class="bar" aria-hidden="true"><i style="width:3%"></i></div></td>
  <td class="num">412</td><td></td></tr>
```

   Bar widths are scaled to the **12-month maximum**, not to the cap — the block's
   job is "is this month anomalous?", which is a comparison between rows. Current
   month gets `.now` (`--s2` fill) **plus the literal "← เดือนนี้" text**, so the
   marker is not colour-only. Numbers are `.num` (tabular, right-aligned) so the
   digits line up column-wise — with the bars, that is two independent readings of
   the same shape, one of which works in greyscale and in a screen reader.
3. **This month by user** — `.collapse` table, sorted desc, columns User · Pushes ·
   Share% · bar. Row 1 is the culprit by construction. Trailing `See what they
   logged →` link to `/activity`.
4. **Caps & thresholds** — `.dl`. "⚠️ แจ้งเตือนแล้วเดือนนี้" as `.tag word warn`;
   "— ยังไม่แจ้งเตือน" as plain `--ink3` text.
5. **Daily digest** — `.meta` schedule line, `.collapse` roster table, then either
   the `.btn` "ส่งสรุปรายวันตอนนี้" + `.meta` help line, **or**, when quota is
   stopped, `.state stop` carrying "ถึงเพดานพุชแล้ว…" *instead of* the button. The
   button is **replaced, never disabled** (Maya §5).

### Screen 5 — Digest-run confirm interstitial

`.wrap.decide`, no nav. `<h1>` → `.dl` blast radius (goes to N · uses ~N pushes ·
this month used/cap) → `.state stop` "🛑 ส่งแล้วยกเลิกไม่ได้ …" → a plain `<p>` for
"อาจใช้เวลาสักครู่ อย่าปิดหรือรีเฟรชหน้านี้" → `.actions`.

**That duration line is the entire loading state for this action** and is styled as
body copy, not as fine print: 16px `--ink`, its own paragraph, directly above the
button. There is no spinner because there is no JS; pre-announcing the wait is the
only honest mitigation available (Maya Flow D).

**Token-spent / replay page:** same `.decide` shell, `.flash mute` carrying
"ส่งไปแล้วเมื่อ 06:22 ระบบไม่ได้ส่งซ้ำ", plus a link back to `/quota`.

**The confirm CTA stays teal, not red.** See §11 E3.

### Screen 6 — Audit (`GET /audit?page=N`)

`.collapse` table, five columns (When · Who · What · Detail · Source). `Detail`
uses `.mono` for ids and carries the full value in `title` when truncated. `Source`
is a `.tag`. Phone cards: `td.head` on When, `td.full` on Detail. `.pager` below.
Empty → `.empty.mute` "ยังไม่มีการเปลี่ยนแปลงที่บันทึกไว้", **no CTA** and the pager
suppressed. Whole-page read failure → heading + `.empty.warn`, pager suppressed.

### Screen 7 — Activity (`GET /activity`)

`.note` with ℹ️ **rendered above the table, always** — AC24 is a promise the owner
should be able to see. `.collapse` table (When · User · Habit · Value · Source). A
`journal` row's Value is an em-dash; the note directly above is what makes that
read as *withheld*, not *missing*, so the two are a single visual unit — do not
separate them with a heading or a rule. No pager in v1.

### Screen 8 — Config (`GET /config`) *[COULD]*

`<h1>` + `.note` 🔒 secrets line, then one `<h2>[section]</h2>` + `.dl` per config
section, in `config.toml` order. Values in `.mono`. Redacted → `••••••` **plus the
word "(ซ่อนไว้)"** in `--ink3`; unset → "(ยังไม่ได้ตั้งค่า)". Two different strings,
because "is my token configured?" is the question this page is opened to answer.

### Screen 9 — 403 Not authorized

```html
<!doctype html><html lang="th"><meta charset="utf-8"><title>403</title>
<p>ไม่มีสิทธิ์เข้าถึง · Not authorized</p>
```

**This page gets no stylesheet, no shell, no brand, no nav, no footer, no version,
no link, no favicon — nothing.** ~150 bytes, byte-identical on every request
regardless of config. It must **not** emit the `<style>` block: an 8KB stylesheet
with product-specific class names is itself a fingerprint, and this is the one page
a hostile stranger might see if the port is ever mis-Funneled. It is a system page,
not a product page. The string is the hardcoded bilingual constant Maya flagged
(UX Screen 9) — deliberately not an `i18n.t()` lookup.

### Screen 10 — 500 Something broke

Brand header, **no nav** (the nav computes a pending count from the DB; if the DB is
what raised, rendering nav in the 500 handler re-raises inside the error path).
`.wrap.decide`, `<h1>`, the two-sentence body, and a plain `→ สถานะ` link home.
Full stylesheet (post-gate, owner-only). No traceback, ever.

---

## 6. Accessibility — visual side

- **Contrast, AA, pre-checked.** Every pair in §2 was computed, not eyeballed, with
  the rich menu's own functions. Body text 17.76:1; secondary 8.53:1; the muted tier
  `--ink3` still clears 4.5:1 on *all three* backgrounds it can sit on (5.48 white /
  5.26 ground / 5.06 `--s2`) — there is no "decorative grey" in this system that
  fails as text. Tier text on its own tint: 4.75 / 4.75 / 5.53. White on tier fills:
  5.47 / 5.43 / 6.54. Nothing ships below 4.5:1 for text or 3:1 for a boundary that
  carries meaning.
- **Focus indicator:** one universal rule, `2px solid var(--ink)` + 2px offset,
  17.02:1 against the ground. `outline:none` appears exactly once in the stylesheet,
  inside `:focus:not(:focus-visible)`, and if that selector is unsupported the rule
  is dropped and the ring stays permanently visible.
- **Colour independence — the design works in greyscale.** Every state carries an
  emoji **and** a word before colour is applied: verdict (✅/⚠️/🛑 + text), gauge
  ("ปกติ" / "ใกล้ถึงเพดานแล้ว" / "ถึงเพดานแล้ว" + the number), tags (the level word
  *is* the content), the nav pending signal (the count is in the label), the current
  month row ("← เดือนนี้"), the invalid field (message + `aria-describedby`), the
  dead scheduler job (a word in a tag). The three tier tints also differ in
  lightness, so they remain distinguishable as *different* even if not as
  *meaningful* without colour.
- **Touch targets:** 44px minimum on every `<a>` in nav, every `.btn`, every
  `<summary>`, `.cancel`, `.more`, and every pager control; the needs-you block is
  64px and full width; pager controls go full-width stacked below 600px — the two
  places Maya flagged as most at risk.
- **Text sizing / 200% zoom:** no fixed heights anywhere (only `min-height`), no
  `overflow:hidden` on a text container, all spacing in px on an 8pt grid, prose
  capped at `68ch`. At 200% zoom the phone layout is what a desktop shows, and it
  works, because it is the same markup.
- **Thai:** `line-height:1.6` body / `1.35` headings, no uppercase, no tracking on
  localized text, nothing below 14px. `<html lang>` must be the resolved render
  language or a screen reader pronounces Thai with an English voice.
- **Motion:** none exists (§1), so `prefers-reduced-motion` has nothing to reduce.
- **No JS anywhere**, therefore: no focus traps, no ARIA widget patterns, no
  keyboard handlers, no live regions beyond the native `role="status"` flash. Tab
  order is document order. This is a large part of why the portal can hold AA with
  a 8KB stylesheet and no test harness for interaction.

---

## 7. Dark mode

**Light only, and that is a decision, not an omission** — see §1 for the four
reasons. There is no `@media (prefers-color-scheme: dark)` block in §8 and no dark
column in §2's colour table.

If it is ever wanted, the whole cost is one media block redefining the 13 colour
tokens; every component reads them indirectly, so nothing else in this document
changes. The one component-level note to carry over: this design has **no shadows**,
so the usual "shadows read weaker in dark mode" adjustment does not apply — borders
are already doing all the work.

---

## 8. Tokens as code — the entire stylesheet

**8,160 bytes** (7.97 KB) — inside the ~8KB budget. 108 rules. Paste verbatim into a
single `<style>` element in `<head>`, on every page **except the 403**.

```css
/* portal.css - the whole admin portal. Light only. No JS, no motion. */
:root{
--bg:#FAFAFB;--card:#fff;--s2:#F4F6F8;--line:#E3E7EC;--line2:#838B96;
--ink:#16181D;--ink2:#454D5A;--ink3:#606A78;
--teal:#0F766E;--teal-d:#0C615A;--teal-t:#E7F1F0;
--warn:#9A5B00;--warn-t:#F5EFE6;--stop:#B3261E;--stop-t:#F7E9E8;
--tier:var(--teal);--tier-t:var(--teal-t);--r1:6px;--r2:10px;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans Thai","Leelawadee UI",Thonburi,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.ok{--tier:var(--teal);--tier-t:var(--teal-t)}
.warn{--tier:var(--warn);--tier-t:var(--warn-t)}
.stop{--tier:var(--stop);--tier-t:var(--stop-t)}
.mute{--tier:var(--ink3);--tier-t:var(--s2)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 var(--sans);-webkit-text-size-adjust:100%}
h1,h2,h3{line-height:1.35;margin:0 0 8px;font-weight:600}
h1{font-size:24px}h2{font-size:18px}
p{margin:0 0 8px;max-width:68ch}
a{color:var(--teal)}
:focus{outline:2px solid var(--ink);outline-offset:2px}
:focus:not(:focus-visible){outline:none}
.vh{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.vh:focus{position:static;width:auto;height:auto;clip:auto}
.wrap{max-width:960px;margin:0 auto;padding:16px}
.decide{max-width:560px;margin-top:32px}
header{background:var(--card);border-bottom:1px solid var(--line)}
.brand{font-size:14px;font-weight:600;color:var(--ink2);margin:0 0 8px}
nav{display:flex;flex-wrap:wrap;gap:8px}
nav a{display:flex;align-items:center;min-height:44px;padding:8px 14px;border:1px solid var(--line);border-radius:var(--r1);background:var(--card);color:var(--ink2);text-decoration:none;font-weight:500}
nav a:hover{border-color:var(--line2);color:var(--ink)}
nav a.pending{border-color:var(--teal);background:var(--teal-t);color:var(--teal);font-weight:600}
nav a[aria-current=page]{background:var(--teal);border-color:var(--teal);color:#fff}
footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--line);color:var(--ink3);font-size:14px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:16px;margin:0 0 16px}
.panel>h2{margin:0 0 12px}
.verdict{background:var(--tier-t);border:1px solid var(--line);border-radius:var(--r2);padding:16px;margin:0 0 16px;font-size:20px;font-weight:600;line-height:1.45}
.verdict a{color:var(--ink)}
.verdict ul{margin:8px 0 0;padding-left:20px;font-size:16px;font-weight:400}
.flash{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--tier);border-radius:var(--r1);padding:12px 16px;margin:0 0 16px}
.needs{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;min-height:64px;background:var(--card);border:1px solid var(--teal);border-radius:var(--r2);padding:16px;margin:0 0 16px;color:var(--ink);font-weight:600;text-decoration:none}
.needs:hover{background:var(--teal-t)}
.needs b{color:var(--teal);white-space:nowrap}
.tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:0 0 16px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:12px;font-size:14px;color:var(--ink3)}
.tile b{display:block;margin-top:2px;font-size:18px;color:var(--ink)}
.tile small{display:block;font-size:14px;color:var(--ink3)}
.wide{grid-column:1/-1}
.bar{height:10px;margin:8px 0;background:var(--line);border-radius:999px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--tier)}
.gauge b{font-size:18px;font-variant-numeric:tabular-nums}
.state{display:flex;flex-wrap:wrap;gap:8px;justify-content:space-between;background:var(--tier-t);border-radius:var(--r1);padding:8px 12px;margin:8px 0 0}
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}
th{background:var(--s2);color:var(--ink2);font-size:14px;font-weight:600;white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.now{background:var(--s2)}
td .bar{width:100%;min-width:56px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r2);padding:16px;margin:0 0 12px}
.card h3{margin:0 0 4px;font-size:18px}
.meta{color:var(--ink3);font-size:14px;margin:0 0 12px}
.actions{display:flex;flex-wrap:wrap;gap:8px}
.btn,.confirm>summary{display:flex;align-items:center;justify-content:center;gap:8px;min-height:44px;padding:10px 18px;border:1px solid var(--tier);border-radius:var(--r1);font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}
.btn{display:inline-flex;background:var(--tier);color:#fff}
.btn:hover{background:var(--teal-d);border-color:var(--teal-d)}
.btn.quiet{background:var(--card);color:var(--tier)}
.btn.quiet:hover{background:var(--tier-t)}
.cancel{display:inline-flex;align-items:center;min-height:44px;padding:0 12px;color:var(--ink2)}
details{margin:0}
summary{cursor:pointer;list-style:none}
summary::-webkit-details-marker{display:none}
summary::after{content:" \25B8"}
details[open]>summary::after{content:" \25BE"}
.confirm{flex:1 1 auto;min-width:150px}
.confirm[open]{flex-basis:100%}
.confirm>summary{background:var(--card);color:var(--tier)}
.confirm[open]>summary{border-radius:var(--r1) var(--r1) 0 0;background:var(--tier-t)}
.confirm>div{padding:12px;border:1px solid var(--tier);border-top:0;border-radius:0 0 var(--r2) var(--r2);background:var(--tier-t)}
.more>summary{display:inline-flex;align-items:center;min-height:44px;color:var(--teal);font-weight:500}
label{display:block;margin:0 0 4px;font-size:14px;font-weight:500;color:var(--ink2)}
input{width:100%;max-width:420px;min-height:44px;padding:10px 12px;border:1px solid var(--line2);border-radius:var(--r1);background:var(--card);color:var(--ink);font:16px/1.5 var(--mono)}
input:focus{border-color:var(--teal)}
input[aria-invalid=true]{border:2px solid var(--stop)}
.tag{display:inline-block;padding:2px 8px;border:1px solid var(--line);border-radius:999px;background:var(--s2);color:var(--ink2);font:12px/1.5 var(--mono);white-space:nowrap}
.tag.word{font:14px/1.5 var(--sans)}
.tag.warn{background:var(--warn-t);border-color:var(--warn);color:var(--warn)}
.tag.stop{background:var(--stop-t);border-color:var(--stop);color:var(--stop)}
.mono{font-family:var(--mono);font-size:14px;color:var(--ink2);word-break:break-all}
.id{display:block;padding:12px;margin:0 0 16px;border:1px solid var(--line);border-radius:var(--r1);background:var(--s2);font-family:var(--mono);font-size:24px;line-height:1.5;letter-spacing:.06em;word-break:break-all}
.note{background:var(--s2);border:1px solid var(--line);border-radius:var(--r1);padding:12px;margin:0 0 12px;color:var(--ink2);font-size:14px}
.empty{background:var(--tier-t);border-radius:var(--r1);padding:12px;color:var(--ink2)}
.pager{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:center;margin:16px 0}
.pager span{padding:0 8px;color:var(--ink2);font-size:14px}
.dl{margin:0}
.dl div{display:flex;gap:12px;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--line)}
.dl dt{color:var(--ink2);font-size:14px}
.dl dd{margin:0;text-align:right;font-weight:500}
@media(min-width:600px){
.wrap{padding:24px}
.tiles{grid-template-columns:repeat(3,1fr)}
tbody tr:hover{background:var(--s2)}}
@media(min-width:960px){
.tiles{grid-template-columns:repeat(4,1fr)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}
@media(max-width:599px){
.collapse thead{display:none}
.collapse tr{display:block;margin:0 0 8px;padding:8px 12px;background:var(--card);border:1px solid var(--line);border-radius:var(--r2)}
.collapse td{display:flex;gap:8px;justify-content:space-between;padding:4px 0;border:0}
.collapse td::before{content:attr(data-label);flex:0 0 auto;color:var(--ink3);font-size:14px}
.collapse td.head{display:block;font-size:18px;font-weight:600}
.collapse td.head::before{display:none}
.collapse td.full,.collapse td.full::before{display:block}
.dl div{display:block}
.dl dd{text-align:left}
.pager{flex-direction:column;align-items:stretch}
.pager .btn{width:100%}}
```

---

## 9. Hand-off to Irine & Luna

### 9.1 For Irine (stack)

- **No new dependency, no build step, no asset pipeline, no CSS file on disk.** The
  stylesheet above is a Python string constant in `core/portal/layout.py`, emitted
  inside one `<style>` element. There is nothing to compile, minify, hash, or serve.
- **No font loading of any kind.** No `@font-face`, no `<link rel=preconnect>`, no
  Google Fonts. The page renders identically with the machine offline from the
  public internet — a hard requirement for a `tailscale serve` surface.
- **No JS.** Nothing in this design needs a script, and Maya forbids one. Do not
  pick a library, a CDN, or a "tiny" helper.
- **No static route needed.** Inline was chosen over a `GET /portal.css` route
  deliberately: at ~8KB × maybe a dozen page views a day the transfer cost is
  irrelevant, and inlining removes a route, a cache-invalidation question, and a
  failure mode where the CSS 403s while the HTML doesn't. If you *do* want a
  cached route later, nothing in this document changes except where the string is
  written — but then the 403 page must still not reference it.
- **The `<style>` block is per-page overhead** on every response except 403. If a
  future page count or a wall-display `?refresh=60` use makes that matter, the
  route split above is the escape hatch.

### 9.2 For Luna — how `layout.py` should emit this

**Markup contracts. Each of these is a real dependency of the stylesheet; breaking
one silently degrades a screen rather than erroring.**

1. `<meta name="viewport" content="width=device-width,initial-scale=1">` on every
   page. Without it none of Maya's phone layouts exist.
2. `<html lang="th|en">` = the resolved render language (AC31).
3. Shell order: `header > .wrap` (brand + nav) · `main.wrap#main` · `footer > .wrap`.
4. Nav: one `<a>` per destination; `aria-current="page"` on the current one;
   `class="pending"` on Users when the count ≥ 1 — computed fail-open, plain chip on
   any exception (UX §8 Q1).
5. **Every `<td>` in a `.collapse` table carries `data-label="<column heading>"`**,
   localized to match its `<th>`. Column heads are `<th scope="col">`; row-key cells
   in the month table are `<th scope="row">`.
6. `<details class="confirm">` contains **exactly** `<summary>` + one `<div>`.
7. Bars: `<div class="bar" aria-hidden="true"><i style="width:{pct}%"></i></div>`.
   **This is the only inline style in the portal.** `{pct}` must be a
   server-computed number clamped to 0–100 and formatted with `%.1f` — it is
   interpolated into an attribute, so it must never be a raw string from anywhere.
8. Flash: `<div id="flash" class="flash ok|stop|mute" role="status" tabindex="-1">`,
   directly below nav; redirects carry the `#flash` fragment.
9. Buttons are `<button class="btn">`; **never `<input type="submit">`** (the bare
   `input` selector styles text fields).
10. `.dl` requires the `<div>` wrapper per row: `<dl class="dl"><div><dt>…</dt><dd>…</dd></div></dl>`.
    `.state` is a `<div>`, not a `<p>` (a `<p>` inherits the 68ch prose cap and
    pulls the trailing link in off the right edge).
11. Tier classes `ok|warn|stop|mute` go on the **component root** and rebind
    `--tier`/`--tier-t` for everything inside. Never hard-code a hex in markup.
12. **The 403 handler must not use the shell, the stylesheet, or the i18n catalog.**
    It returns the fixed ~150-byte bilingual document in §5 Screen 9.
13. The 500 handler renders brand + copy + link, **no nav** (nav touches the DB).
14. Escape every interpolated value (`html.escape`, `quote=True` for attributes) —
    display names, chat ids, `data-label`, logger names and log messages all reach
    the page from outside.

**Suggested build order** (matches Maya's visual priority, so the highest-value
screen is verifiable first):

1. `layout.py`: the `<style>` constant, `page(...)` shell, nav + pending, flash,
   footer, and the escaping helpers. Everything else depends on this.
2. Status: verdict → needs-you → tiles → gauge. Stop and look at it at 375px before
   going further; this is the only page the owner may ever see.
3. Users: pending `.card` + the `.confirm` disclosure, both states.
4. Quota: gauge reuse, month table, by-user table, digest block + the two
   interstitial pages.
5. Audit / Activity / Config: pure `.collapse` table + `.dl` work, no new visuals.
6. 403 / 500.

**Things I have deliberately not left to your judgement**, because they are
contracts rather than styling: the 44px minimums, `aria-hidden` on every bar,
`data-label` on every collapsible cell, the 16px input font-size, the 1.6/1.35
line-heights, `<summary>` staying a `<summary>`, and the bare 403.

**If you need a component that isn't here, or a token contradicts itself, stop and
send it back** (via Archi) rather than inventing a value — a single invented hex is
how a design language starts drifting, and this one is shared with a shipped asset.

---

## 10. Coverage check against Maya's §9

Every component Maya listed, and where it lives:

| Maya §9 asks for | Spec'd in | Status |
|---|---|---|
| Verdict banner, 3 states | §3.3 | ✅ |
| Needs-you banner (whole-block tap target, conditional) | §3.4 | ✅ |
| Flash banner, 2 states, distinct from verdict | §3.5 | ✅ (fill vs rule vs type size) |
| Stat tile, 2/3/4-up | §3.6 | ✅ |
| Quota gauge, 3 states, bar decorative | §3.7 | ✅ |
| Horizontal bar-in-row, one primitive two uses | §3.8 | ✅ (same `.bar`) |
| Panel + "unavailable" variant keeping its heading | §3.9 | ✅ |
| Data table, `th scope=col`, `td[data-label]` collapse | §3.10 | ✅ (+ month-table exception) |
| Card list | §3.11 | ✅ |
| Inline confirm disclosure, `<summary>` stays `<summary>` | §3.12 | ✅ |
| Interstitial page, nav-less, two instances | §3.22, §5 Screens 3 & 5 | ✅ |
| Pager, full-width stacked on phone | §3.21 | ✅ |
| Definition list (Config + blast radius) | §3.20 | ✅ |
| Monospace id at three sizes | §3.17 | ✅ |
| Status tag (levels + sources), not colour-only | §3.16 | ✅ |
| Nav wraps, no sidebar, no hamburger | §3.2 | ✅ |
| Nav pending signal | §3.2 | ✅ (see E1) |

**Wireframe elements I had to interpret** (Maya draws them; her component list
doesn't name them):

1. **`[ 7 backups ▸ ]`** (Screen 1, Storage) — a *second kind* of `<details>` that
   is an expander, not a confirm. Spec'd as `.more` (§3.13), visually distinct from
   `.confirm` so the two are never confused.
2. **Recent-errors rows** (Screen 1) — spec'd as a `.collapse` table rather than a
   bespoke log-row component (§3.23). Zero new CSS, correct table semantics, and it
   collapses to the exact stacked shape her wireframe draws.
3. **Scheduler / Storage / Caps panels** — spec'd as `.dl` (§3.20). Her wireframes
   show aligned label→value pairs; a definition list is the honest markup.
4. **Dead scheduler job** (`next_run_time is None`) — her verdict table requires
   this state, no wireframe draws the row. Spec'd as `.tag word stop` in the `dd`.
5. **`[copy]` next to a chat id** (Screen 2) — **not rendered in v1.** It cannot work
   without JS, and Maya §5 permits JS only as an *additive* enhancement. Ids are
   `user-select`-able and `word-break`-wrapped so a long-press selects cleanly. If
   the owner asks for it later, it is a `<button hidden>` progressively revealed by
   a script — a new decision, not a silent one.
6. **Pager "Newer" on page 1** — not rendered at all (Maya §5: a control whose only
   outcome is failure is not rendered). The row stays centred.
7. **"← this month" marker** — `.now` row tint **plus** the literal text, so the
   marker is never colour-only.
8. **Verdict multi-cause list** — `<ul>` at 16px/400 inside the 20px/600 banner.
9. **Arrow links** (`Review →`, `More →`, `See what they logged →`) — plain teal
   `<a>`; only the one inside `.needs` gets `<b>` + `nowrap`. No new component.
10. **Owner's row action cell** — rendered as an empty `<td>`, not a disabled
    control (UX §8 Q7).

---

## 11. Escalations

- **E1 — Nav pending badge vs. the i18n key. (Maya / Archi — low stakes, has a
  working default.)** Maya's `portal_nav_users` is a single string, `ผู้ใช้ ({n})` /
  `Users ({n})`, so the count cannot be lifted into a separate pill without
  splitting the key. I have therefore styled the *whole chip* (`nav a.pending`:
  teal border + tint + weight) and left the count inside the label, which works with
  her key exactly as written and is greyscale-safe. If a pill badge is preferred,
  the key splits into label + count and I supply a two-line `.badge` rule. **Default
  if unanswered: ship as designed.**

- **E2 — Thai font coverage on a machine with no Thai font. (User — needs an
  answer only if the owner browses from an unusual desktop.)** The stack in §2 has
  no webfont by instruction, and it covers iOS, macOS, Windows 8+, Android and any
  Linux with `fonts-noto`. It does **not** cover a bare Linux/BSD desktop with no
  Thai font installed — that renders tofu boxes, the exact failure the repo
  vendored `assets/fonts/NotoSansThai-Regular.ttf` to fix for chart PNGs. Two
  options if that machine exists: (a) `apt install fonts-noto-thai` on it, zero code
  change; (b) serve the already-vendored TTF from the portal's own origin via a
  `GET /f.ttf` route + one `@font-face` — **still zero external requests** and still
  offline-clean, at the cost of one route and ~180KB on first load. **Default if
  unanswered: (a) — the stack as designed. The owner reads from a phone and a
  laptop, both covered.**

- **E3 — The digest-confirm CTA is teal, not red. (Archi — a call I made; flag it
  so it is a decision and not an oversight.)** Screen 5's `Yes, send now` is the
  only irreversible action in the portal, and the conventional move is a red button.
  I kept it teal because (1) red is already spoken for in this system — it means
  *the quota has stopped* and *this panel failed* — and a red button would make the
  page's own tier vocabulary ambiguous; (2) the blast radius is already stated three
  ways above the button (the `.dl` numbers, the 🛑 irreversibility strip, the
  duration warning), which is stronger than button colour; (3) the owner is on that
  page *because they chose to be*, having already passed a deliberate interstitial.
  **If Archi prefers a red CTA**, it is `<button class="btn stop">` plus one new
  `--stop-d` hover shade — say the word and I will add the token rather than have
  Luna inline it.

- **E4 — `summary::after` caret and screen-reader verbosity. (Low stakes, noted for
  Vera.)** The `▸`/`▾` glyphs are CSS generated content, kept because Maya's
  wireframes draw them and sighted users need the affordance once the native marker
  is hidden. Some screen readers announce generated content, so a `<summary>` may
  read as "Approve, right-pointing triangle, collapsed, button" — redundant, not
  wrong. If Vera judges it noisy, deleting the two `::after` rules is a safe,
  self-contained change (the native expanded/collapsed announcement is unaffected).

- **E5 — Nothing here conflicts with the rich menu, but one divergence is
  deliberate and worth stating:** the portal uses emoji (§4) where
  `assets/richmenu/README.md` bans them. That ban exists because PIL needs a
  colour-emoji font installed on the *build* machine to rasterize them into a PNG.
  HTML has no such constraint — the glyphs render from the reader's own OS. The
  emoji vocabulary is also already in the i18n catalog Maya's copy uses, so removing
  them would mean editing her strings. **No action needed; recorded so nobody reads
  it as a drift.**

---

## 12. Revision log

*(First delivery — no revisions yet.)*
