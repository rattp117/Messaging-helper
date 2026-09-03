# Rich menu image — deployment asset

`richmenu.png` is the tappable command-menu image `LineChannel.
register_rich_menu()` uploads at startup (`channels/line.py`, Module A;
`config.toml`'s `[line].rich_menu_image`, default
`assets/richmenu/richmenu.png`). LINE's rich menu is a single flat image
plus a list of pixel-rect tap areas defined in the `POST /v2/bot/richmenu`
call — the PNG here is the **visual** half of that; the areas and the
actions they send are code (Module A).

## Status: v2 — actions on top, navigation below

**v1** (which replaced the SPEC-LINE.md §9 OQ3 grey-and-blue placeholder)
drew six interchangeable cells, because all six *were* interchangeable:
every one sent a slash command as an ordinary text message.

**v2** (rich-menu rewire, 2026-09-03 request) is no longer six of a kind.
The top row leads with two **direct-log** cells — postback actions that
write a log row on a single tap — for thumb reach: the two things people
open this menu to do sit where a one-handed grip lands first. `/log` and
`/guide` lost their cells (both are still typable commands). So the menu
now holds **two kinds** of button and the artwork says so; see "The six
cells" below.

Rich-menu registration remains fail-open (`register_rich_menu()` logs and
continues on any failure, R-A10), so a missing or rejected image still
means "no menu", never a crash.

> **Editing note** — two tests parse this file. Keep them in mind before
> reformatting:
>
> - `tests/test_deploy_line.py::test_richmenu_readme_documents_the_current_
>   design_tokens_cells_and_regeneration` asserts the literal headings
>   `## Design tokens`, `## The six cells` and `## Regenerating it` are
>   present, that the string `generate_richmenu.py` appears, and that every
>   **message**-action cell's command appears somewhere in backticks.
> - `tests/test_line_d_gaps.py::test_richmenu_button_commands_are_real_
>   dispatchable_commands` also cross-checks each message cell's command
>   against this file in backticks (a format-tolerant substring search, not
>   a table-shape regex).
>
> Both derive the command list from `_default_rich_menu_payload()` itself,
> so they stay in sync automatically if the button set changes — but the
> backticked `` `/habits` ``-style spellings and those three headings have
> to survive any rewrite.

## Design tokens

**Style:** Modern & Clean — near-white page, hairline borders, one accent,
flat. No gradients, no shadows, no emoji.

### Colour

| Token | Value | Use | Contrast |
|---|---|---|---|
| `BG` | `#FAFAFB` | Page background | — |
| `CARD` | `#FFFFFF` | Nav cell surface; **and the ink on an action cell** | 1.04:1 on `BG` |
| `BORDER` | `#E3E7EC` | Hairline separator (nav cells only) | 1.24:1 on `CARD` |
| `INK` | `#16181D` | Thai label on a nav cell | **17.76:1** on `CARD` |
| `ACCENT` | `#0F766E` | Nav icons + sublabel; **and the action cell's card fill** | **5.47:1** on `CARD` |
| `ACCENT_MID` | `#63A6A1` | Heatmap icon, mid cell | derived — `tint(ACCENT, 0.35)` |
| `ACCENT_SOFT` | `#D4E6E5` | Heatmap icon, coldest cell | derived — `tint(ACCENT, 0.82)` |

Still **one accent**, and v2 adds no new colour token — it just uses the
one it has in both directions: hairline-weight ink on a white nav card,
and the whole card fill on an action card. A deep teal reads calm and
clinical rather than shouty, which suits a health/habit product, and at
`#0F766E` it clears **4.5:1 AA for text** on white — a lighter teal (e.g.
`#0D9488`) would not, and this accent is used for real text, not just
decoration. Reversed (white on `#0F766E`) it is the same 5.47:1, which is
why the filled cards can carry their labels in plain white rather than
needing a second, lighter accent. The two tints are computed from `ACCENT`
by `_tint()`, never hand-picked, so the palette stays a single hue by
construction.

