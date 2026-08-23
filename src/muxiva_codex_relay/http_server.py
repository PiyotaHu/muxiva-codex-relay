from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import queue
from typing import Any

from .dispatcher import TaskDispatcher


class RelayHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, dispatcher: TaskDispatcher):
        self.token = token
        self.dispatcher = dispatcher
        super().__init__(address, RelayRequestHandler)


class RelayRequestHandler(BaseHTTPRequestHandler):
    server: RelayHttpServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": "muxiva-codex-relay"})
            return
        if self.path == "/v1/status":
            if not self._authorized():
                return
            self._json(HTTPStatus.OK, self.server.dispatcher.snapshot())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/transcripts":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > 64 * 1024:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        try:
            payload: dict[str, Any] = json.loads(self.rfile.read(length))
            transcript = str(payload.get("transcript", "")).strip()
            source = str(payload.get("source", "esp32"))[:64]
            request_id = str(payload.get("request_id", "")).strip() or None
            job = self.server.dispatcher.enqueue(transcript, source, request_id)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except queue.Full:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "task queue is full"})
            return
        self._json(HTTPStatus.ACCEPTED, {"accepted": True, "job_id": job.id})

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        if hmac.compare_digest(supplied, expected):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return False

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return
