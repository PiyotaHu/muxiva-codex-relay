import pytest

from muxiva_codex_relay.qwen_asr import QwenRealtimeAsr


def test_qwen_asr_session_uses_server_vad() -> None:
    asr = QwenRealtimeAsr("key", "workspace", "qwen3-asr-flash-realtime")
    event = asr._session_update()
    session = event["session"]
    assert session["sample_rate"] == 16000
    assert session["turn_detection"]["type"] == "server_vad"


def test_qwen_asr_uses_workspace_specific_endpoint() -> None:
    asr = QwenRealtimeAsr("key", "workspace", "qwen3-asr-flash-realtime")
    assert asr.workspace_endpoint == (
        "wss://workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen3-asr-flash-realtime"
    )


def test_qwen_asr_requires_even_pcm() -> None:
    asr = QwenRealtimeAsr("", "", "model")
    with pytest.raises(ValueError, match="PCM"):
        asr.transcribe(b"\x00")