`NEEDLE_SOUTH` (v1's third tint) is **gone** along with the `/guide`
compass icon that was its only user.

`generate_richmenu.py` re-checks every pair above on each run and refuses
to write the PNG if one drops below its floor (4.5:1 text, 3:1 graphics).

### Type

All text — Thai *and* Latin — is set in the repo's bundled
`assets/fonts/NotoSansThai-Regular.ttf` (the same face `core/fonts.py` uses
for chart PNGs, ROADMAP v1.9.0). It covers both scripts, so the image needs
exactly one font file and renders identically on any machine. A system
default font renders Thai as tofu boxes — the exact issue that font was
vendored to fix. **Do not swap it for a system font.**

| Token | Value | Rendered on a phone* |
|---|---|---|
| `LABEL_SIZE` | 78 px | ~12 pt |
| `SUB_SIZE` | 54 px | ~8.4 pt |
| `SUB_BOLDEN` | 1.2 px text stroke | faux-bold (the face has no bold cut) |

Renamed in v2 (`CMD_SIZE`/`CMD_BOLDEN`/`CMD_BASELINE` → `SUB_*`): the small
line under the label is no longer always a command — on an action cell it
is an English gloss, because no typed command is equivalent to that button.

\* LINE renders the full 2500 px width at roughly a phone width (~390 pt),
i.e. **~0.156×**. Every size in this asset is chosen against that number,
not against how it looks at full resolution.

### Geometry (8 pt grid)

| Token | Value | Note |
|---|---|---|
| Canvas | 2500 × 1686 | LINE "large"; matches `_default_rich_menu_payload()` |
| Grid | 3 × 2, cells 833 × 843 | `w // 3` × `h // 2`, exactly as the code cuts them |
| `CARD_INSET` | 32 px | → a 64 px gutter between neighbouring cards |
| `CARD_RADIUS` | 32 px | |
| `CARD_BORDER` | 6 px | ~0.9 pt rendered: a true hairline that still survives |
| `ICON_R` | 100 px | every icon is a 200 × 200 box |
| `STROKE` | 18 px | one stroke weight across all six icons (~2.8 pt) |
| `ICON_CY` / `LABEL_BASELINE` / `SUB_BASELINE` | 310 / 546 / 636 px from cell top | fixed baselines, so all three cells in a row align — **action and nav cells share them**, so the two treatments still line up |
| `SS` | 2 | whole canvas drawn at 2× and LANCZOS-downsampled (PIL has no antialiased primitives) |

### The two cell treatments

| | ACTION cell | NAV cell |
|---|---|---|
| What a tap does | writes a log row (postback) | sends a slash command (message) |
| Card | filled `ACCENT` | `CARD` + `BORDER` hairline |
| Icon + sublabel | `CARD` (white) | `ACCENT` |
| Big label | `CARD` (white) | `INK` |
| Sublabel content | English gloss of the amount | the literal slash command |
| Card edge | carried by fill-vs-page (5.25:1) | carried by the hairline |

This is v1's own *"solid = the action"* rationale promoted from the icon to
the whole card. In v1 the `/log` cell was the single solid icon in the menu
because logging was the one thing people opened the menu for; in v2 the
action **is** the card — tapping it logs, it doesn't open a keyboard one
tap deeper — so the card carries the treatment. Two filled teal blocks on
the top row against four white cards read as primaries at thumbnail size,
which is the only size that matters (see the ~0.156× note above).

## The six cells

Left-to-right, top-to-bottom — the order `_default_rich_menu_payload()`
builds its areas in. Cells 1–2 are **postback** actions; cells 3–6 are
**message** actions, delivered as an ordinary inbound text message that
routes through the existing command dispatch unchanged (R-A10).

| Cell | Tap area (x, y, w, h) | Action | Payload | Thai label | Sublabel | Icon |
|---|---|---|---|---|---|---|
| 1 | 0, 0, 833, 843 | postback | `log:water:250` | น้ำ 250 มล. | 250 ml water | solid droplet |
| 2 | 833, 0, 833, 843 | postback | `log:stretch:10` | ยืดเส้น 10 นาที | 10 min stretch | figure, arms raised |
| 3 | 1666, 0, 833, 843 | message | `/habits` | ความคืบหน้า | `/habits` | checklist, top item done |
| 4 | 0, 843, 833, 843 | message | `/heatmap` | ปฏิทิน | `/heatmap` | 4×4 heat grid |
| 5 | 833, 843, 833, 843 | message | `/wrapped` | สรุปภาพรวม | `/wrapped` | rising bar chart |
| 6 | 1666, 843, 833, 843 | message | `/help` | ช่วยเหลือ | `/help` | circled question mark |

Notes on the labels:

- **A nav cell's sublabel is the literal text that tapping it sends**, so
  the artwork can't drift from what the button does.
- **An action cell's label carries the amount it logs**, because that
  amount is the whole point of the button — and it deliberately shows **no
  slash command**, because there is no command you could type that does
  this (`/log` only opens a keyboard). `verify_against_code()` enforces
  that the drawn amount really is the amount the payload sends: a card
  reading "250 มล." over a `log:water:500` payload is the one drift this
  artwork can't survive.
- The two action labels are the app's **own** phrasing, not invented for
  the menu: `core/i18n.py` renders these logs as `น้ำ {n} มล.` and
  `ยืดเส้น {n} นาที` (`undo_description`), and its own clarify prompt tells
  users to type exactly *"น้ำ 500 มล."* / *"ยืดเส้น 10 นาที"*. So the menu
  teaches vocabulary that also works when typed — and the confirmation a
  tap produces reads back in the same words as the button.
- **ปฏิทิน** is the app's own Thai alias for `/heatmap` (`core/i18n.py`,
  `help_heatmap_cmd`); **ความคืบหน้า** matches `/habits`' own gloss in
  `guide_key_commands` ("today's progress").
