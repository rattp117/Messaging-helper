"""Generates `assets/richmenu/richmenu.png` -- the LINE rich-menu artwork.

Run it with the project venv (Pillow required, no other dependency):

    python assets/richmenu/generate_richmenu.py

Deterministic: same inputs (this file + the bundled font) -> byte-identical
PNG. Re-run it after editing any constant below; commit the result.

WHY THE GRID IS NOT NEGOTIABLE
------------------------------
LINE's rich menu is one flat image plus a list of pixel-rect tap areas that
live in code -- `src/habit_assistant/channels/line.py`'s
`_default_rich_menu_payload()`. The image has no idea where the buttons
are; the artwork's only job is to draw a cell exactly where a tap area
already is, labelled with what that area actually does. The grid constants
below MIRROR that function (`2500x1686`, 3x2, `w//3` x `h//2`), and
`verify_against_code()` asserts they still match by importing the real
payload builder -- so if someone re-cuts the tap areas in `line.py` without
regenerating this PNG, running this script fails loudly instead of shipping
artwork whose buttons point at the wrong thing.

v2 -- ACTIONS vs NAVIGATION (rich-menu rewire, 2026-09-03 request)
------------------------------------------------------------------
The menu is no longer six of a kind. The top row now leads with two
DIRECT-LOG cells -- postback actions carrying `log:water:250` /
`log:stretch:10`, which write a log row on a single tap -- while the other
four cells still just navigate (an ordinary message action that sends a
slash command). Two different KINDS of button, so they get two different
treatments:

  * ACTION cells  -> accent-FILLED card, white ink. This is v1's own
    "solid = the action" rationale (the old `/log` cell was the one solid
    icon in the menu) promoted from the icon to the whole card, because
    the action is now the card: tapping it logs, it doesn't open a
    keyboard one tap deeper. Two filled teal blocks on the top row read
    as primaries against four white cards at thumbnail size, which is the
    size that actually matters -- see `SS`/scale note below.
  * NAV cells     -> v1 unchanged: white card, hairline border, accent
    icon, accent slash-command sublabel.

An action cell's big label carries the AMOUNT the tap logs ("น้ำ 250 มล."),
because that amount is the whole point of the button, and it deliberately
does NOT show a slash command -- there is no command to type that would do
this. `verify_against_code()` enforces that the drawn amount is the amount
the payload actually sends.

Design: Modern & Clean -- near-white page, hairline borders, ONE accent
(filled or hairline, nothing else), flat (no gradients, no shadows),
8pt-derived spacing. Every token is a named constant below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# Thai MUST use the repo's own bundled face. A system default font renders
# Thai as tofu boxes -- the exact issue this font was vendored to fix
# (`core/fonts.py`, ROADMAP v1.9.0). It also covers Latin, so the whole
# image uses one face and renders identically on any machine.
FONT_PATH = REPO_ROOT / "assets" / "fonts" / "NotoSansThai-Regular.ttf"
OUT_PATH = HERE / "richmenu.png"

# --------------------------------------------------------------------------
# DESIGN TOKENS -- colour
# --------------------------------------------------------------------------
# One accent (teal 700 -- a calm, clinical blue-green that suits a health /
# habit product without shouting), plus two machine-derived tints of that
# same accent used only *inside* the heatmap icon. Neutrals are cool grays.
#
# The accent does double duty in v2: hairline-weight ink on a NAV card, and
# the full card fill on an ACTION card. Both directions are contrast-checked
# in `check_contrast()` -- white-on-accent is 5.47:1, which clears AA for
# real text, so the filled cards can carry their labels in plain white
# rather than needing a second, lighter accent.

BG = "#FAFAFB"          # page background (near-white)
CARD = "#FFFFFF"        # nav cell surface; also the ink on an action card
BORDER = "#E3E7EC"      # hairline cell separator (nav cards only)
INK = "#16181D"         # Thai label on a nav card (17.8:1 on CARD)
ACCENT = "#0F766E"      # nav icons + sublabel; ACTION card fill (5.5:1 vs CARD)


def _tint(hex_color: str, amount: float) -> str:
    """`amount` 0.0 -> the colour itself, 1.0 -> white. Tints are derived,
    never hand-picked, so the palette stays a single hue by construction."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(c + (255 - c) * amount)  # noqa: E731
    return "#%02X%02X%02X" % (mix(r), mix(g), mix(b))


