#!/usr/bin/env bash
# SPEC-LINE.md §4 R-D2/R-D3 (Module D, branch line-version): idempotent VPS
# bootstrap. Safe to re-run (e.g. after `git pull`) -- every step checks
# before it acts, so an existing .env/config.toml is never clobbered, and
# re-installs are no-ops where the target already exists.
#
# Usage (as a sudo-capable user, from the repo root on the target VPS):
#   bash deploy/setup.sh
#
# What this does NOT do (deliberately -- see docs/DEPLOY-LINE.md for the
# full step-by-step runbook, including the LINE Developers console steps):
#   - Does not fill in .env secrets (LINE_CHANNEL_ACCESS_TOKEN etc.) -- those
#     come from the LINE Developers console, a manual step only you can do.
#   - Does not run `tailscale up` / authenticate Tailscale -- that's
#     interactive and browser-based. Prints the funnel command instead
#     (SPEC-LINE.md §4 R-D3: "Funnel ... steps are documentation, not code").
#   - Does not start the main bot service -- enables it (so it starts on
#     next boot) but leaves the first `systemctl start` to you, after .env
#     is filled in.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${HABIT_ASSISTANT_USER:-habitbot}"
UNIT_DIR="/etc/systemd/system"

log() { echo "[setup.sh] $*"; }

# --- 1. OS packages: python3-venv/pip (+ dev headers, for any source-built
#     wheel matplotlib/[charts] might need) ---------------------------------
if command -v apt-get >/dev/null 2>&1; then
    log "Installing python3-venv / python3-pip via apt (idempotent)..."
    sudo apt-get update -qq
    sudo apt-get install -y --no-install-recommends python3-venv python3-pip python3-dev build-essential
else
    log "apt-get not found -- skipping OS package install. Ensure python3 (>=3.11), the venv module, and pip are already installed."
fi

# --- 2. Dedicated, unprivileged service user (matches the systemd units'
#     User=/Group=habitbot, R-D1) -------------------------------------------
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    log "Creating system user '$SERVICE_USER'..."
    NOLOGIN_SHELL="$(command -v nologin || echo /usr/sbin/nologin)"
    sudo useradd --system --home-dir "$REPO_ROOT" --shell "$NOLOGIN_SHELL" "$SERVICE_USER"
else
    log "System user '$SERVICE_USER' already exists -- skipping."
fi

# --- 3. Python venv (idempotent) --------------------------------------------
if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    log "Creating venv at $REPO_ROOT/.venv (Python >=3.11 required, SPEC-LINE.md §4 R-D3)..."
    python3 -m venv "$REPO_ROOT/.venv"
else
    log "venv already present at $REPO_ROOT/.venv -- skipping creation."
fi

# --- 4. Install the app + the [charts] extra -------------------------------
# aiohttp is already a base [project.dependencies] entry (SPEC-LINE.md §4
# R-S8) -- plain `pip install -e ".[charts]"` pulls it in with everything
# else; no separate `pip install aiohttp` is needed. Re-running this is
# safe/idempotent: pip no-ops on an unchanged tree and upgrades in place
# when the source (or pyproject.toml) has changed.
log "Installing habit-assistant + [charts] extra into the venv..."
"$REPO_ROOT/.venv/bin/pip" install --upgrade pip --quiet
"$REPO_ROOT/.venv/bin/pip" install -e "$REPO_ROOT[charts]" --quiet

# --- 5. .env from the LINE template (never overwrite an existing .env) -----
if [ ! -f "$REPO_ROOT/.env" ]; then
    log "Copying .env.line.example -> .env (fill in the LINE_* secrets before starting the service!)."
    cp "$REPO_ROOT/.env.line.example" "$REPO_ROOT/.env"
    chmod 600 "$REPO_ROOT/.env"
else
    log ".env already exists -- leaving it untouched."
fi

