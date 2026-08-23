from pathlib import Path
import time

from muxiva_codex_relay.codex_client import CodexProtocolError
from muxiva_codex_relay.dispatcher import TaskDispatcher
from muxiva_codex_relay.normalizer import NormalizationResult


class FakeNormalizer:
    def normalize(self, text: str) -> NormalizationResult:
        return NormalizationResult(text.replace("um ", ""), "test-normalizer")


class FakeCodex:
    def __init__(self) -> None:
        self.received: list[str] = []
        self.listeners = []

    def add_listener(self, listener) -> None:
        self.listeners.append(listener)

    def submit_task(
        self, text: str, target: str, cwd: Path, sandbox: str, approval: str,
        client_message_id: str | None = None,
    ):
        self.received.append(text)
        return {"threadId": "thread-1", "turn": {"id": "turn-1"}}

    def complete(self, status: str = "completed", error=None) -> None:
        event = {
            "method": "turn/completed",
            "params": {"turn": {"id": "turn-1", "status": status, "error": error}},
        }
        for listener in self.listeners:
            listener(event)


class FakeAsr:
    def transcribe(self, audio: bytes) -> str:
        assert audio == b"\x01\x00" * 4
        return "修复登录测试"


class FakeQueuedCodex(FakeCodex):
    def __init__(self) -> None:
        super().__init__()
        self.start_attempts = 0

    def submit_task(
        self, text: str, target: str, cwd: Path, sandbox: str, approval: str,
        client_message_id: str | None = None,
    ):
        self.received.append(text)
        return {
            "threadId": "thread-1",
            "turn": {},
            "queued": True,
            "queuedSubmissionId": "queued-1",
        }

    def start_queued_task(self, thread_id: str, queued_submission_id: str):
        self.start_attempts += 1
        if self.start_attempts == 1:
            raise CodexProtocolError("active turn")
        return {"id": "turn-queued"}


def test_dispatcher_normalizes_and_submits() -> None:
    codex = FakeCodex()
    dispatcher = TaskDispatcher(
        codex, FakeNormalizer(), "latest", Path.cwd(), "workspace-write", "never"  # type: ignore[arg-type]
    )
    dispatcher.start()
    dispatcher.enqueue("um fix the login test", "test")
    deadline = time.time() + 2
    while time.time() < deadline and dispatcher.snapshot()["stage"] != "running":
        time.sleep(0.01)
    dispatcher.stop()
    assert codex.received == ["fix the login test"]
    assert dispatcher.snapshot()["thread_id"] == "thread-1"


def test_dispatcher_deduplicates_transport_retries() -> None:
    codex = FakeCodex()
    dispatcher = TaskDispatcher(
        codex, FakeNormalizer(), "latest", Path.cwd(), "workspace-write", "never"  # type: ignore[arg-type]
    )
    first = dispatcher.enqueue("fix it", "wifi", "request-1")
    second = dispatcher.enqueue("fix it", "ble", "request-1")
    assert first is second
    assert dispatcher.snapshot()["queue_size"] == 1


def test_dispatcher_transcribes_audio_before_normalizing() -> None:
    codex = FakeCodex()
    dispatcher = TaskDispatcher(
        codex,
        FakeNormalizer(),
        "latest",
        Path.cwd(),
        "workspace-write",
        "never",
        FakeAsr(),  # type: ignore[arg-type]
    )
    dispatcher.start()
    dispatcher.enqueue_audio(b"\x01\x00" * 4, "esp32")
    deadline = time.time() + 2
    while time.time() < deadline and dispatcher.snapshot()["stage"] != "running":
        time.sleep(0.01)
    dispatcher.stop()
    assert codex.received == ["修复登录测试"]


def test_dispatcher_closes_running_state_on_codex_completion() -> None:
    codex = FakeCodex()
    dispatcher = TaskDispatcher(
        codex, FakeNormalizer(), "latest", Path.cwd(), "workspace-write", "never"  # type: ignore[arg-type]
    )
    dispatcher.start()
    dispatcher.enqueue("fix it", "test")
    deadline = time.time() + 2
    while time.time() < deadline and dispatcher.snapshot()["stage"] != "running":
        time.sleep(0.01)
    codex.complete()
    dispatcher.stop()
    assert dispatcher.snapshot()["stage"] == "completed"
    assert dispatcher.snapshot()["detail"] == "Codex 任务已完成"


def test_dispatcher_reports_codex_turn_failure() -> None:
    codex = FakeCodex()
    dispatcher = TaskDispatcher(
        codex, FakeNormalizer(), "latest", Path.cwd(), "workspace-write", "never"  # type: ignore[arg-type]
    )
    dispatcher.start()
    dispatcher.enqueue("fix it", "test")
    deadline = time.time() + 2
    while time.time() < deadline and dispatcher.snapshot()["stage"] != "running":
        time.sleep(0.01)
    codex.complete("failed", {"message": "network unavailable"})
    dispatcher.stop()
    assert dispatcher.snapshot()["stage"] == "failed"
    assert "network unavailable" in str(dispatcher.snapshot()["detail"])


def test_dispatcher_keeps_busy_session_task_queued_until_codex_can_start_it() -> None:
    codex = FakeQueuedCodex()
    dispatcher = TaskDispatcher(
        codex,  # type: ignore[arg-type]
        FakeNormalizer(),
        "latest",
        Path.cwd(),
        "workspace-write",
        "never",
        queue_retry_seconds=0.01,
        queue_wait_seconds=1,
    )
    dispatcher.start()
    dispatcher.enqueue("follow-up task", "test", "voice-job-1")
    deadline = time.time() + 2
    saw_queued = False
    while time.time() < deadline and dispatcher.snapshot()["stage"] != "running":
        saw_queued = saw_queued or dispatcher.snapshot()["stage"] == "queued"
        time.sleep(0.005)
    dispatcher.stop()

    assert saw_queued
    assert codex.start_attempts == 2
    assert dispatcher.snapshot()["stage"] == "running"
    assert dispatcher.snapshot()["thread_id"] == "thread-1"
