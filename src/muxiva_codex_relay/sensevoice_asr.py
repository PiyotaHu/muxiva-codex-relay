from __future__ import annotations

from pathlib import Path
import threading


class SenseVoiceAsrError(RuntimeError):
    pass


class SenseVoiceAsr:
    """Cross-platform local SenseVoiceSmall INT8 ASR with inverse text normalization."""

    def __init__(self, model_dir: Path, num_threads: int = 2, language: str = "zh"):
        self.model_dir = model_dir
        self.num_threads = max(1, num_threads)
        self.language = language
        self._recognizer = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return (self.model_dir / "model.int8.onnx").is_file() and (self.model_dir / "tokens.txt").is_file()

    def _load(self):
        if self._recognizer is not None:
            return self._recognizer
        if not self.configured:
            raise SenseVoiceAsrError(f"SenseVoice 模型不完整：{self.model_dir}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise SenseVoiceAsrError("缺少 sherpa-onnx，请安装 relay 的 local-asr 依赖") from exc
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(self.model_dir / "model.int8.onnx"),
            tokens=str(self.model_dir / "tokens.txt"),
            num_threads=self.num_threads,
            language=self.language,
            use_itn=True,
            provider="cpu",
        )
        return self._recognizer

    def transcribe(self, pcm: bytes) -> str:
        if not pcm or len(pcm) % 2:
            raise ValueError("audio must be non-empty PCM s16le")
        try:
            import numpy as np
        except ImportError as exc:
            raise SenseVoiceAsrError("缺少 numpy，请安装 relay 的 local-asr 依赖") from exc
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        with self._lock:
            recognizer = self._load()
            stream = recognizer.create_stream()
            stream.accept_waveform(16000, samples)
            recognizer.decode_stream(stream)
            text = stream.result.text.strip()
        if not text:
            raise SenseVoiceAsrError("SenseVoice 没有返回有效文字")
        return text