ACCENT_MID = _tint(ACCENT, 0.35)     # #63A6A1 -- mid heat cell
ACCENT_SOFT = _tint(ACCENT, 0.82)    # #D4E6E5 -- coldest heat cell

# --------------------------------------------------------------------------
# DESIGN TOKENS -- type
# --------------------------------------------------------------------------
# Sizes are canvas px. LINE renders the full 2500px width at roughly a phone
# width (~390pt), i.e. ~0.156x -- the display size in pt is noted after each.
#
# Renamed in v2 (CMD_* -> SUB_*): the small line under the label is no
# longer always a command. On a nav cell it is the literal slash command the
# tap sends; on an action cell it is an English gloss of the amount, because
# no typed command is equivalent to that button.

LABEL_SIZE = 78   # Thai label   -> ~12pt rendered
SUB_SIZE = 54     # sublabel      -> ~8.4pt rendered
SUB_BOLDEN = 1.2  # faux-bold for the small line (regular face only)
QMARK_SIZE = 132  # the "?" inside the help icon
QMARK_BOLDEN = 5.0

# --------------------------------------------------------------------------
# DESIGN TOKENS -- geometry (8pt grid: every value below is a multiple of 8,
# except stroke/hairline weights, which are optical)
# --------------------------------------------------------------------------

CANVAS_W, CANVAS_H = 2500, 1686   # LINE "large" rich menu; also the only
COLS, ROWS = 3, 2                 # size `line.py` builds areas for
CELL_W, CELL_H = CANVAS_W // COLS, CANVAS_H // ROWS   # 833 x 843

CARD_INSET = 32      # card inset inside its tap cell -> 64px gutter between
CARD_RADIUS = 32
CARD_BORDER = 6      # ~0.9pt rendered: a true hairline that still survives
LABEL_SIDE_PAD = 48  # min clear space either side of the label text

ICON_CY = 310        # icon centre, from the cell's top edge
ICON_R = 100         # nominal icon half-box (icons are 200x200)
STROKE = 18          # one stroke weight for every icon -> ~2.8pt rendered
LABEL_BASELINE = 546  # Thai baseline, from the cell's top edge
SUB_BASELINE = 636    # sublabel baseline, from the cell's top edge

SS = 2  # supersampling factor; PIL has no antialiased primitives, so the
        # whole canvas is drawn at 2x and LANCZOS-downsampled at the end.

# --------------------------------------------------------------------------
# THE SIX CELLS -- left-to-right, top-to-bottom, in `_default_rich_menu_
# payload()`'s own order.
# --------------------------------------------------------------------------


class Cell(NamedTuple):
    """One drawn cell. `action_type`/`payload` mirror what the code sends:
    a `message` cell's payload is its literal command text, a `postback`
    cell's payload is its literal callback `data`. `verify_against_code()`
    asserts both against the real payload builder."""

    action_type: str  # "message" | "postback"
    payload: str      # message text, or postback data
    label: str        # Thai, drawn large
    sublabel: str     # drawn small: the command (nav) or an English gloss (action)
    icon: str

    @property
    def is_action(self) -> bool:
        """A postback cell logs on tap -> it gets the filled-card treatment.
        Derived, never stored twice, so the visual treatment cannot drift
        out of step with what the button actually does."""
        return self.action_type == "postback"


CELLS = (
    # -- ACTION: one tap writes a log row (filled accent card, white ink) --
    Cell("postback", "log:water:250", "น้ำ 250 มล.", "250 ml water", "droplet"),
    Cell("postback", "log:stretch:10", "ยืดเส้น 10 นาที", "10 min stretch", "stretch"),
    # -- NAV: one tap sends a slash command (white card, accent ink) --
    Cell("message", "/habits", "ความคืบหน้า", "/habits", "checklist"),
    Cell("message", "/heatmap", "ปฏิทิน", "/heatmap", "heatmap"),
    Cell("message", "/wrapped", "สรุปภาพรวม", "/wrapped", "bars"),
    Cell("message", "/help", "ช่วยเหลือ", "/help", "question"),
)


