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


@dataclass
class Preview:
    id: str
    transcript: str
    normalizer: str


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

    def preview_audio(self, audio: bytes, source: str, request_id: str | None = None) -> Preview:
        self.received.append((f"preview:{len(audio)}", source))
        return Preview(request_id or "preview-1", "修复登录测试", "test-asr")

    def confirm_preview(self, request_id: str) -> Job:
        self.received.append((f"confirm:{request_id}", "pending"))
        return Job(request_id)

    def cancel_preview(self, request_id: str) -> bool:
        self.received.append((f"cancel:{request_id}", "pending"))
        return True


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


def test_audio_preview_requires_explicit_confirmation() -> None:
    dispatcher = FakeDispatcher()
    server = RelayHttpServer(("127.0.0.1", 0), "secret", dispatcher)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    headers = {
        "Authorization": "Bearer secret",
        "Content-Type": "application/octet-stream",
        "X-Muxiva-Source": "esp32-direct",
        "X-Request-Id": "preview-42",
    }
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/audio/preview",
            data=b"\x00\x00" * 960,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
            assert response.status == 200
        assert payload["request_id"] == "preview-42"
        assert payload["transcript"] == "修复登录测试"
        assert dispatcher.received == [("preview:1920", "esp32-direct")]

        confirm = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/pending/confirm",
            data=json.dumps({"request_id": "preview-42"}).encode(),
            headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(confirm, timeout=2) as response:
            assert response.status == 202
        assert dispatcher.received[-1] == ("confirm:preview-42", "pending")
    finally:
        server.shutdown()
        server.server_close()


def test_pending_preview_can_be_cancelled() -> None:
    dispatcher = FakeDispatcher()
    server = RelayHttpServer(("127.0.0.1", 0), "secret", dispatcher)  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = urllib.request.Request(
        f"http://127.0.0.1:{server.server_port}/v1/pending/cancel",
        data=json.dumps({"request_id": "preview-42"}).encode(),
        headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = json.load(response)
            assert response.status == 200
        assert payload["cancelled"] is True
        assert dispatcher.received == [("cancel:preview-42", "pending")]
    finally:
        server.shutdown()
        server.server_close()


def test_display_endpoint_controls_status_publishing() -> None:
    dispatcher = FakeDispatcher()
    states: list[tuple[bool, bool]] = []
    server = RelayHttpServer(
        ("127.0.0.1", 0),
        "secret",
        dispatcher,
        lambda active, refresh: states.append((active, refresh)),
    )  # type: ignore[arg-type]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for active, refresh in ((True, True), (False, False)):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/display",
                data=json.dumps({"active": active, "refresh_latest": refresh}).encode(),
                headers={"Authorization": "Bearer secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                assert response.status == 200
        assert states == [(True, True), (False, False)]
    finally:
        server.shutdown()
        server.server_close()
