#!/usr/bin/env bash
# Create the virtualenv from the hash-pinned requirements and install + start the systemd user unit.
# Idempotent; re-run after pulling changes. Does not touch the firewall or enable lingering.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install --quiet --disable-pip-version-check --require-hashes -r "$ROOT/requirements.txt"

"$ROOT/.venv/bin/python" -m unittest discover -s "$ROOT/tests" >/dev/null 2>&1 \
    || { echo "unit tests failed; run: .venv/bin/python -m unittest discover -s tests -v" >&2; exit 1; }

mkdir -p "$UNIT_DIR"
# The unit runs the service from this checkout, wherever it is
sed "s|@ROOT@|$ROOT|g" "$ROOT/systemd/smalltv-monitor.service" >"$UNIT_DIR/smalltv-monitor.service"
chmod 0644 "$UNIT_DIR/smalltv-monitor.service"
systemctl --user daemon-reload
systemctl --user enable smalltv-monitor.service
systemctl --user restart smalltv-monitor.service
sleep 2
systemctl --user --no-pager --lines=5 status smalltv-monitor.service
