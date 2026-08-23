from dataclasses import dataclass

from muxiva_codex_relay.ble_transport import BleCodexTransport


@dataclass
class Job:
    id: str


class FakeDispatcher:
    def __init__(self) -> None:
        self.received: list[tuple[str, str, str | None]] = []

    def enqueue(self, transcript: str, source: str, request_id: str | None = None) -> Job:
        self.received.append((transcript, source, request_id))
        return Job(request_id or "generated")


def test_ble_reassembles_chunks_and_authenticates() -> None:
    dispatcher = FakeDispatcher()
    transport = BleCodexTransport(True, "Muxiva-RLCD", "secret", dispatcher)  # type: ignore[arg-type]
    transport.feed_notification(b'{"token":"secret","trans')
    transport.feed_notification(b'cript":"run tests","request_id":"r1"}\n')
    transport.feed_notification(b'{"token":"wrong","transcript":"ignore"}\n')
    assert dispatcher.received == [("run tests", "esp32-ble", "r1")]
