import asyncio
from dataclasses import dataclass
import json

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


class FakeBleClient:
    is_connected = True
    mtu_size = 128

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, bool]] = []

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self.writes.append((uuid, bytes(data), response))


def test_ble_status_writes_respect_negotiated_mtu() -> None:
    transport = BleCodexTransport(True, "Muxiva-RLCD", "secret", FakeDispatcher())  # type: ignore[arg-type]
    client = FakeBleClient()
    transport._client = client

    asyncio.run(transport._write_status(b"x" * 250))

    assert [len(data) for _, data, _ in client.writes] == [120, 120, 10]
    assert all(response for _, _, response in client.writes)


def test_ble_status_uses_compact_single_conversation_frame() -> None:
    payload = {
        "type": "status",
        "ts": 1,
        "latest": {"agent_id": "thread-1", "last_assistant": "回答" * 180},
        "all_agents": [{"agent_id": "thread-1", "last_user": "问题" * 32, "last_assistant": "回答" * 180}],
        "active_count": 0,
        "groups": [{"name": "PC", "items": [{"label": "Relay", "value": "idle"}]}],
    }

    frame = BleCodexTransport._encode_status(payload)
    decoded = json.loads(frame)

    assert len(frame) <= 1024
    assert "latest" not in decoded
    assert "groups" not in decoded
    assert decoded["all_agents"][0]["agent_id"] == "thread-1"


def test_ble_status_trims_oversized_user_and_metadata_without_answer() -> None:
    payload = {
        "type": "status",
        "ts": 1,
        "all_agents": [{
            "agent_id": "thread-1",
            "state": "busy",
            "detail": "状态" * 300,
            "cwd": "目录" * 300,
            "last_user": "问题" * 300,
            "last_assistant": "",
        }],
        "active_count": 1,
    }

    frame = BleCodexTransport._encode_status(payload)

    assert len(frame) <= 1024
    assert json.loads(frame)["all_agents"][0]["agent_id"] == "thread-1"
