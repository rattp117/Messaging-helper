"""SPEC-LINE-PORTAL.md §6/UI.md §8/§9.2 (shared surface, admin web portal,
branch `line-version`): `core/portal/layout.py`'s own unit tests --
escaping (the XSS boundary, UI.md §9.2 contract 14), the page shell's
markup contracts (viewport meta, `<html lang>`, shell order), the
markup-contract-bearing builders (`.confirm` shape, `td[data-label]`,
the ONE inline style rule), and the stylesheet itself (still under the
~8KB budget, still carries the `@font-face` addition, still parses as
valid CSS by brace-balance).
"""

from __future__ import annotations

from habit_assistant.core.portal import layout


# ===========================================================================
# escape() -- the XSS boundary.
# ===========================================================================


def test_escape_neutralizes_script_tags_and_quotes():
    dangerous = '<script>alert("x")</script>&\'"'
    out = layout.escape(dangerous)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert '"' not in out.replace("&quot;", "")  # only the escaped form remains
    assert "&amp;" in out


def test_escape_handles_non_string_input():
    assert layout.escape(42) == "42"
    assert layout.escape(None) == "None"


def test_render_nav_escapes_a_hostile_current_key_and_still_renders():
    # `current` never legitimately contains markup, but every builder here
    # is defensive regardless -- prove nav rendering doesn't choke on it.
    html_out = layout.render_nav(current="<img src=x>", lang="en", pending=0)
    assert "<img" not in html_out


def test_tile_escapes_label_value_and_small():
    html_out = layout.tile("<i>label</i>", "<u>value</u>", small="<s>small</s>")
    assert "<i>label" not in html_out and "&lt;i&gt;label" in html_out
    assert "<u>value" not in html_out and "&lt;u&gt;value" in html_out
    assert "<s>small" not in html_out and "&lt;s&gt;small" in html_out


def test_confirm_disclosure_escapes_hidden_field_values_and_body_text():
    html_out = layout.confirm_disclosure(
        action="/users/approve",
        hidden_fields={"chat_id": '"><script>evil()</script>'},
        summary_text="Approve",
        body_text="<b>inject</b>",
        submit_text="Confirm",
        cancel_href="/users",
        cancel_text="Cancel",
    )
    assert "<script>evil()</script>" not in html_out
    assert "<b>inject</b>" not in html_out


def test_td_cell_escapes_the_data_label():
    html_out = layout.td_cell('"><script>x</script>', "value")
    assert "<script>x</script>" not in html_out
    assert 'data-label="' in html_out


# ===========================================================================
# UI.md §9.2 markup contracts.
# ===========================================================================


def test_page_shell_carries_viewport_meta_and_resolved_lang():
    html_out = layout.page(
        lang="th",
        title="Status",
        current="status",
        pending=0,
        body="<p>hi</p>",
        path_qs="/",
        tz_name="Asia/Bangkok",
        as_of="06:14:03",
    )
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html_out
    assert '<html lang="th">' in html_out
    # shell order: header > .wrap > main.wrap#main > footer > .wrap
    assert html_out.index("<header>") < html_out.index('id="main"') < html_out.index("<footer>")


def test_page_shell_places_flash_immediately_inside_main_before_body():
    html_out = layout.page(
        lang="en",
        title="Users",
        current="users",
        pending=1,
        body="<p>BODY-MARKER</p>",
        path_qs="/users",
        tz_name="Asia/Bangkok",
        as_of="06:14:03",
        flash='<div id="flash">FLASH-MARKER</div>',
    )
    assert html_out.index('id="main"') < html_out.index("FLASH-MARKER") < html_out.index("BODY-MARKER")


def test_bar_clamps_and_formats_to_one_decimal():
    assert 'style="width:0.0%"' in layout.bar(-50)
    assert 'style="width:100.0%"' in layout.bar(250)
    assert 'style="width:87.3%"' in layout.bar(87.3456)


def test_bar_is_aria_hidden():
    assert 'aria-hidden="true"' in layout.bar(50)


def test_confirm_disclosure_shape_is_exactly_summary_plus_one_div():
    html_out = layout.confirm_disclosure(
        action="/users/block",
        hidden_fields={"chat_id": "U123"},
        summary_text="Block",
        body_text="Block this user?",
        submit_text="Confirm block",
        cancel_href="/users",
        cancel_text="Cancel",
        tier="stop",
    )
    assert '<details class="confirm stop">' in html_out
    assert html_out.count("<summary>") == 1
    assert html_out.count("</details>") == 1
    assert '<button class="btn">' in html_out


