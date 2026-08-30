#!/usr/bin/env bash
# SPEC-LINE.md §4 R-D2 (Module D, branch line-version): manual/dev launcher
# -- venv activate + run in the foreground. NOT used by systemd (see
# habit-assistant-line.service, which calls the venv's python directly);
# this is for a one-off foreground run while testing/debugging, or a
# screen/tmux session on a box that isn't running systemd yet.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="$REPO_ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    echo "run.sh: venv not found at $REPO_ROOT/.venv -- run deploy/setup.sh first." >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "run.sh: $REPO_ROOT/.env not found -- copy .env.line.example to .env and fill in" >&2
    echo "        LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / LINE_OWNER_USER_ID." >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/config.toml" ]; then
    echo "run.sh: $REPO_ROOT/config.toml not found -- copy config.toml.line to config.toml first." >&2
    exit 1
fi

exec "$PYTHON" -m habit_assistant.main "$@"