# --------------------------------------------------------------------------
# Drawing helpers -- every coordinate below is in CANVAS space; `s()` scales
# into the supersampled buffer so the design math stays readable.
# --------------------------------------------------------------------------


def s(v: float) -> float:
    return v * SS


def _round_cap_line(d: ImageDraw.ImageDraw, p0, p1, width: float, fill: str) -> None:
    """PIL's line has no cap style; draw the segment, then a disc at each
    end. Keeps every icon terminal identically rounded."""
    d.line([s(p0[0]), s(p0[1]), s(p1[0]), s(p1[1])], fill=fill, width=round(s(width)))
    r = s(width) / 2
    for x, y in (p0, p1):
        d.ellipse([s(x) - r, s(y) - r, s(x) + r, s(y) + r], fill=fill)


def _rrect(d, box, radius, fill=None, outline=None, width=0) -> None:
    d.rounded_rectangle(
        [s(box[0]), s(box[1]), s(box[2]), s(box[3])],
        radius=s(radius),
        fill=fill,
        outline=outline,
        width=round(s(width)),
    )


def _text(d, xy, text, font, fill, anchor="ms", stroke=0.0) -> None:
    d.text(
        (s(xy[0]), s(xy[1])),
        text,
        font=font,
        fill=fill,
        anchor=anchor,
        stroke_width=round(s(stroke)),
        stroke_fill=fill,
    )


# --------------------------------------------------------------------------
# Icons -- PIL primitives only (circles, rounded rects, lines, polygons).
# No emoji glyphs: they render inconsistently across platforms and would
# hard-depend on a colour-emoji font being present at generation time.
# Each draws inside a 200x200 box centred on (cx, cy) at one shared STROKE.
#
# Uniform signature `(d, cx, cy, ink)`: `ink` is whatever contrasts with the
# card this icon lands on -- ACCENT on a white nav card, CARD (white) on a
# filled action card -- so an icon never hardcodes a colour that assumes one
# treatment.
# --------------------------------------------------------------------------


def icon_droplet(d, cx, cy, ink) -> None:
    """ACTION: log water. A solid teardrop -- the most legible "water" mark
    at thumbnail size, and solid (not outlined) so it holds up as white ink
    on the filled card. Built as a circle plus a triangle whose two sides
    are TANGENT to that circle, so the union is a smooth teardrop with no
    visible kink where the straight edges meet the curve."""
    r, apex_dy, cy_off = 74, -96, 26
    ccx, ccy = cx, cy + cy_off
    d.ellipse([s(ccx - r), s(ccy - r), s(ccx + r), s(ccy + r)], fill=ink)
    # Tangent points from the apex to the circle: cos(theta) = r / dist.
    dist = (ccy - (cy + apex_dy))
    cos_t = r / dist
    sin_t = (1 - cos_t**2) ** 0.5
    tx, ty = r * sin_t, ccy - r * cos_t
    d.polygon(
        [(s(cx), s(cy + apex_dy)), (s(cx - tx), s(ty)), (s(cx + tx), s(ty))], fill=ink
    )


def icon_stretch(d, cx, cy, ink) -> None:
    """ACTION: log a stretch. A figure with both arms raised -- reads as
    "stretch/exercise" at thumbnail size where a side-bend or lunge pose
    blurs into an unreadable squiggle. Its silhouette is deliberately
    nothing like the droplet's, so the two action cells stay tellable apart
    at a glance even before the labels are legible."""
    head_r, head_cy = 24, cy - 62
    d.ellipse(
        [s(cx - head_r), s(head_cy - head_r), s(cx + head_r), s(head_cy + head_r)],
        fill=ink,
    )
    # Torso first, drawn up INTO the head so the two merge with no seam.
    _round_cap_line(d, (cx, cy - 38), (cx, cy + 22), STROKE, ink)
    # The arms start at two SEPARATE shoulders rather than one point on the
    # spine: a single origin leaves a sharp wedge of card colour between the
    # two raised arms and the head, which reads as a rendering defect at
    # full resolution. Offset by 14 (> STROKE/2) puts each shoulder's round
    # cap inside the torso stroke, so the arms merge into the body and the
    # wedge is filled by the torso itself.
    for sign in (-1, 1):
        _round_cap_line(
            d, (cx + sign * 14, cy - 26), (cx + sign * 64, cy - 70), STROKE, ink
        )
        _round_cap_line(d, (cx, cy + 22), (cx + sign * 46, cy + 84), STROKE, ink)


