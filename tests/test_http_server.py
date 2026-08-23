from __future__ import annotations

from dataclasses import dataclass
import json
import threading
import urllib.error
import urllib.request

from muxiva_codex_relay.http_server import RelayHttpServer


@dataclass
class Job:
    id: str


class FakeDispatcher:
    def __init__(self) -> None:
        self.received: list[tuple[str, str]] = []

    def enqueue(self, transcript: str, source: str, request_id: str | None = None) -> Job:
        if not transcript:
            raise ValueError("transcript is empty")
        self.received.append((transcript, source))
        return Job("job-1")

    def snapshot(self) -> dict[str, object]:
        return {"stage": "idle"}

    def enqueue_audio(self, audio: bytes, source: str, request_id: str | None = None) -> Job:
        self.received.append((f"audio:{len(audio)}", source))
        return Job("job-audio")


def test_transcript_endpoint_requires_auth_and_accepts_json() -> None:
    dispatcher = FakeDispatcher()
    server = RelayHttpServer(("127.0.0.1", 0), "secret", dispatcher)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/v1/transcripts"
    data = json.dumps({"transcript": "fix the tests", "source": "test"}).encode()
    try:
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=2)
            raise AssertionError("unauthorized request unexpectedly succeeded")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 202
        assert dispatcher.received == [("fix the tests", "test")]
    finally:
        server.shutdown()
        server.server_close()


def test_audio_endpoint_accepts_pcm() -> None:
    dispatcher = FakeDispatcher()
    server = RelayHttpServer(("127.0.0.1", 0), "secret", dispatcher)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/v1/audio",
        data=b"\x00\x00" * 960,
        headers={
            "Authorization": "Bearer secret",
            "Content-Type": "application/octet-stream",
            "X-Muxiva-Source": "esp32-direct",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 202
        assert dispatcher.received == [("audio:1920", "esp32-direct")]
    finally:
        server.shutdown()
        server.server_close()


def test_display_endpoint_controls_status_publishing() -> None:
    dispatcher = FakeDispatcher()
    states: list[bool] = []
    server = RelayHttpServer(("127.0.0.1", 0), "secret", dispatcher, states.append)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for active in (True, False):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/display",
                data=json.dumps({"active": active}).encode(),
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 200
        assert states == [True, False]
    finally:
        server.shutdown()
        server.server_close()
