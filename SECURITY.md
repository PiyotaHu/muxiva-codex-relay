# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories instead of a public issue.

## Deployment guidance

- Keep `.env`, local model files, device-selection state and Codex credentials out of commits.
- The relay exposes no HTTP listener. Keep Bluetooth disabled when the device integration is not in use.
- Keep Codex at `workspace-write` unless broader access is intentionally required.
- The relay never needs Codex account cookies or API tokens; it launches the locally authenticated Codex CLI.
