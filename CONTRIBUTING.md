# Contributing

Thanks for helping improve muxiva-codex-relay.

1. Create a branch from `main`.
2. Keep secrets, Codex credentials, downloaded models, and local runtime binaries out of commits.
3. Run `python -m pytest` before opening a pull request.
4. Describe protocol or configuration changes in `README.md` and add regression tests.

Please keep the relay provider-neutral at its boundaries: ESP32 transport, transcript normalization,
and Codex control should remain separate modules.
