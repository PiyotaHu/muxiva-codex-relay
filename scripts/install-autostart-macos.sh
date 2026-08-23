#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
launcher="$repo/scripts/start-relay.sh"
label="com.muxiva.codex-relay"
plist="$HOME/Library/LaunchAgents/$label.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$repo/runtime"
chmod +x "$launcher" "$repo/scripts/start-s1-mini.sh"
cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$launcher</string></array>
  <key>WorkingDirectory</key><string>$repo</string>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$repo/runtime/relay.stdout.log</string>
  <key>StandardErrorPath</key><string>$repo/runtime/relay.stderr.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
printf 'Installed and started %s\n' "$label"

if [[ "${MUXIVA_S1_AUTOSTART:-0}" =~ ^(1|true|yes|on)$ ]]; then
  s1_label="com.muxiva.s1-mini"
  s1_launcher="$repo/scripts/start-s1-mini.sh"
  s1_plist="$HOME/Library/LaunchAgents/$s1_label.plist"
  cat >"$s1_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$s1_label</string>
  <key>ProgramArguments</key><array><string>$s1_launcher</string></array>
  <key>WorkingDirectory</key><string>$repo</string>
  <key>ProcessType</key><string>Background</string>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$repo/runtime/s1-mini.stdout.log</string>
  <key>StandardErrorPath</key><string>$repo/runtime/s1-mini.stderr.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$s1_label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$s1_plist"
  printf 'Installed and started %s\n' "$s1_label"
fi
