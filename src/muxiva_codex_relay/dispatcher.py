from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import queue
import threading
import time
import uuid
from typing import Protocol

from .codex_client import CodexAppServer
from .codex_client import CodexProtocolError
from .normalizer import TranscriptNormalizer


class AudioTranscriber(Protocol):
    def transcribe(self, pcm: bytes) -> str: ...


@dataclass(slots=True)
class RelayJob:
    id: str
    transcript: str
    source: str
    created_at: float
    audio: bytes | None = None


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
        queue_retry_seconds: float = 2.0,
        queue_wait_seconds: float = 900.0,
    ):
        self.codex = codex
        self.normalizer = normalizer
        self.target = target
        self.cwd = cwd
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self.asr = asr
        self.queue_retry_seconds = queue_retry_seconds
        self.queue_wait_seconds = queue_wait_seconds
        self._queue: queue.Queue[RelayJob] = queue.Queue(maxsize=8)
        self._state = RelayState(updated_at=time.time())
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active_turn_id: str | None = None
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
            previous = getattr(self, "_seen", {}).get(job_id)
            if previous is not None:
                return previous
            job = RelayJob(job_id, transcript, source, time.time())
            self._queue.put_nowait(job)
            if not hasattr(self, "_seen"):
                self._seen: dict[str, RelayJob] = {}
            self._seen[job_id] = job
            if len(self._seen) > 256:
                self._seen.pop(next(iter(self._seen)))
        self._set_state("queued", "语音任务已进入队列", job.id)
        return job

    def enqueue_audio(self, audio: bytes, source: str, request_id: str | None = None) -> RelayJob:
        if not audio or len(audio) % 2:
            raise ValueError("audio must be non-empty PCM s16le")
        job_id = (request_id or str(uuid.uuid4())).strip()[:128] or str(uuid.uuid4())
        with self._lock:
            previous = getattr(self, "_seen", {}).get(job_id)
            if previous is not None:
                return previous
            job = RelayJob(job_id, "", source, time.time(), bytes(audio))
            self._queue.put_nowait(job)
            if not hasattr(self, "_seen"):
                self._seen: dict[str, RelayJob] = {}
            self._seen[job_id] = job
        self._set_state("transcribing", "桌面 relay 正在进行语音识别", job.id)
        return job

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            result = asdict(self._state)
        result["queue_size"] = self._queue.qsize()
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
        if message.get("method") != "turn/completed":
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
                    job.transcript = self.asr.transcribe(job.audio)
                    job.audio = None
                self._set_state("normalizing", "正在清洗语音转写", job.id)
                normalized = self.normalizer.normalize(job.transcript)
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
                if submitted.get("queued"):
                    queued_submission_id = str(submitted.get("queuedSubmissionId") or "")
                    self._set_state(
                        "queued",
                        "Codex 正在回答，语音任务已排队；当前回答结束后会自动提交",
                        job.id,
                        thread_id,
                        normalized.engine,
                    )
                    turn = self._wait_for_queued_turn(thread_id, queued_submission_id)
                else:
                    turn = submitted.get("turn") or {}
                self._set_running(
                    f"Codex 已接收：{normalized.text[:72]}",
                    job.id,
                    thread_id,
                    str(turn.get("id") or "") or None,
                    normalized.engine,
                )
            except Exception as exc:
                self._set_state("failed", f"提交失败：{exc}", job.id)
            finally:
                self._queue.task_done()

    def _wait_for_queued_turn(self, thread_id: str, queued_submission_id: str) -> dict[str, object]:
        if not queued_submission_id:
            raise CodexProtocolError("Codex 返回了无效的排队任务 ID")
        deadline = time.monotonic() + self.queue_wait_seconds
        last_error = "会话仍在回答"
        while not self._stop.is_set() and time.monotonic() < deadline:
            if self._stop.wait(self.queue_retry_seconds):
                break
            try:
                return self.codex.start_queued_task(thread_id, queued_submission_id)
            except CodexProtocolError as exc:
                last_error = str(exc)
        if self._stop.is_set():
            raise CodexProtocolError("relay 已停止，排队任务保留在 Codex 队列中")
        raise CodexProtocolError(f"等待 Codex 当前回答结束超时；任务仍保留在队列中：{last_error}")