# --- 6. config.toml from the LINE template (never overwrite an existing,
#     possibly hand-edited config.toml) --------------------------------------
# config.toml is TRACKED in git (the repo's Telegram-flavored default,
# R-S2's `channel.type` default of "telegram" -- it has no [channel]
# section at all), so on a fresh clone the old `[ ! -f config.toml ]`
# guard never fired and a LINE deploy silently kept running the Telegram
# config (hotfix v1.0.2, found live on the VPS). A real LINE config
# always has `type = "line"` under `[channel]` (config.toml.line sets
# it, and it's the only place that string appears) -- its absence means
# this config.toml is still Telegram-flavored and needs replacing; its
# presence means an operator has already installed/hand-edited a real
# LINE config, which must never be clobbered.
if [ ! -f "$REPO_ROOT/config.toml" ]; then
    log "Copying config.toml.line -> config.toml."
    cp "$REPO_ROOT/config.toml.line" "$REPO_ROOT/config.toml"
elif [ -f "$REPO_ROOT/config.toml.line" ] && ! grep -q '^type = "line"' "$REPO_ROOT/config.toml"; then
    log "config.toml exists but is still Telegram-flavored (no [channel] type = \"line\") -- backing it up to config.toml.telegram.bak and installing config.toml.line."
    cp "$REPO_ROOT/config.toml" "$REPO_ROOT/config.toml.telegram.bak"
    cp "$REPO_ROOT/config.toml.line" "$REPO_ROOT/config.toml"
else
    log "config.toml already exists -- leaving it untouched."
fi

# --- 7. data/ (SQLite + WAL + tokened media PNGs) writable by the service
#     user (R-D4) -------------------------------------------------------------
mkdir -p "$REPO_ROOT/data" "$REPO_ROOT/data/media" "$REPO_ROOT/data/backups"
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$REPO_ROOT"
log "chown -R $SERVICE_USER:$SERVICE_USER $REPO_ROOT done."

# --- 8. Install + enable the systemd units (does not start the main bot) ---
log "Installing systemd units to $UNIT_DIR..."
sudo cp "$REPO_ROOT/deploy/habit-assistant-line.service" "$UNIT_DIR/"
sudo cp "$REPO_ROOT/deploy/habit-assistant-line-backup.service" "$UNIT_DIR/"
sudo cp "$REPO_ROOT/deploy/habit-assistant-line-backup.timer" "$UNIT_DIR/"
sudo systemctl daemon-reload
sudo systemctl enable habit-assistant-line.service
sudo systemctl enable --now habit-assistant-line-backup.timer
log "Units enabled. habit-assistant-line.service is NOT started yet -- fill in .env first, then run:"
log "    sudo systemctl start habit-assistant-line.service"

# --- 9. Tailscale Funnel: print the command, don't run it (R-D3: this is
#     documentation, not code -- it also requires an already-authenticated
#     `tailscale up`, which is interactive/browser-based) -------------------
BIND_PORT="$(sed -n 's/^bind_port *= *\([0-9]*\).*/\1/p' "$REPO_ROOT/config.toml" 2>/dev/null | head -1)"
BIND_PORT="${BIND_PORT:-8080}"
if command -v tailscale >/dev/null 2>&1; then
    log "Tailscale is installed. Once 'tailscale up' has been run and the service is started, expose it with:"
else
    log "Tailscale not found. Install it (https://tailscale.com/download/linux), run 'tailscale up', then expose the bot with:"
fi
log "    sudo tailscale funnel --bg $BIND_PORT"
log "This makes https://<host>.<tailnet>.ts.net/ publicly reachable, forwarding to 127.0.0.1:$BIND_PORT."
log ""

