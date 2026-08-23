#!/usr/bin/env bash
set -u

repo="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$repo/src${PYTHONPATH:+:$PYTHONPATH}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.volta/bin:$PATH"
cd "$repo"

if [[ -n "${MUXIVA_PYTHON:-}" ]]; then
  python_bin="$MUXIVA_PYTHON"
elif [[ -x "$repo/.venv/bin/python" ]]; then
  python_bin="$repo/.venv/bin/python"
else
  python_bin="$(command -v python3 || true)"
fi
if [[ -z "$python_bin" || ! -x "$python_bin" ]]; then
  printf 'Python 3.11+ was not found; run scripts/install-macos.sh first\n' >&2
  exit 1
fi

while true; do
  "$python_bin" -m muxiva_codex_relay
  code=$?
  printf 'muxiva-codex-relay exited with code %s; restarting in 5 seconds\n' "$code" >&2
  sleep 5
done