def test_td_cell_carries_data_label_and_head_full_modifiers():
    plain = layout.td_cell("When", "08-31 14:03")
    head = layout.td_cell("Name", "Somchai", head=True)
    full = layout.td_cell("Detail", "a long value", full=True)
    assert 'data-label="When"' in plain and 'class="' not in plain
    assert 'class="head"' in head
    assert 'class="full"' in full


def test_btn_never_renders_input_submit():
    assert "<input" not in layout.btn("Send")
    assert '<button class="btn" type="submit">' in layout.btn("Send")


def test_nav_pending_count_in_label_not_a_separate_badge():
    html_out = layout.render_nav(current="status", lang="en", pending=2)
    assert 'class="pending"' in html_out
    assert "Users (2)" in html_out
    zero_html = layout.render_nav(current="status", lang="en", pending=0)
    assert "Users (0)" not in zero_html
    assert ">Users<" in zero_html
    assert 'class="pending"' not in zero_html


# ===========================================================================
# panel_or_unavailable: fail-open, keeps its heading.
# ===========================================================================


def test_panel_or_unavailable_keeps_heading_when_body_raises():
    def _boom():
        raise RuntimeError("db hiccup")

    html_out = layout.panel_or_unavailable("Storage", _boom, lang="en")
    assert "<h2>Storage</h2>" in html_out
    assert "Can&#x27;t read this right now." in html_out or "Can't read this right now." in html_out.replace("&#x27;", "'")


def test_panel_or_unavailable_renders_body_normally_on_success():
    html_out = layout.panel_or_unavailable("Storage", lambda: "<p>4.2 MB</p>", lang="en")
    assert "<h2>Storage</h2><p>4.2 MB</p>" in html_out


# ===========================================================================
# pending_count(db): fail-open.
# ===========================================================================


class _RaisingDB:
    def list_users(self):
        raise RuntimeError("simulated DB failure")


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def list_users(self):
        return self._rows


def test_pending_count_fails_open_to_zero():
    assert layout.pending_count(_RaisingDB()) == 0


def test_pending_count_counts_only_pending_status():
    rows = [{"status": "pending"}, {"status": "active"}, {"status": "pending"}, {"status": "blocked"}]
    assert layout.pending_count(_FakeDB(rows)) == 2


# ===========================================================================
# render_500: no nav, no traceback, localized.
# ===========================================================================


def test_render_500_has_no_nav_and_no_traceback_markers():
    html_out = layout.render_500("en")
    assert "<nav>" not in html_out
    assert "Traceback" not in html_out
    assert "Something went wrong" in html_out


def test_render_500_localizes_to_thai():
    html_out = layout.render_500("th")
    assert "เกิดข้อผิดพลาด" in html_out


# ===========================================================================
# redirect_with_flash: POST-redirect-GET helper, #flash fragment.
# ===========================================================================


def test_redirect_with_flash_carries_the_flash_fragment_and_query():
    resp = layout.redirect_with_flash("/users", ok="approve", chat="U123")
    assert resp.status == 303
    assert resp.location.endswith("#flash")
    assert "ok=approve" in resp.location
    assert "chat=U123" in resp.location


def test_redirect_with_flash_with_no_params_still_carries_the_fragment():
    resp = layout.redirect_with_flash("/quota")
    assert resp.location == "/quota#flash"


# ===========================================================================
# The stylesheet itself.
# ===========================================================================


def test_portal_css_stays_reasonably_close_to_the_8kb_budget():
    # 8,160 bytes verbatim + one @font-face rule (E2, option b, adopted) --
    # allow generous headroom rather than pin an exact byte count.
    size = len(layout.PORTAL_CSS.encode("utf-8"))
    assert size < 9000, f"PORTAL_CSS grew to {size} bytes -- still meant to be a single small <style> block"


def test_portal_css_braces_are_balanced():
    assert layout.PORTAL_CSS.count("{") == layout.PORTAL_CSS.count("}")


def test_portal_css_carries_the_font_face_fallback_route():
    assert "@font-face" in layout.PORTAL_CSS
    assert layout.THAI_FONT_ROUTE in layout.PORTAL_CSS
    assert 'local("Noto Sans Thai")' in layout.PORTAL_CSS


def test_portal_css_declares_the_focus_ring_and_reduced_motion_is_absent():
    assert ":focus{outline:2px solid var(--ink);outline-offset:2px}" in layout.PORTAL_CSS
    assert "prefers-reduced-motion" not in layout.PORTAL_CSS  # UI.md §1: zero motion exists, so nothing to guard
    assert "prefers-color-scheme" not in layout.PORTAL_CSS  # UI.md §7: light-only, deliberately
