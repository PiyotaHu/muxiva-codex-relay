from pathlib import Path

import pytest

from muxiva_codex_relay.sensevoice_asr import SenseVoiceAsr


def test_sensevoice_requires_even_pcm() -> None:
    asr = SenseVoiceAsr(Path("missing"))
    with pytest.raises(ValueError, match="PCM"):
        asr.transcribe(b"\x00")


def test_sensevoice_reports_missing_model() -> None:
    assert not SenseVoiceAsr(Path("missing")).configured
