from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import Protocol

from .codex_client import CodexAppServer
from .normalizer import NormalizationResult, TranscriptNormalizer


class AudioTranscriber(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


@dataclass(slots=True)
class RelayJob:
    id: str
    transcript: str
    source: str
    created_at: float
    audio: bytes | None = None
    normalizer: str | None = None


@dataclass(slots=True)
class PreparedPreview:
    id: str
    transcript: str
    source: str
    created_at: float
    normalizer: str


@dataclass(slots=True)
class RelayState:
    stage: str = "idle"
    detail: str = "等待语音任务"
    job_id: str | None = None
    thread_id: str | None = None
    updated_at: float = 0
    normalizer: str | None = None


class TaskDispatcher:
    def __init__(
        self,
        codex: CodexAppServer,
        normalizer: TranscriptNormalizer,
        target: str,
        cwd: Path,
        sandbox: str,
        approval_policy: str,
        asr: AudioTranscriber | None = None,
        preview_state_path: Path | None = None,
        preview_ttl_seconds: float = 600.0,
    ):
        self.codex = codex
        self.normalizer = normalizer
        self.target = target
        self.cwd = cwd
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.asr = asr
        self.preview_state_path = preview_state_path
        self.preview_ttl_seconds = preview_ttl_seconds
        self._queue: queue.Queue[RelayJob] = queue.Queue(maxsize=8)
        self._state = RelayState(updated_at=time.time())
        self._lock = threading.Lock()
        self._asr_lock = threading.Lock()
        self._normalizer_lock = threading.Lock()
        self._stop = threading.Event()
        self._active_turn_id: str | None = None
        self._previews: dict[str, PreparedPreview] = {}
        self._seen: dict[str, RelayJob] = {}
        self._load_previews()
        self._worker = threading.Thread(target=self._run, name="codex-dispatch", daemon=True)
        self.codex.add_listener(self._handle_codex_event)

    def start(self) -> None:
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, transcript: str, source: str, request_id: str | None = None) -> RelayJob:
        transcript = transcript.strip()
        if not transcript:
            raise ValueError("transcript is empty")
        job_id = (request_id or str(uuid.uuid4())).strip()[:128]
        if not job_id:
            job_id = str(uuid.uuid4())
        with self._lock:
            previous = self._seen.get(job_id)
            if previous is not None:
                return previous
            job = RelayJob(job_id, transcript, source, time.time())
            self._queue.put_nowait(job)
            self._seen[job_id] = job
            if len(self._seen) > 256:
                self._seen.pop(next(iter(self._seen)))
        self._set_state("submitting", "语音任务正在提交", job.id)
        return job

    def enqueue_audio(self, audio: bytes, source: str, request_id: str | None = None) -> RelayJob:
        if not audio or len(audio) % 2:
            raise ValueError("audio must be non-empty PCM s16le")
        job_id = (request_id or str(uuid.uuid4())).strip()[:128] or str(uuid.uuid4())
        with self._lock:
            previous = self._seen.get(job_id)
            if previous is not None:
                return previous
            job = RelayJob(job_id, "", source, time.time(), bytes(audio))
            self._queue.put_nowait(job)
            self._seen[job_id] = job
        self._set_state("transcribing", "桌面 relay 正在进行语音识别", job.id)
        return job

    def preview_audio(
        self,
        audio: bytes,
        source: str,
        request_id: str | None = None,
    ) -> PreparedPreview:
        """Transcribe and clean audio without creating a Codex task."""
        if not audio or len(audio) % 2:
            raise ValueError("audio must be non-empty PCM s16le")
        if self.asr is None:
            raise RuntimeError("桌面 relay ASR 未配置")
        preview_id = (request_id or str(uuid.uuid4())).strip()[:128] or str(uuid.uuid4())
        with self._lock:
            self._remove_expired_previews_locked()
            previous = self._previews.get(preview_id)
            if previous is not None:
                return previous

        self._set_state("transcribing", "桌面 relay 正在进行语音识别", preview_id)
        with self._asr_lock:
            transcript = self.asr.transcribe(bytes(audio))
        self._set_state("normalizing", "正在清洗语音转写", preview_id)
        with self._normalizer_lock:
            normalized = self.normalizer.normalize(transcript)
        if not normalized.text:
            self._set_state(
                "ignored",
                "只检测到口水词或空内容，未创建任务",
                preview_id,
                normalizer=normalized.engine,
            )
            raise ValueError("只检测到口水词或空内容")

        preview = PreparedPreview(
            preview_id,
            normalized.text,
            source[:64],
            time.time(),
            normalized.engine,
        )
        with self._lock:
            self._previews[preview.id] = preview
            self._remove_expired_previews_locked()
            self._save_previews_locked()
        self._set_state(
            "awaiting_confirmation",
            f"等待确认：{preview.transcript[:96]}",
            preview.id,
            normalizer=preview.normalizer,
        )
        return preview

    def confirm_preview(self, preview_id: str) -> RelayJob:
        preview_id = preview_id.strip()[:128]
        if not preview_id:
            raise ValueError("request_id is required")
        with self._lock:
            self._remove_expired_previews_locked()
            # Confirmation is idempotent: if the relay accepted the first
            # request but the HTTP response was lost, a board retry must not
            # report a false failure or enqueue the task twice.
            previous = self._seen.get(preview_id)
            if previous is not None:
                return previous
            preview = self._previews.get(preview_id)
            if preview is None:
                raise ValueError("待确认的语音已过期或不存在")
            job = RelayJob(
                preview.id,
                preview.transcript,
                preview.source,
                time.time(),
                normalizer=preview.normalizer,
            )
            self._queue.put_nowait(job)
            self._seen[job.id] = job
            if len(self._seen) > 256:
                self._seen.pop(next(iter(self._seen)))
            self._previews.pop(preview_id, None)
            self._save_previews_locked()
        self._set_state("submitting", "已确认，正在提交给 Codex", job.id, normalizer=job.normalizer)
        return job

    def cancel_preview(self, preview_id: str) -> bool:
        preview_id = preview_id.strip()[:128]
        if not preview_id:
            raise ValueError("request_id is required")
        with self._lock:
            self._remove_expired_previews_locked()
            removed = self._previews.pop(preview_id, None) is not None
            self._save_previews_locked()
        self._set_state("cancelled", "已取消，未提交 Codex", preview_id)
        return removed

    def _remove_expired_previews_locked(self) -> None:
        cutoff = time.time() - self.preview_ttl_seconds
        expired = [key for key, item in self._previews.items() if item.created_at < cutoff]
        for key in expired:
            self._previews.pop(key, None)

    def _load_previews(self) -> None:
        path = self.preview_state_path
        if path is None or not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                return
            for item in raw:
                if not isinstance(item, dict):
                    continue
                preview = PreparedPreview(
                    str(item.get("id") or "")[:128],
                    str(item.get("transcript") or "").strip(),
                    str(item.get("source") or "esp32")[:64],
                    float(item.get("created_at") or 0),
                    str(item.get("normalizer") or "asr-original"),
                )
                if preview.id and preview.transcript:
                    self._previews[preview.id] = preview
            self._remove_expired_previews_locked()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._previews.clear()

    def _save_previews_locked(self) -> None:
        path = self.preview_state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps([asdict(item) for item in self._previews.values()], ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            # A read-only runtime directory must not make voice confirmation fail.
            return

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            result = asdict(self._state)
            self._remove_expired_previews_locked()
            preview_count = len(self._previews)
        result["queue_size"] = self._queue.qsize()
        result["pending_confirmation"] = preview_count
        result["dispatcher_alive"] = self._worker.is_alive()
        diagnostics = getattr(self.codex, "diagnostics", None)
        if callable(diagnostics):
            try:
                result["codex"] = diagnostics()
            except Exception as exc:
                result["codex"] = {"running": False, "error": str(exc)}
        return result

    def _set_state(
        self,
        stage: str,
        detail: str,
        job_id: str | None = None,
        thread_id: str | None = None,
        normalizer: str | None = None,
    ) -> None:
        with self._lock:
            self._state = RelayState(stage, detail, job_id, thread_id, time.time(), normalizer)

    def _set_running(
        self,
        detail: str,
        job_id: str,
        thread_id: str,
        turn_id: str | None,
        normalizer: str,
    ) -> None:
        with self._lock:
            self._active_turn_id = turn_id
            self._state = RelayState("running", detail, job_id, thread_id, time.time(), normalizer)

    def _handle_codex_event(self, message: dict[str, object]) -> None:
        method = message.get("method")
        if method != "turn/completed":
            return
        params = message.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if not isinstance(turn, dict):
            return
        turn_id = str(turn.get("id") or "")
        status = str(turn.get("status") or "")
        with self._lock:
            if self._state.stage != "running":
                return
            if self._active_turn_id and turn_id != self._active_turn_id:
                return
            if status == "completed":
                stage, detail = "completed", "Codex 任务已完成"
            elif status == "interrupted":
                stage, detail = "interrupted", "Codex 任务已取消"
            else:
                error = turn.get("error")
                message_text = error.get("message") if isinstance(error, dict) else None
                stage, detail = "failed", f"Codex 任务失败：{message_text or status or '未知错误'}"
            self._state = RelayState(
                stage,
                detail,
                self._state.job_id,
                self._state.thread_id,
                time.time(),
                self._state.normalizer,
            )
            self._active_turn_id = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if job.audio is not None:
                    if self.asr is None:
                        raise RuntimeError("桌面 relay ASR 未配置")
                    self._set_state("transcribing", "桌面 relay 正在进行语音识别", job.id)
                    with self._asr_lock:
                        job.transcript = self.asr.transcribe(job.audio)
                    job.audio = None
                if job.normalizer is None:
                    self._set_state("normalizing", "正在清洗语音转写", job.id)
                    with self._normalizer_lock:
                        normalized = self.normalizer.normalize(job.transcript)
                else:
                    normalized = NormalizationResult(job.transcript, job.normalizer)
                if not normalized.text:
                    self._set_state("ignored", "只检测到口水词或空内容，未创建任务", job.id, normalizer=normalized.engine)
                    continue
                self._set_state("submitting", "正在提交给 Codex", job.id, normalizer=normalized.engine)
                submitted = self.codex.submit_task(
                    normalized.text,
                    self.target,
                    self.cwd,
                    self.sandbox,
                    self.approval_policy,
                    job.id,
                )
                thread_id = submitted["threadId"]
                turn = submitted.get("turn") or {}
                detail = (
                    f"已追加到当前 Codex 回答：{normalized.text[:64]}"
                    if submitted.get("steered")
                    else f"Codex 已接收：{normalized.text[:72]}"
                )
                self._set_running(
                    detail,
                    job.id,
                    thread_id,
                    str(turn.get("id") or "") or None,
                    normalized.engine,
                )
            except Exception as exc:
                self._set_state("failed", f"提交失败：{exc}", job.id)
            finally:
                self._queue.task_done()
