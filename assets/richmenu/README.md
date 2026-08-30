# Rich menu image — deployment asset

`richmenu.png` is the tappable command-menu image `LineChannel.
register_rich_menu()` uploads at startup (`channels/line.py`, Module A;
`config.toml`'s `[line].rich_menu_image`, default
`assets/richmenu/richmenu.png`). LINE's rich menu is a single image with
pixel-rect tap areas defined in the `POST /v2/bot/richmenu` call — the PNG
here is the **visual** half of that; the areas/actions are code (Module A).

## Current status: placeholder (SPEC-LINE.md §9 OQ3)

This is a **generated placeholder**, not a final design. OQ3's own
resolution: *"ship a plain 6-button layout as placeholder; Maya/Iris refine
later."* No Maya/Iris design pass has run for this deployment yet (Module D
scope is deployment, not visual design), so this placeholder ships instead
of blocking the release — rich-menu registration is fail-open
(`register_rich_menu()` logs and continues on any failure, R-A10), so even
a missing/broken image just means no menu, never a crash.

- **Size:** 2500×1686 px (LINE's large layout; the other valid size is
  2500×843 for a compact/half-height menu — not used here).
- **Layout:** a plain 3×2 grid, 6 equal cells, each a message-action button
  (tapping sends that literal text as an ordinary inbound message, R-A10 —
  no new wiring needed, it routes through the existing command dispatch
  unchanged).
- **Buttons** (EN command / TH label), left-to-right, top-to-bottom:

  | Cell | Command | Thai label |
  |---|---|---|
  | 1 | `/log` | บันทึก |
  | 2 | `/habits` | รายการนิสัย |
  | 3 | `/heatmap` | ปฏิทินความร้อน |
  | 4 | `/wrapped` | สรุปรายเดือน |
  | 5 | `/help` | ช่วยเหลือ |
  | 6 | `/guide` | เริ่มต้นใช้งาน |

  Each red **PLACEHOLDER** watermark is deliberate — it's there so nobody
  mistakes this for a finished design mid-review.
- **Font:** Thai text is set in the repo's own bundled
  `assets/fonts/NotoSansThai-Regular.ttf` (the same font `core/fonts.py`
  uses for chart PNGs, ROADMAP.md v1.9.0) — a plain system font renders
  Thai as tofu boxes, the same issue that font was bundled to fix.

## Regenerating it

The image was generated with Pillow (already a transitive dependency via
`matplotlib`/`[charts]`, or `pip install pillow` standalone). There's no
committed generator script — it's a ~70-line one-off (grid + centered
command/label text per cell). Re-derive it from the table above, or hand it
to a designer for the real pass; either way, keep the same filename and
pixel size so `config.toml`'s `rich_menu_image` path and LINE's size
validation don't need to change.

## Constraints for whoever designs the real version

- PNG, ≤1 MB (LINE's own limit), 2500×1686 or 2500×843.
- ≤20 tappable areas (LINE's cap) — 6 is plenty of headroom.
- Each area's *action* is a **message action** whose text is one of this
  bot's own commands (`/log`, `/habits`, `/heatmap`, `/wrapped`, `/help`,
  `/guide`, or any other — see `core/commands.py` for the full command
  list) — not a postback or URI action, so no code changes are needed on
  the Module A side for a different button set, only the area-rect ↔
  command-text mapping `register_rich_menu()` builds (a small, explicit
  table in `channels/line.py`).
- Bilingual (EN + TH) labeling matches this whole edition's reason for
  existing (SPEC-LINE.md header) — keep both languages visible, not just
  one.
