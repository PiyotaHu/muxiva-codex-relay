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

    def enqueue(self, transcript: str, source: str) -> Job:
        if not transcript:
            raise ValueError("transcript is empty")
        self.received.append((transcript, source))
        return Job("job-1")

    def snapshot(self) -> dict[str, object]:
        return {"stage": "idle"}


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
