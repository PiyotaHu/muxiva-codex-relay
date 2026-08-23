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


@dataclass(slots=True)
class NativeQueuedJob:
    job_id: str
    thread_id: str
    submission_id: str
    transcript: str
    normalizer: str


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
        self._native_pending: dict[str, NativeQueuedJob] = {}
        self._native_wakeup = threading.Event()
        self._worker = threading.Thread(target=self._run, name="codex-dispatch", daemon=True)
        self._native_worker = threading.Thread(
            target=self._run_native_queue_pump,
            name="codex-native-queue",
            daemon=True,
        )
        self.codex.add_listener(self._handle_codex_event)

    def start(self) -> None:
        self._worker.start()
        self._native_worker.start()

    def stop(self) -> None:
        self._stop.set()
        self._native_wakeup.set()

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
            native_count = len(self._native_pending)
        result["queue_size"] = self._queue.qsize() + native_count
        result["dispatcher_alive"] = self._worker.is_alive()
        result["native_queue_alive"] = self._native_worker.is_alive()
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
        if method == "thread/queue/changed":
            self._native_wakeup.set()
            return
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
        # A completed turn may have made the next persistent Codex queue item
        # runnable. Wake the independent pump; never block audio ingestion.
        self._native_wakeup.set()

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
                    if not queued_submission_id:
                        raise CodexProtocolError("Codex 返回了无效的排队任务 ID")
                    with self._lock:
                        self._native_pending[queued_submission_id] = NativeQueuedJob(
                            job.id,
                            thread_id,
                            queued_submission_id,
                            normalized.text,
                            normalized.engine,
                        )
                    self._set_state(
                        "queued",
                        "Codex 正在回答，语音任务已排队；当前回答结束后会自动提交",
                        job.id,
                        thread_id,
                        normalized.engine,
                    )
                    self._native_wakeup.set()
                    # The native queue pump owns waiting/retry. Returning here
                    # keeps this worker free to transcribe and persist every
                    # later request instead of blocking for up to 15 minutes.
                    continue
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

    def _run_native_queue_pump(self) -> None:
        """Start persistent Codex queue items without blocking ASR ingestion."""
        while not self._stop.is_set():
            self._native_wakeup.wait(self.queue_retry_seconds)
            self._native_wakeup.clear()
            if self._stop.is_set():
                return
            with self._lock:
                thread_ids = {item.thread_id for item in self._native_pending.values()}
            # Also recover queue items that survived a relay restart.
            session_thread_id = self.codex.session_thread_id
            if session_thread_id:
                thread_ids.add(session_thread_id)
            for thread_id in thread_ids:
                if self._stop.is_set():
                    return
                try:
                    queued = self.codex.list_queued_tasks(thread_id, limit=20)
                    if not queued:
                        continue
                    submission = queued[0]
                    submission_id = str(submission.get("id") or "")
                    if not submission_id:
                        continue
                    turn = self.codex.start_queued_task(thread_id, submission_id)
                except (CodexProtocolError, TimeoutError, KeyError):
                    # An active desktop turn is the normal case. Retry after
                    # the next completion notification or periodic wake-up.
                    continue

                with self._lock:
                    pending = self._native_pending.pop(submission_id, None)
                client_job_id = str(submission.get("clientUserMessageId") or submission_id)
                if pending:
                    detail = f"Codex 已接收：{pending.transcript[:72]}"
                    job_id = pending.job_id
                    normalizer = pending.normalizer
                else:
                    detail = "Codex 已开始此前排队的语音任务"
                    job_id = client_job_id
                    normalizer = "codex-native-queue"
                self._set_running(
                    detail,
                    job_id,
                    thread_id,
                    str(turn.get("id") or "") or None,
                    normalizer,
                )
                # Start at most one turn per pass. If other conversations have
                # work, the short periodic wake-up will revisit them safely.
                break

