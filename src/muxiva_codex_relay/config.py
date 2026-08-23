from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


def load_env_file(path: Path) -> None:
    """Load a minimal KEY=VALUE file without adding a runtime dependency."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] == value[-1:] and value.startswith(("'", '"')):
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class RelayConfig:
    host: str
    port: int
    relay_token: str
    esp_hub_url: str
    esp_hub_token: str
    status_interval_seconds: int
    codex_bin: str | None
    codex_target: str
    codex_cwd: Path
    codex_sandbox: str
    codex_approval_policy: str
    s1_base_url: str | None
    s1_model: str
    s1_timeout_seconds: int
    ble_enabled: bool
    ble_device_name: str
    asr_api_key: str
    asr_workspace_id: str
    asr_region: str
    asr_model: str
    asr_timeout_seconds: int
    asr_provider: str
    sensevoice_model_dir: Path
    sensevoice_threads: int

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> "RelayConfig":
        load_env_file(env_file or Path.cwd() / ".env")
        token = os.getenv("MUXIVA_RELAY_TOKEN", "").strip()
        if not token or token.startswith("replace-"):
            raise ValueError("MUXIVA_RELAY_TOKEN must be set to a non-placeholder value")
        hub_token = os.getenv("MUXIVA_ESP_HUB_TOKEN", token).strip()
        cwd = Path(os.getenv("MUXIVA_CODEX_CWD", str(Path.cwd()))).expanduser().resolve()
        if not cwd.exists():
            raise ValueError(f"MUXIVA_CODEX_CWD does not exist: {cwd}")
        s1_url = os.getenv("MUXIVA_S1_BASE_URL", "").strip().rstrip("/") or None
        return cls(
            host=os.getenv("MUXIVA_RELAY_HOST", "0.0.0.0"),
            port=_int("MUXIVA_RELAY_PORT", 8765),
            relay_token=token,
            esp_hub_url=os.getenv("MUXIVA_ESP_HUB_URL", "http://rlcd-hub.local:8080/hub"),
            esp_hub_token=hub_token,
            status_interval_seconds=max(1, _int("MUXIVA_STATUS_INTERVAL_SECONDS", 3)),
            codex_bin=os.getenv("MUXIVA_CODEX_BIN") or None,
            codex_target=os.getenv("MUXIVA_CODEX_TARGET", "latest").strip(),
            codex_cwd=cwd,
            codex_sandbox=os.getenv("MUXIVA_CODEX_SANDBOX", "workspace-write"),
            codex_approval_policy=os.getenv("MUXIVA_CODEX_APPROVAL_POLICY", "never"),
            s1_base_url=s1_url,
            s1_model=os.getenv("MUXIVA_S1_MODEL", "superwhisper/s1-mini"),
            s1_timeout_seconds=max(1, _int("MUXIVA_S1_TIMEOUT_SECONDS", 20)),
            ble_enabled=os.getenv("MUXIVA_BLE_ENABLED", "0").lower() in {"1", "true", "yes", "on"},
            ble_device_name=os.getenv("MUXIVA_BLE_DEVICE_NAME", "Muxiva-RLCD").strip(),
            asr_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
            asr_workspace_id=os.getenv("DASHSCOPE_WORKSPACE_ID", "").strip(),
            asr_region=os.getenv("MUXIVA_ASR_REGION", "cn-beijing").strip(),
            asr_model=os.getenv("MUXIVA_ASR_MODEL", "qwen3-asr-flash-realtime").strip(),
            asr_timeout_seconds=max(5, _int("MUXIVA_ASR_TIMEOUT_SECONDS", 25)),
            asr_provider=os.getenv("MUXIVA_ASR_PROVIDER", "sensevoice").strip().lower(),
            sensevoice_model_dir=Path(
                os.getenv(
                    "MUXIVA_SENSEVOICE_MODEL_DIR",
                    str(Path.cwd() / "runtime" / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"),
                )
            ).expanduser().resolve(),
            sensevoice_threads=max(1, _int("MUXIVA_SENSEVOICE_THREADS", 2)),
        )
