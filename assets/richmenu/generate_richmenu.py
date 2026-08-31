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
already is, labelled with the command that area actually sends. The grid
constants below MIRROR that function (`2500x1686`, 3x2, `w//3` x `h//2`),
and `verify_against_code()` asserts they still match by importing the real
payload builder -- so if someone re-cuts the tap areas in `line.py` without
regenerating this PNG, running this script fails loudly instead of shipping
artwork whose buttons point at the wrong commands.

Design: Modern & Clean -- near-white page, white cards, hairline borders,
one teal accent, flat (no gradients, no shadows), 8pt-derived spacing.
Every token is a named constant below.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

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
# same accent used only *inside* two icons. Neutrals are cool grays.

BG = "#FAFAFB"          # page background (near-white)
CARD = "#FFFFFF"        # cell surface
BORDER = "#E3E7EC"      # hairline cell separator
INK = "#16181D"         # Thai label (17.6:1 on CARD)
ACCENT = "#0F766E"      # icons + command text (5.3:1 on CARD)


def _tint(hex_color: str, amount: float) -> str:
    """`amount` 0.0 -> the colour itself, 1.0 -> white. Tints are derived,
    never hand-picked, so the palette stays a single hue by construction."""
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(c + (255 - c) * amount)  # noqa: E731
    return "#%02X%02X%02X" % (mix(r), mix(g), mix(b))


ACCENT_MID = _tint(ACCENT, 0.35)     # #63A6A1 -- mid heat cell
ACCENT_SOFT = _tint(ACCENT, 0.82)    # #D4E6E5 -- coldest heat cell
NEEDLE_SOUTH = _tint(ACCENT, 0.22)   # #3F918B -- compass south half

# --------------------------------------------------------------------------
# DESIGN TOKENS -- type
# --------------------------------------------------------------------------
# Sizes are canvas px. LINE renders the full 2500px width at roughly a phone
# width (~390pt), i.e. ~0.156x -- the display size in pt is noted after each.

LABEL_SIZE = 78   # Thai label       -> ~12pt rendered
CMD_SIZE = 54     # command sublabel -> ~8.4pt rendered
CMD_BOLDEN = 1.2  # faux-bold for the small line (regular face only)
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
CMD_BASELINE = 636    # command baseline, from the cell's top edge

SS = 2  # supersampling factor; PIL has no antialiased primitives, so the
        # whole canvas is drawn at 2x and LANCZOS-downsampled at the end.

# --------------------------------------------------------------------------
# THE SIX CELLS -- left-to-right, top-to-bottom, in `_default_rich_menu_
# payload()`'s own order. `command` is the literal text the tap sends.
# --------------------------------------------------------------------------

