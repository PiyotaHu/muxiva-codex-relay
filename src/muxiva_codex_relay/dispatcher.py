from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import queue
import threading
import time
import uuid

from .codex_client import CodexAppServer
from .normalizer import TranscriptNormalizer


@dataclass(slots=True)
class RelayJob:
    id: str
    transcript: str
    source: str
    created_at: float


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
    ):
        self.codex = codex
        self.normalizer = normalizer
        self.target = target
        self.cwd = cwd
        self.sandbox = sandbox
        self.approval_policy = approval_policy
        self._queue: queue.Queue[RelayJob] = queue.Queue(maxsize=8)
        self._state = RelayState(updated_at=time.time())
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="codex-dispatch", daemon=True)

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

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
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
                )
                thread_id = submitted["threadId"]
                self._set_state(
                    "running",
                    f"Codex 已接收：{normalized.text[:72]}",
                    job.id,
                    thread_id,
                    normalized.engine,
                )
            except Exception as exc:
                self._set_state("failed", f"提交失败：{exc}", job.id)
            finally:
                self._queue.task_done()
