"""SPEC-LINE-PORTAL.md §4 (shared surface, admin web portal, branch
`line-version`), dispatch item 6: `deploy/setup.sh` prints (never
executes) the `tailscale serve --bg <port>` step for the portal, and
`docs/DEPLOY-LINE.md` documents it -- both conditioned on `[portal]
enabled = true`, and both explicit that `tailscale funnel` must never be
used for this port (that's the whole security boundary, SPEC-LINE-
PORTAL.md's own "Security boundary decision" section).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_setup_sh_prints_tailscale_serve_for_the_portal_conditionally():
    text = (REPO_ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
    assert "tailscale serve --bg" in text
    assert "[portal]" in text
    assert "never Funnel this port" in text or "do NOT run" in text


def test_setup_sh_never_actually_executes_a_funnel_command():
    """Every `tailscale funnel` mention in the whole script is either
    inside a `log "..."` string (R-D3's own "documentation, not code" --
    printed, never run) or a code comment -- never a bare, executed
    `sudo tailscale funnel ...` statement. This holds for BOTH the
    pre-existing LINE webhook line (port 8080) and the new portal
    warning text (which mentions `$PORTAL_PORT` only to tell the
    operator not to funnel it)."""
    text = (REPO_ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if "tailscale funnel" not in stripped:
            continue
        assert stripped.startswith("#") or stripped.startswith('log "'), (
            f"a `tailscale funnel` mention must be a comment or inside log(...), never executed directly: {line!r}"
        )


def test_setup_sh_is_idempotent_and_read_only_toward_portal_config(tmp_path):
    """The new step only READS config.toml (grep/awk/sed for detection) --
    it must never WRITE to the [portal] section the way step 10 writes
    [line].public_base_url. A quick static check: no `sed -i` targeting
    a portal key."""
    text = (REPO_ROOT / "deploy" / "setup.sh").read_text(encoding="utf-8")
    assert 'sed -i' not in text.split("--- 11.")[1].split("log \"Full runbook")[0]


def test_deploy_line_doc_covers_the_portal_section():
    text = (REPO_ROOT / "docs" / "DEPLOY-LINE.md").read_text(encoding="utf-8")
    for must_contain in [
        "Admin portal",
        "tailscale serve --bg",
        "do NOT run",
        "tailscale funnel 8081",
        "owner_login",
        "[portal]",
    ]:
        assert must_contain in text, f"docs/DEPLOY-LINE.md missing expected admin-portal coverage of: {must_contain!r}"


def test_deploy_line_doc_covers_the_identity_header_verification_step():
    """Integration item 7: a concrete, reproducible verification step for
    the security boundary -- a header-less curl against the portal's own
    port must show 403, not a page. Not just "open it in a browser"."""
    text = (REPO_ROOT / "docs" / "DEPLOY-LINE.md").read_text(encoding="utf-8")
    for must_contain in [
        "curl -i http://127.0.0.1:8081/",
        "403",
        "Verify the security boundary",
    ]:
        assert must_contain in text, f"docs/DEPLOY-LINE.md missing expected identity-header verification coverage of: {must_contain!r}"


def test_config_toml_line_documents_the_portal_section():
    """Integration item 7 (line/v1.3.0): this deployment's own reference
    config now ships with the portal turned ON (`enabled = true`) -- the
    feature is finished and this release is what actually launches it
    for the real operator. `PortalConfig`'s own Python-level default
    (a config with no `[portal]` section at all, or an older config
    predating this release) is unaffected and stays `false` (AC1) --
    this test is specifically about THIS file's own shipped template."""
    text = (REPO_ROOT / "config.toml.line").read_text(encoding="utf-8")
    assert "[portal]" in text
    assert "enabled = true" in text
    assert "tailscale serve" in text
    # The [portal] block's own comment (not the pre-existing, unrelated
    # [line] one) only mentions funneling as an explicit warning against
    # it, never as a suggestion.
    portal_comment = "\n".join(text.split("[portal]")[0].splitlines()[-8:])
    if "tailscale funnel" in portal_comment:
        assert "NEVER" in portal_comment
