from __future__ import annotations

import base64
import json
import time
import uuid
from urllib.parse import quote


class QwenAsrError(RuntimeError):
    pass


class QwenRealtimeAsr:
    """Transcribe mono 16 kHz PCM with Qwen realtime ASR on the desktop relay."""

    def __init__(
        self,
        api_key: str,
        workspace_id: str,
        model: str,
        timeout_seconds: int = 25,
        region: str = "cn-beijing",
    ):
        self.api_key = api_key.strip()
        self.workspace_id = workspace_id.strip()
        self.model = model.strip() or "qwen3-asr-flash-realtime"
        self.timeout_seconds = timeout_seconds
        self.region = region.strip()
        if self.region not in {"cn-beijing", "ap-southeast-1"}:
            raise ValueError("Qwen ASR region must be cn-beijing or ap-southeast-1")

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.workspace_id)

    @property
    def workspace_endpoint(self) -> str:
        return (
            f"wss://{self.workspace_id}.{self.region}.maas.aliyuncs.com/api-ws/v1/realtime"
            f"?model={quote(self.model, safe='-._')}"
        )

    def transcribe(self, pcm: bytes) -> str:
        if not pcm or len(pcm) % 2:
            raise ValueError("audio must be non-empty PCM s16le")
        if not self.configured:
            raise QwenAsrError("桌面 relay 未配置 Qwen ASR")
        try:
            import websocket
        except ImportError as exc:
            raise QwenAsrError("缺少 websocket-client，请重新安装 relay") from exc

        ws = websocket.create_connection(
            self.workspace_endpoint,
            header=[f"Authorization: Bearer {self.api_key}", "OpenAI-Beta: realtime=v1"],
            timeout=10,
            enable_multithread=False,
        )
        transcripts: list[str] = []
        try:
            ws.send(json.dumps(self._session_update(), separators=(",", ":")))
            ws.settimeout(0.05)
            for offset in range(0, len(pcm), 1920):
                chunk = pcm[offset : offset + 1920]
                ws.send(json.dumps(self._audio_append(chunk), separators=(",", ":")))
                self._drain(ws, transcripts)

            deadline = time.monotonic() + self.timeout_seconds
            while time.monotonic() < deadline:
                event = self._receive(ws, websocket)
                if event is None:
                    continue
                self._accept_event(event, transcripts)
                if transcripts and event.get("type") == "input_audio_buffer.speech_stopped":
                    break
            if not transcripts:
                raise QwenAsrError("Qwen ASR 没有返回有效文字")
            return transcripts[-1].strip()
        finally:
            try:
                ws.send(json.dumps({"event_id": self._event_id(), "type": "session.finish"}))
            except Exception:
                pass
            ws.close()

    def _drain(self, ws: object, transcripts: list[str]) -> None:
        import websocket

        for _ in range(12):
            event = self._receive(ws, websocket)
            if event is None:
                return
            self._accept_event(event, transcripts)

    @staticmethod
    def _receive(ws: object, websocket: object) -> dict[str, object] | None:
        try:
            value = ws.recv()
        except (websocket.WebSocketTimeoutException, TimeoutError, BlockingIOError):
            return None
        event = json.loads(value)
        if not isinstance(event, dict):
            return None
        if event.get("type") == "error":
            error = event.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else error
            raise QwenAsrError(str(message)[:512])
        return event

    @staticmethod
    def _accept_event(event: dict[str, object], transcripts: list[str]) -> None:
        kind = str(event.get("type") or "")
        if kind.endswith("input_audio_transcription.completed"):
            text = str(event.get("transcript") or event.get("text") or "").strip()
            if text:
                transcripts.append(text)
        elif kind.endswith("input_audio_transcription.failed"):
            raise QwenAsrError(str(event.get("error") or "ASR transcription failed")[:512])

    def _session_update(self) -> dict[str, object]:
        return {
            "event_id": self._event_id(),
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": 16000,
                "input_audio_transcription": {"language": "zh"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.42,
                    "silence_duration_ms": 900,
                },
            },
        }

    def _audio_append(self, pcm: bytes) -> dict[str, str]:
        return {
            "event_id": self._event_id(),
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm).decode("ascii"),
        }

    @staticmethod
    def _event_id() -> str:
        return f"event_muxiva_codex_{uuid.uuid4().hex}"
