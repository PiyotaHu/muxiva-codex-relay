import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace

from muxiva_codex_relay.ble_transport import BleCodexTransport, MAX_STATUS_FRAME_BYTES


@dataclass
class Job:
    id: str


class FakeDispatcher:
    def __init__(self) -> None:
        self.received: list[tuple[str, str, str | None]] = []

    def enqueue(self, transcript: str, source: str, request_id: str | None = None) -> Job:
        self.received.append((transcript, source, request_id))
        return Job(request_id or "generated")

    def preview_audio(self, audio: bytes, source: str, request_id: str) -> SimpleNamespace:
        self.preview = (audio, source, request_id)
        return SimpleNamespace(id=request_id, transcript="清洗后的任务", normalizer="sensevoice")

    def confirm_preview(self, request_id: str) -> None:
        self.confirmed = request_id

    def cancel_preview(self, request_id: str) -> None:
        self.cancelled = request_id


def test_ble_reassembles_control_without_shared_token() -> None:
    dispatcher = FakeDispatcher()
    transport = BleCodexTransport("Muxiva-RLCD", dispatcher)  # type: ignore[arg-type]
    transport.feed_control_notification(b'{"type":"transcript","trans')
    transport.feed_control_notification(b'cript":"run tests","request_id":"r1"}\n')
    assert dispatcher.received == [("run tests", "esp32-ble", "r1")]


class FakeBleClient:
    is_connected = True
    mtu_size = 128

    def __init__(self) -> None:
        self.writes: list[tuple[str, bytes, bool]] = []

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self.writes.append((uuid, bytes(data), response))


def test_ble_status_writes_respect_negotiated_mtu() -> None:
    transport = BleCodexTransport("Muxiva-RLCD", FakeDispatcher())  # type: ignore[arg-type]
    client = FakeBleClient()
    transport._client = client

    asyncio.run(transport._write_status(b"x" * 250))

    assert [len(data) for _, data, _ in client.writes] == [125, 125]
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

    assert len(frame) <= MAX_STATUS_FRAME_BYTES
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

    assert len(frame) <= MAX_STATUS_FRAME_BYTES
    assert json.loads(frame)["all_agents"][0]["agent_id"] == "thread-1"


def test_ble_audio_frames_are_reassembled_for_preview(monkeypatch) -> None:
    dispatcher = FakeDispatcher()
    transport = BleCodexTransport("Muxiva-RLCD", dispatcher)  # type: ignore[arg-type]
    events: list[dict] = []
    monkeypatch.setattr(transport, "_publish_event", events.append)

    class InlineThread:
        def __init__(self, *, target, args, **_kwargs) -> None:
            self.target = target
            self.args = args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr("muxiva_codex_relay.ble_transport.threading.Thread", InlineThread)
    transport.feed_control_notification(b'{"type":"audio_start","request_id":"voice-1"}\n')
    transport.feed_audio_notification(b"\x01\x02")
    transport.feed_audio_notification(b"\x03\x04")
    transport.feed_control_notification(b'{"type":"audio_end","request_id":"voice-1"}\n')

    assert dispatcher.preview == (b"\x01\x02\x03\x04", "esp32-ble", "voice-1")
    assert events == [{
        "type": "preview_result",
        "ok": True,
        "request_id": "voice-1",
        "transcript": "清洗后的任务",
        "normalizer": "sensevoice",
    }]


def test_unique_ble_device_connects_and_is_remembered(tmp_path: Path) -> None:
    selection = tmp_path / "ble-device.json"
    transport = BleCodexTransport("Muxiva-RLCD", FakeDispatcher(), selection)  # type: ignore[arg-type]
    device = SimpleNamespace(name="Muxiva-RLCD", address="AA:BB")

    assert asyncio.run(transport._select_device([device])) is device
    assert json.loads(selection.read_text(encoding="utf-8"))["device_id"] == "AA:BB"


def test_background_start_uses_confirmed_device_when_multiple(tmp_path: Path, monkeypatch) -> None:
    selection = tmp_path / "ble-device.json"
    selection.write_text('{"device_id":"second"}', encoding="utf-8")
    transport = BleCodexTransport("Muxiva-RLCD", FakeDispatcher(), selection)  # type: ignore[arg-type]
    first = SimpleNamespace(name="Muxiva-RLCD", address="first")
    second = SimpleNamespace(name="Muxiva-RLCD", address="second")
    monkeypatch.setattr(
        "muxiva_codex_relay.ble_transport.sys.stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    assert asyncio.run(transport._select_device([first, second])) is second
