#!/usr/bin/env bash
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  printf 'This installer is for macOS.\n' >&2
  exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"

# launchd and freshly opened shells often omit Homebrew and user-level bins.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.volta/bin:$PATH"

python_bin="${MUXIVA_BOOTSTRAP_PYTHON:-$(command -v python3 || true)}"
if [[ -z "$python_bin" ]]; then
  printf 'Python 3.11+ is required. Install it with Homebrew or python.org.\n' >&2
  exit 1
fi
"$python_bin" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required")
PY

"$python_bin" -m venv .venv
.venv/bin/python -m pip install --upgrade pip
if [[ "${MUXIVA_BLE_ENABLED:-0}" =~ ^(1|true|yes|on)$ ]]; then
  .venv/bin/python -m pip install -e '.[ble]'
else
  .venv/bin/python -m pip install -e .
fi

if [[ "${MUXIVA_SKIP_SENSEVOICE_MODEL:-0}" != "1" ]]; then
  .venv/bin/python scripts/install-sensevoice.py
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  printf 'Created %s/.env; edit its token, ESP32 address, Codex workspace and thread.\n' "$repo"
fi

chmod +x scripts/start-relay.sh scripts/start-s1-mini.sh scripts/install-autostart-macos.sh
if ! command -v codex >/dev/null 2>&1; then
  printf 'Codex CLI is not on PATH. Install it using the official macOS installer, then sign in with: codex\n' >&2
else
  codex --version
fi

printf 'macOS relay environment is ready. Start it with ./scripts/start-relay.sh\n'