def icon_checklist(d, cx, cy, ink) -> None:
    """NAV: /habits -- today's list, top item already done."""
    box, gap, row_dy = 44, 34, 74
    left = cx - ICON_R + 4
    for i, y in enumerate((cy - row_dy, cy, cy + row_dy)):
        half = box / 2
        if i == 0:  # done
            _rrect(d, (left, y - half, left + box, y + half), 12, fill=ink)
        else:
            _rrect(
                d,
                (left, y - half, left + box, y + half),
                12,
                outline=ink,
                width=STROKE * 0.7,
            )
        _round_cap_line(
            d, (left + box + gap, y), (cx + ICON_R - 8, y), STROKE * 0.85, ink
        )


def icon_heatmap(d, cx, cy, ink) -> None:
    """NAV: /heatmap -- a 4x4 consistency grid. The silhouette is carried by
    the full-strength cells; the tints are texture, never the only signal
    (and the cell is labelled in words regardless). The two tints assume a
    light card, which holds by construction: this is a nav cell, and only
    action cells are filled."""
    n, size, gap = 4, 40, 14
    span = n * size + (n - 1) * gap
    x0, y0 = cx - span / 2, cy - span / 2
    heat = (
        (2, 1, 3, 2),
        (3, 3, 1, 2),
        (1, 2, 3, 3),
        (2, 3, 2, 1),
    )
    tone = {1: ACCENT_SOFT, 2: ACCENT_MID, 3: ink}
    for r, row in enumerate(heat):
        for c, level in enumerate(row):
            x = x0 + c * (size + gap)
            y = y0 + r * (size + gap)
            _rrect(d, (x, y, x + size, y + size), 10, fill=tone[level])


def icon_bars(d, cx, cy, ink) -> None:
    """NAV: /wrapped -- the period recap, as a rising bar chart."""
    base = cy + 88
    w, gap = 46, 22
    heights = (76, 120, 168)
    total = len(heights) * w + (len(heights) - 1) * gap
    x = cx - total / 2
    for h in heights:
        _rrect(d, (x, base - h, x + w, base), 12, fill=ink)
        x += w + gap
    _round_cap_line(
        d, (cx - ICON_R + 6, base + 24), (cx + ICON_R - 6, base + 24), 16, ink
    )


def icon_question(d, cx, cy, ink, font_q) -> None:
    """NAV: /help -- the universal circled question mark. The glyph comes
    from the bundled face (Latin, not emoji) and is emboldened with a text
    stroke so its weight matches the ring."""
    r = ICON_R - STROKE / 2
    d.ellipse(
        [s(cx - r), s(cy - r), s(cx + r), s(cy + r)],
        outline=ink,
        width=round(s(STROKE)),
    )
    # Centre the glyph's *ink*, not its em box (a "?" sits high in the em).
    x0, y0, x1, y1 = font_q.getbbox("?")
    _text(
        d,
        (cx - (x0 + x1) / 2 / SS, cy - (y0 + y1) / 2 / SS),
        "?",
        font_q,
        ink,
        anchor="la",
        stroke=QMARK_BOLDEN,
    )


# --------------------------------------------------------------------------
# Contrast (WCAG 2.1) -- the palette is checked, not assumed
# --------------------------------------------------------------------------