# --- 10. Auto-fill [line].public_base_url from Tailscale (Archi rider,
#     live incident 2026-08-31: the CHANGE-ME placeholder shipped once and
#     /wrapped + /heatmap silently sent images LINE could never fetch) ----
# config.toml.line ships a literal "CHANGE-ME" placeholder for
# public_base_url on purpose (test_config_toml_line_public_base_url_is_a_
# placeholder_to_edit's own intentional contract) -- an obvious, unmistakable
# value so a forgotten step stands out. "Obvious" only helps if someone's
# actually watching, though, so this step fills it in automatically from the
# box's own Tailscale identity when it can. Fail-soft throughout: any
# missing piece (no tailscale, `status --json` failing, no DNS name yet, no
# python to parse JSON with) leaves the placeholder exactly as-is and logs
# LOUDLY instead of guessing or crashing setup. Runs after step 9 so
# `tailscale status` reflects whatever the operator already ran `tailscale
# up`/`tailscale funnel` with, if anything -- this step never runs either.
if [ -f "$REPO_ROOT/config.toml" ] && grep -q 'public_base_url = "https://CHANGE-ME' "$REPO_ROOT/config.toml" 2>/dev/null; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
    if [ ! -x "$PYTHON_BIN" ]; then
        PYTHON_BIN="$(command -v python3 || true)"
    fi
    if command -v tailscale >/dev/null 2>&1 && [ -n "$PYTHON_BIN" ]; then
        DNS_NAME="$(tailscale status --json 2>/dev/null | "$PYTHON_BIN" -c '
import json, sys
try:
    data = json.load(sys.stdin)
    print(data.get("Self", {}).get("DNSName", "").rstrip("."))
except Exception:
    print("")
' 2>/dev/null || true)"
        if [ -n "$DNS_NAME" ]; then
            NEW_URL="https://$DNS_NAME"
            sed -i "s#^public_base_url = \"https://CHANGE-ME.*\$#public_base_url = \"$NEW_URL\"#" "$REPO_ROOT/config.toml"
            log "Auto-filled [line].public_base_url = $NEW_URL (from tailscale status --json)."
        else
            log "WARNING: [line].public_base_url in config.toml is STILL the CHANGE-ME placeholder -- 'tailscale status --json' didn't return a usable DNS name. Set it by hand (docs/DEPLOY-LINE.md) before starting the service, or media links (heatmap/wrapped) will silently break."
        fi
    else
        log "WARNING: [line].public_base_url in config.toml is STILL the CHANGE-ME placeholder -- tailscale (or python3) isn't available to auto-fill it. Set it by hand (docs/DEPLOY-LINE.md) before starting the service, or media links (heatmap/wrapped) will silently break."
    fi
else
    log "[line].public_base_url is already configured (not the CHANGE-ME placeholder) -- leaving config.toml untouched."
fi

# --- 11. Tailscale Serve for the admin portal: print the command, don't
#     run it (R-D3's own "documentation, not code" posture, same as step
#     9's Funnel line) -- ONLY when [portal] enabled = true in
#     config.toml. NEVER print `tailscale funnel` for this port:
#     SPEC-LINE-PORTAL.md's own Security boundary decision requires
#     `tailscale serve` (tailnet-only) -- Funnel would put every admin
#     function on the public internet. ---------------------------------
PORTAL_SECTION="$(awk '/^\[portal\]/{f=1;next} /^\[/{f=0} f' "$REPO_ROOT/config.toml" 2>/dev/null || true)"
if [ -n "$PORTAL_SECTION" ] && echo "$PORTAL_SECTION" | grep -qE '^enabled *= *true'; then
    PORTAL_PORT="$(echo "$PORTAL_SECTION" | sed -n 's/^bind_port *= *\([0-9]*\).*/\1/p' | head -1)"
    PORTAL_PORT="${PORTAL_PORT:-8081}"
    log "[portal] enabled = true in config.toml. Expose the admin portal to your TAILNET ONLY (never Funnel this port):"
    log "    sudo tailscale serve --bg $PORTAL_PORT"
    log "This makes https://<host>.<tailnet>.ts.net:$PORTAL_PORT reachable from your OWN tailnet devices only, forwarding to 127.0.0.1:$PORTAL_PORT."
    log "WARNING: do NOT run 'tailscale funnel $PORTAL_PORT' -- that would expose every admin function to the public internet."
else
    log "[portal] is not enabled in config.toml -- skipping the admin portal's 'tailscale serve' step (see docs/DEPLOY-LINE.md if you want to turn it on)."
fi

log "Full runbook (LINE console webhook registration, rich-menu image, verification checklist): docs/DEPLOY-LINE.md"
log "setup.sh done."