- Thai is primary and Latin secondary, but both stay visible on every
  cell — bilingual labelling is this edition's whole reason for existing
  (SPEC-LINE.md header).

## Accessibility

- Nav label contrast is **17.76:1** and nav sublabel **5.47:1**; on the
  filled action cards, white ink on `ACCENT` is **5.47:1** — all well past
  AA (4.5:1) for text. Icons clear the 3:1 floor for graphics (WCAG
  1.4.11) in both treatments, and the filled card's own edge against the
  page is **5.25:1**.
- **Colour is never the only signal that a cell is an action.** The filled
  card is reinforced by a different sublabel *kind* (an amount gloss, not
  a slash command) and by the label naming the amount outright — so the
  action/nav distinction survives greyscale, and survives not noticing the
  colour at all.
- **No icon is the sole carrier of meaning** — every cell is labelled in
  words, in two scripts. The heatmap icon's pale tints are texture only;
  the icon's silhouette is carried by its full-strength cells.
- Tap targets are 833 × 843 px ≈ 130 × 132 pt — far past the 44 pt minimum.
- No emoji glyphs anywhere in the image: they render inconsistently across
  platforms and would make the output depend on a colour-emoji font being
  installed wherever the generator runs. (The two postback cells' own
  `displayText` in `line.py` *does* use emoji — but that is chat text LINE
  renders with its own font stack, not this image.)

## Regenerating it

```bash
python assets/richmenu/generate_richmenu.py          # writes richmenu.png here
python assets/richmenu/generate_richmenu.py -o /tmp/preview.png
```

Pillow is the only dependency (already present transitively via
`matplotlib`/`[charts]`; otherwise `pip install pillow`). The script is
deterministic — same source, byte-identical PNG (verified: two consecutive
runs hash identically) — so a regeneration with no edits produces no diff.
Output is a **truecolour** PNG of about **198 KB**, against LINE's 1 MB
ceiling. Quantising this flat art to a 64-colour palette would cut it
substantially, but at ~20% of the budget that saving buys nothing and costs
two things worth more: an indexed PNG is a needless edge case for a
third-party uploader, and a small palette risks banding on the antialiased
icon curves.

Before it draws anything the script **imports the real
`_default_rich_menu_payload()` and asserts its own grid still matches** —
canvas size, cell count, every bound, every cell's action **type**, and
every cell's payload: the command text for a message cell, the callback
`data` for a postback cell (plus a non-empty `displayText`, and the
drawn-amount check described above). Re-cut the tap areas or change an
action in `channels/line.py` and the next run fails loudly instead of
shipping artwork whose buttons point at the wrong thing. It then
re-verifies the contrast table above and refuses to write on a failure.

To change a label, an icon, or the palette, edit the constants at the top of
the script and re-run — everything is a named token, there are no magic
numbers in the drawing code. The cell list itself is a tuple of `Cell`
NamedTuples (`action_type`, `payload`, `label`, `sublabel`, `icon`); the
filled-vs-white treatment is **derived** from `action_type` via
`Cell.is_action`, never stored separately, so a cell's look cannot drift out
of step with what it does.

## Constraints for any future redesign

- PNG, ≤ 1 MB (LINE's limit), 2500 × 1686 or 2500 × 843 (the compact
  half-height layout — not used here). `tests/test_deploy_line.py` asserts
  the dimensions and the size ceiling.
- ≤ 20 tappable areas (LINE's cap) — 6 leaves plenty of headroom.
- Each area's action is either a **message action** whose text is one of
  this bot's own commands (see `core/commands.py` for the full list), or a
  **postback action** whose `data` parses against `core/quicklog.py`'s
  `_LOG_CALLBACK_RE` (`log:<habit_id>:<value>`) and names a habit that
  exists in the base registry — `tests/test_line_d_gaps.py` asserts exactly
  that, per action type. No URI actions. A different button set therefore
  needs **no** Module A code change beyond the area-rect ↔ action table in
  `_default_rich_menu_payload()`; change it there and regenerate the PNG so
  the two stay in step.
- A postback cell's `displayText` is fixed at rich-menu registration time —
  there is no per-user-language hook the way a reply's own
  `i18n.resolve_reply_language` has — so it stays Thai-primary like the
  artwork.
- Keep the filename and pixel size so `config.toml`'s `rich_menu_image` path
  and LINE's own size validation don't need to change.
- The generator has no Raqm/libraqm dependency, so Pillow falls back to
  BASIC text layout. All six labels above are verified to shape correctly
  under it — including **น้ำ**, whose stacked tone-mark-plus-sara-am
  (`น` + `้` + `ำ`) is exactly the sequence that BASIC layout can collide,
  and which was eyeballed at full resolution before shipping. A **new**
  Thai label with unusual stacked vowel+tone sequences should get the same
  check.
