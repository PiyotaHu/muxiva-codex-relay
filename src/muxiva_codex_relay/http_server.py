from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import queue
from typing import Any, Callable

from .dispatcher import TaskDispatcher


class RelayHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        dispatcher: TaskDispatcher,
        set_display_active: Callable[[bool], None] | None = None,
    ):
        self.token = token
        self.dispatcher = dispatcher
        self.set_display_active = set_display_active or (lambda _active: None)
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
        if self.path not in {
            "/v1/transcripts",
            "/v1/audio",
            "/v1/audio/preview",
            "/v1/pending/confirm",
            "/v1/pending/cancel",
            "/v1/display",
        }:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        maximum = 1024 * 1024 if self.path in {"/v1/audio", "/v1/audio/preview"} else 64 * 1024
        if length <= 0 or length > maximum:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid content length"})
            return
        try:
            body = self.rfile.read(length)
            if self.path == "/v1/display":
                payload = json.loads(body)
                active = payload.get("active")
                if not isinstance(active, bool):
                    raise ValueError("active must be boolean")
                self.server.set_display_active(active)
                self._json(HTTPStatus.OK, {"ok": True, "active": active})
                return
            if self.path in {"/v1/audio", "/v1/audio/preview"}:
                self.server.set_display_active(True)
                source = str(self.headers.get("X-Muxiva-Source", "esp32"))[:64]
                request_id = str(self.headers.get("X-Request-Id", "")).strip() or None
                if self.path == "/v1/audio/preview":
                    preview = self.server.dispatcher.preview_audio(body, source, request_id)
                    self._json(
                        HTTPStatus.OK,
                        {
                            "ready": True,
                            "request_id": preview.id,
                            "transcript": preview.transcript,
                            "normalizer": preview.normalizer,
                        },
                    )
                    return
                job = self.server.dispatcher.enqueue_audio(body, source, request_id)
            elif self.path in {"/v1/pending/confirm", "/v1/pending/cancel"}:
                payload = json.loads(body)
                request_id = str(payload.get("request_id", "")).strip()
                if self.path == "/v1/pending/confirm":
                    job = self.server.dispatcher.confirm_preview(request_id)
                else:
                    cancelled = self.server.dispatcher.cancel_preview(request_id)
                    self._json(HTTPStatus.OK, {"cancelled": cancelled, "request_id": request_id})
                    return
            else:
                self.server.set_display_active(True)
                payload: dict[str, Any] = json.loads(body)
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
        except RuntimeError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
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