CELLS = (
    # command,     Thai label,        icon
    ("/log",      "บันทึก",         "plus"),
    ("/habits",   "ความคืบหน้า",     "checklist"),
    ("/heatmap",  "ปฏิทิน",          "heatmap"),
    ("/wrapped",  "สรุปภาพรวม",      "bars"),
    ("/help",     "ช่วยเหลือ",       "question"),
    ("/guide",    "เริ่มต้นใช้งาน",   "compass"),
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
# --------------------------------------------------------------------------


def icon_plus(d, cx, cy) -> None:
    """/log -- add an entry. The only SOLID icon in the menu: logging is the
    one action people open this menu for, so it gets the CTA treatment and
    stops sharing a plain-ring silhouette with /help."""
    r = ICON_R
    d.ellipse([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=ACCENT)
    arm = 50
    _round_cap_line(d, (cx - arm, cy), (cx + arm, cy), STROKE + 4, CARD)
    _round_cap_line(d, (cx, cy - arm), (cx, cy + arm), STROKE + 4, CARD)


def icon_checklist(d, cx, cy) -> None:
    """/habits -- today's list, top item already done."""
    box, gap, row_dy = 44, 34, 74
    left = cx - ICON_R + 4
    for i, y in enumerate((cy - row_dy, cy, cy + row_dy)):
        half = box / 2
        if i == 0:  # done
            _rrect(d, (left, y - half, left + box, y + half), 12, fill=ACCENT)
        else:
            _rrect(
                d,
                (left, y - half, left + box, y + half),
                12,
                outline=ACCENT,
                width=STROKE * 0.7,
            )
        _round_cap_line(
            d, (left + box + gap, y), (cx + ICON_R - 8, y), STROKE * 0.85, ACCENT
        )


def icon_heatmap(d, cx, cy) -> None:
    """/heatmap -- a 4x4 consistency grid. The silhouette is carried by the
    full-strength cells; the tints are texture, never the only signal (and
    the cell is labelled in words regardless)."""
    n, size, gap = 4, 40, 14
    span = n * size + (n - 1) * gap
    x0, y0 = cx - span / 2, cy - span / 2
    heat = (
        (2, 1, 3, 2),
        (3, 3, 1, 2),
        (1, 2, 3, 3),
        (2, 3, 2, 1),
    )
    tone = {1: ACCENT_SOFT, 2: ACCENT_MID, 3: ACCENT}
    for r, row in enumerate(heat):
        for c, level in enumerate(row):
            x = x0 + c * (size + gap)
            y = y0 + r * (size + gap)
            _rrect(d, (x, y, x + size, y + size), 10, fill=tone[level])


def icon_bars(d, cx, cy) -> None:
    """/wrapped -- the period recap, as a rising bar chart."""
    base = cy + 88
    w, gap = 46, 22
    heights = (76, 120, 168)
    total = len(heights) * w + (len(heights) - 1) * gap
    x = cx - total / 2
    for h in heights:
        _rrect(d, (x, base - h, x + w, base), 12, fill=ACCENT)
        x += w + gap
    _round_cap_line(
        d, (cx - ICON_R + 6, base + 24), (cx + ICON_R - 6, base + 24), 16, ACCENT
    )


def icon_question(d, cx, cy, font_q) -> None:
    """/help -- the universal circled question mark. The glyph comes from
    the bundled face (Latin, not emoji) and is emboldened with a text
    stroke so its weight matches the ring."""
    r = ICON_R - STROKE / 2
    d.ellipse(
        [s(cx - r), s(cy - r), s(cx + r), s(cy + r)],
        outline=ACCENT,
        width=round(s(STROKE)),
    )
    # Centre the glyph's *ink*, not its em box (a "?" sits high in the em).
    x0, y0, x1, y1 = font_q.getbbox("?")
    _text(
        d,
        (cx - (x0 + x1) / 2 / SS, cy - (y0 + y1) / 2 / SS),
        "?",
        font_q,
        ACCENT,
        anchor="la",
        stroke=QMARK_BOLDEN,
    )


def icon_compass(d, cx, cy) -> None:
    """/guide -- a compass, matching the app's own 🧭 in `guide_header`."""
    r = ICON_R - STROKE / 2
    d.ellipse(
        [s(cx - r), s(cy - r), s(cx + r), s(cy + r)],
        outline=ACCENT,
        width=round(s(STROKE)),
    )
    # The needle is deliberately fat-waisted: a thin one blurs into a
    # straight line at LINE's rendered size and the icon starts reading as
    # a "no entry" sign. At waist 13 it stays a two-tone kite.
    tip, waist = 54, 13
    north = [(cx + tip, cy - tip), (cx + waist, cy + waist), (cx - waist, cy - waist)]
    south = [(cx - tip, cy + tip), (cx + waist, cy + waist), (cx - waist, cy - waist)]
    d.polygon([(s(x), s(y)) for x, y in south], fill=NEEDLE_SOUTH)
    d.polygon([(s(x), s(y)) for x, y in north], fill=ACCENT)


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
        ("Thai label", INK, CARD, 4.5),
        ("command sublabel", ACCENT, CARD, 4.5),
        ("icon stroke", ACCENT, CARD, 3.0),
        ("plus on /log disc", CARD, ACCENT, 3.0),
        ("card vs page", CARD, BG, 1.0),
        ("hairline vs card", BORDER, CARD, 1.0),
    ]
    for name, fg, bg, floor in checks:
        ratio = contrast(fg, bg)
        ok = ratio >= floor
        report.append(
            f"  {'ok ' if ok else 'FAIL'} {name:<18} {fg} on {bg}  "
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
    matches it, cell for cell and command for command."""
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
        if area["action"].get("text") != cell[0]:
            raise SystemExit(
                f"Cell {i}: code sends {area['action'].get('text')!r}, "
                f"artwork is labelled {cell[0]!r}"
            )
    return f"  ok  {len(areas)} tap areas match the drawn grid and their labels"


# --------------------------------------------------------------------------
# Render
# --------------------------------------------------------------------------


def render() -> Image.Image:
    if not FONT_PATH.is_file():
        raise SystemExit(f"Missing bundled Thai font: {FONT_PATH}")
    font_label = ImageFont.truetype(str(FONT_PATH), round(s(LABEL_SIZE)))
    font_cmd = ImageFont.truetype(str(FONT_PATH), round(s(CMD_SIZE)))
    font_q = ImageFont.truetype(str(FONT_PATH), round(s(QMARK_SIZE)))

    img = Image.new("RGB", (CANVAS_W * SS, CANVAS_H * SS), BG)
    d = ImageDraw.Draw(img)

    icons = {
        "plus": icon_plus,
        "checklist": icon_checklist,
        "heatmap": icon_heatmap,
        "bars": icon_bars,
        "question": lambda dd, x, y: icon_question(dd, x, y, font_q),
        "compass": icon_compass,
    }
    label_max_w = CELL_W - 2 * (CARD_INSET + LABEL_SIDE_PAD)

    for i, (command, label, icon) in enumerate(CELLS):
        row, col = divmod(i, COLS)
        cx0, cy0 = col * CELL_W, row * CELL_H
        cx = cx0 + CELL_W / 2

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
            fill=CARD,
            outline=BORDER,
            width=CARD_BORDER,
        )

        icons[icon](d, cx, cy0 + ICON_CY)

        # Deterministic shrink-to-fit: a longer label in a future edit gets
        # smaller rather than colliding with the card edge.
        f_label = font_label
        size = LABEL_SIZE
        while d.textlength(label, font=f_label) / SS > label_max_w and size > 40:
            size -= 2
            f_label = ImageFont.truetype(str(FONT_PATH), round(s(size)))
        if size != LABEL_SIZE:
            print(f"  note: '{label}' shrunk to {size}px to fit {label_max_w}px")

        _text(d, (cx, cy0 + LABEL_BASELINE), label, f_label, INK)
        _text(d, (cx, cy0 + CMD_BASELINE), command, font_cmd, ACCENT, stroke=CMD_BOLDEN)

    return img.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate the LINE rich-menu PNG.")
    ap.add_argument("-o", "--out", type=Path, default=OUT_PATH)
    args = ap.parse_args()

    print("Tap areas:")
    print(verify_against_code())
    print("Contrast:")
    for line in check_contrast():
        print(line)

    img = render()
    if img.size != (CANVAS_W, CANVAS_H):  # pragma: no cover - guard
        raise SystemExit(f"Rendered {img.size}, expected {(CANVAS_W, CANVAS_H)}")
    # Truecolour, deliberately: quantising this flat art to a 64-colour
    # palette halves the file (~60KB vs ~130KB), but 130KB is already ~13%
    # of LINE's 1MB ceiling, so the saving buys nothing and costs two
    # things worth more -- an indexed PNG is a needless edge case for a
    # third-party uploader, and a small palette risks banding on the
    # antialiased icon curves. Spend the headroom.
    img.save(args.out, format="PNG", optimize=True)

    kb = args.out.stat().st_size / 1024
    print(f"Wrote {args.out} -- {img.size[0]}x{img.size[1]}, {kb:.1f} KB (limit 1000 KB)")
    if args.out.stat().st_size >= 1_000_000:
        raise SystemExit("Over LINE's 1MB rich-menu image limit")


if __name__ == "__main__":
    main()
