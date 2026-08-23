from __future__ import annotations

import signal
import sys
import threading

from .codex_client import CodexAppServer, discover_codex_binary
from .config import RelayConfig
from .dispatcher import TaskDispatcher
from .http_server import RelayHttpServer
from .normalizer import TranscriptNormalizer
from .status_publisher import StatusPublisher


def run(config: RelayConfig) -> None:
    binary = discover_codex_binary(config.codex_bin)
    codex = CodexAppServer(binary)
    codex.start()
    normalizer = TranscriptNormalizer(config.s1_base_url, config.s1_model, config.s1_timeout_seconds)
    dispatcher = TaskDispatcher(
        codex,
        normalizer,
        config.codex_target,
        config.codex_cwd,
        config.codex_sandbox,
        config.codex_approval_policy,
    )
    publisher = StatusPublisher(
        codex,
        dispatcher,
        config.esp_hub_url,
        config.esp_hub_token,
        config.status_interval_seconds,
    )
    server = RelayHttpServer((config.host, config.port), config.relay_token, dispatcher)
    stop = threading.Event()

    def shutdown(*_: object) -> None:
        if stop.is_set():
            return
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    dispatcher.start()
    publisher.start()
    print(f"muxiva-codex-relay listening on http://{config.host}:{config.port}")
    print(f"Codex: {binary}")
    print("S1-mini:", config.s1_base_url or "disabled (Chinese ASR text still passes through safely)")
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        publisher.stop()
        dispatcher.stop()
        server.server_close()
        codex.close()


def main() -> None:
    try:
        config = RelayConfig.from_env()
        run(config)
    except Exception as exc:
        print(f"relay startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
