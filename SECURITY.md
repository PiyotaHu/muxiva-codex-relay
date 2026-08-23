# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub Security Advisories instead of a public issue.

## Deployment guidance

- Use a long random relay token and never commit `.env`.
- Expose the relay only on a trusted local network.
- Keep Codex at `workspace-write` unless broader access is intentionally required.
- The relay never needs Codex account cookies or tokens; it launches the locally authenticated Codex CLI.
