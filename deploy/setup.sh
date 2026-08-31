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
log "Full runbook (LINE console webhook registration, rich-menu image, verification checklist): docs/DEPLOY-LINE.md"
log "setup.sh done."
