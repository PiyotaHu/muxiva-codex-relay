from pathlib import Path
import time

from muxiva_codex_relay.dispatcher import TaskDispatcher
from muxiva_codex_relay.normalizer import NormalizationResult


class FakeNormalizer:
    def normalize(self, text: str) -> NormalizationResult:
        return NormalizationResult(text.replace("um ", ""), "test-normalizer")


class FakeCodex:
    def __init__(self) -> None:
        self.received: list[str] = []

    def submit_task(self, text: str, target: str, cwd: Path, sandbox: str, approval: str):
        self.received.append(text)
        return {"threadId": "thread-1", "turn": {}}


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
