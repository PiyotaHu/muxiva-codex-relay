from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from .codex_client import CodexAppServer, discover_codex_binary
from .ble_transport import BleCodexTransport
from .config import RelayConfig
from .dispatcher import TaskDispatcher
from .normalizer import TranscriptNormalizer
from .qwen_asr import QwenRealtimeAsr
from .sensevoice_asr import SenseVoiceAsr
from .status_publisher import StatusPublisher


def run(config: RelayConfig) -> None:
    binary = discover_codex_binary(config.codex_bin)
    codex = CodexAppServer(binary)
    codex.start()
    codex.configure_session_target(config.codex_target)
    normalizer = TranscriptNormalizer(config.s1_base_url, config.s1_model, config.s1_timeout_seconds)
    if config.asr_provider == "qwen":
        asr = QwenRealtimeAsr(
            config.asr_api_key,
            config.asr_workspace_id,
            config.asr_model,
            config.asr_timeout_seconds,
            config.asr_region,
        )
    elif config.asr_provider == "sensevoice":
        asr = SenseVoiceAsr(config.sensevoice_model_dir, config.sensevoice_threads)
    else:
        raise ValueError(f"unsupported MUXIVA_ASR_PROVIDER: {config.asr_provider}")
    dispatcher = TaskDispatcher(
        codex,
        normalizer,
        config.codex_target,
        config.codex_cwd,
        config.codex_sandbox,
        config.codex_approval_policy,
        asr,
        preview_state_path=Path("runtime/pending-previews.json"),
    )
    ble = BleCodexTransport(
        config.ble_device_name,
        dispatcher,
        selection_path=config.ble_selection_path,
    )
    publisher = StatusPublisher(
        codex,
        dispatcher,
        config.status_interval_seconds,
        ble.publish_status,
        config.codex_target,
        Path("runtime/display-active.json"),
    )
    ble.set_display_handler(publisher.set_display_active)
    stop = False

    def shutdown(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    dispatcher.start()
    ble.start()
    publisher.start()
    print("muxiva-codex-relay ready; waiting for ESP32 over Bluetooth LE")
    print(f"Codex: {binary}")
    print("S1-mini:", config.s1_base_url or "disabled (Chinese ASR text still passes through safely)")
    print("ASR:", f"{config.asr_provider} ({'ready' if asr.configured else 'model/config missing'})")
    try:
        while not stop:
            time.sleep(0.25)
    finally:
        publisher.stop()
        ble.stop()
        dispatcher.stop()
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
