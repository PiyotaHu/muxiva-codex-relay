#!/usr/bin/env bash
set -u

repo="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export LLAMA_ARG_CHAT_TEMPLATE_KWARGS='{"enable_thinking":false}'

llama_server="${MUXIVA_LLAMA_SERVER:-$(command -v llama-server || true)}"
if [[ -z "$llama_server" || ! -x "$llama_server" ]]; then
  printf 'llama-server was not found. Install llama.cpp with Homebrew: brew install llama.cpp\n' >&2
  exit 1
fi

while true; do
  if curl --silent --fail --max-time 2 http://127.0.0.1:8091/health >/dev/null 2>&1; then
    printf 'S1-mini by Superwhisper is already healthy at http://127.0.0.1:8091/v1\n'
    while curl --silent --fail --max-time 2 http://127.0.0.1:8091/health >/dev/null 2>&1; do
      sleep 30
    done
    continue
  fi
  "$llama_server" \
    -hf superwhisper/s1-mini-GGUF:Q4_K_M \
    --host 127.0.0.1 \
    --port 8091 \
    --jinja \
    --temp 0
  code=$?
  printf 'S1-mini exited with code %s; restarting in 5 seconds\n' "$code" >&2
  sleep 5
done