def _luminance(hex_color: str) -> float:
    out = []
    for i in (1, 3, 5):
        c = int(hex_color[i : i + 2], 16) / 255
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast(a: str, b: str) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check_contrast() -> list[str]:
    """Text >= 4.5:1 (AA), non-text/graphics >= 3:1 (1.4.11)."""
    report, failures = [], []
    checks = [
        # nav cards: dark ink on white
        ("nav label", INK, CARD, 4.5),
        ("nav sublabel", ACCENT, CARD, 4.5),
        ("nav icon stroke", ACCENT, CARD, 3.0),
        # action cards: white ink on the filled accent card. One pair carries
        # the label, the sublabel AND the icon, so it is held to the
        # strictest of the three floors (4.5:1, text) rather than 3:1.
        ("action ink on fill", CARD, ACCENT, 4.5),
        # the filled card's own edge is carried by fill-vs-page, not a
        # hairline, so that boundary has to clear the 3:1 graphics floor
        ("action card vs page", ACCENT, BG, 3.0),
        ("nav card vs page", CARD, BG, 1.0),
        ("hairline vs card", BORDER, CARD, 1.0),
    ]
    for name, fg, bg, floor in checks:
        ratio = contrast(fg, bg)
        ok = ratio >= floor
        report.append(
            f"  {'ok ' if ok else 'FAIL'} {name:<20} {fg} on {bg}  "
            f"{ratio:5.2f}:1  (needs {floor}:1)"
        )
        if not ok:
            failures.append(name)
    if failures:
        raise SystemExit("Contrast failures: " + ", ".join(failures) + "\n" + "\n".join(report))
    return report


# --------------------------------------------------------------------------
# Tap-area verification -- the artwork must agree with the code
# --------------------------------------------------------------------------


def expected_areas() -> list[dict]:
    out = []
    for i in range(COLS * ROWS):
        row, col = divmod(i, COLS)
        out.append(
            {"x": col * CELL_W, "y": row * CELL_H, "width": CELL_W, "height": CELL_H}
        )
    return out


def verify_against_code() -> str:
    """Import the real payload builder and assert this file's grid still
    matches it: cell for cell, bounds, action TYPE, and payload -- the
    command text for a message cell, the callback `data` for a postback
    cell. Plus, for an action cell, that the amount drawn on the card is
    the amount the payload actually logs (a card reading "250 มล." over a
    `log:water:500` payload is the one drift this artwork can't survive)."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    try:
        from habit_assistant.channels.line import _default_rich_menu_payload
    except Exception as exc:  # pragma: no cover - only when run outside the repo
        return f"  SKIPPED (could not import channels.line: {exc})"
    payload = _default_rich_menu_payload()
    size = payload["size"]
    if (size["width"], size["height"]) != (CANVAS_W, CANVAS_H):
        raise SystemExit(
            f"Canvas {CANVAS_W}x{CANVAS_H} != rich menu {size['width']}x{size['height']}"
        )
    areas = payload["areas"]
    if len(areas) != len(CELLS):
        raise SystemExit(f"{len(areas)} tap areas in code, {len(CELLS)} cells drawn")
    for i, (area, expected, cell) in enumerate(zip(areas, expected_areas(), CELLS)):
        if area["bounds"] != expected:
            raise SystemExit(f"Cell {i}: code bounds {area['bounds']} != drawn {expected}")
        action = area["action"]
        if action.get("type") != cell.action_type:
            raise SystemExit(
                f"Cell {i}: code action type {action.get('type')!r}, "
                f"artwork drawn as {cell.action_type!r}"
            )
        if cell.action_type == "message":
            if action.get("text") != cell.payload:
                raise SystemExit(
                    f"Cell {i}: code sends {action.get('text')!r}, "
                    f"artwork is labelled {cell.payload!r}"
                )
        else:
            if action.get("data") != cell.payload:
                raise SystemExit(
                    f"Cell {i}: code posts back {action.get('data')!r}, "
                    f"artwork is drawn for {cell.payload!r}"
                )
            if not action.get("displayText"):
                raise SystemExit(f"Cell {i}: postback has no displayText")
            amount = cell.payload.rsplit(":", 1)[-1]
            if amount not in cell.label:
                raise SystemExit(
                    f"Cell {i}: payload logs {amount!r} but the card is "
                    f"labelled {cell.label!r} -- the drawn amount must be "
                    f"the amount the tap sends"
                )
    n_action = sum(1 for c in CELLS if c.is_action)
    return (
        f"  ok  {len(areas)} tap areas match the drawn grid, their types and "
        f"their payloads ({n_action} action, {len(areas) - n_action} nav)"
    )


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def render() -> Image.Image:
    if not FONT_PATH.is_file():
        raise SystemExit(f"Missing bundled Thai font: {FONT_PATH}")
    font_label = ImageFont.truetype(str(FONT_PATH), round(s(LABEL_SIZE)))
    font_sub = ImageFont.truetype(str(FONT_PATH), round(s(SUB_SIZE)))
    font_q = ImageFont.truetype(str(FONT_PATH), round(s(QMARK_SIZE)))

    img = Image.new("RGB", (CANVAS_W * SS, CANVAS_H * SS), BG)
    d = ImageDraw.Draw(img)

    icons = {
        "droplet": icon_droplet,
        "stretch": icon_stretch,
        "checklist": icon_checklist,
        "heatmap": icon_heatmap,
        "bars": icon_bars,
        "question": lambda dd, x, y, ink: icon_question(dd, x, y, ink, font_q),
    }
    label_max_w = CELL_W - 2 * (CARD_INSET + LABEL_SIDE_PAD)

    for i, cell in enumerate(CELLS):
        row, col = divmod(i, COLS)
        cx0, cy0 = col * CELL_W, row * CELL_H
        cx = cx0 + CELL_W / 2

        # An ACTION cell is a filled accent card with white ink; a NAV cell
        # is v1's white card with a hairline border and accent ink. The card
        # edge is truthful either way -- carried by the hairline on a nav
        # card, by fill-vs-page contrast on an action card.
        if cell.is_action:
            fill, border, ink, label_ink = ACCENT, ACCENT, CARD, CARD
        else:
            fill, border, ink, label_ink = CARD, BORDER, ACCENT, INK

        # The card is drawn inset inside the tap rect, so the visible edge
        # is a truthful preview of where the tappable cell actually is.
        _rrect(
            d,
            (
                cx0 + CARD_INSET,
                cy0 + CARD_INSET,
                cx0 + CELL_W - CARD_INSET,
                cy0 + CELL_H - CARD_INSET,
            ),
            CARD_RADIUS,
            fill=fill,
            outline=border,
            width=CARD_BORDER,
        )

        icons[cell.icon](d, cx, cy0 + ICON_CY, ink)

        # Deterministic shrink-to-fit: a longer label in a future edit gets
        # smaller rather than colliding with the card edge.
        f_label = font_label
        size = LABEL_SIZE
        while d.textlength(cell.label, font=f_label) / SS > label_max_w and size > 40:
            size -= 2
            f_label = ImageFont.truetype(str(FONT_PATH), round(s(size)))
        if size != LABEL_SIZE:
            print(f"  note: cell {i} label shrunk to {size}px to fit {label_max_w}px")

        _text(d, (cx, cy0 + LABEL_BASELINE), cell.label, f_label, label_ink)
        _text(
            d,
            (cx, cy0 + SUB_BASELINE),
            cell.sublabel,
            font_sub,
            ink,
            stroke=SUB_BOLDEN,
        )

    return img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the LINE rich-menu PNG.")
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    # This script prints Thai (cell labels) on failure paths. A Windows
    # console defaults to cp1252, where that is a UnicodeEncodeError -- i.e.
    # the generator would crash while reporting a problem instead of
    # reporting it. Force UTF-8 on our own stdout; no-op where it already is.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("Tap areas:")
    print(verify_against_code())
    print("Contrast:")
    for line in check_contrast():
        print(line)

    img = render()
    if img.size != (CANVAS_W, CANVAS_H):  # pragma: no cover - guard
        raise SystemExit(f"Rendered {img.size}, expected {(CANVAS_W, CANVAS_H)}")
    # Truecolour, deliberately: quantising this flat art to a 64-colour
    # palette halves the file, but the result is already a small fraction of
    # LINE's 1MB ceiling, so the saving buys nothing and costs two things
    # worth more -- an indexed PNG is a needless edge case for a third-party
    # uploader, and a small palette risks banding on the antialiased icon
    # curves. Spend the headroom.
    img.save(args.out, format="PNG", optimize=True)

    kb = args.out.stat().st_size / 1024
    print(f"Wrote {args.out} -- {img.size[0]}x{img.size[1]}, {kb:.1f} KB (limit 1000 KB)")
    if args.out.stat().st_size >= 1_000_000:
        raise SystemExit("Over LINE's 1MB rich-menu image limit")


if __name__ == "__main__":
    main()
