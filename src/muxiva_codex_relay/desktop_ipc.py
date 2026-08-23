from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import uuid
from typing import Any, BinaryIO


class CodexDesktopIpcError(RuntimeError):
    pass


_METHOD_VERSIONS = {
    "thread-owner-discovery": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-steer-turn": 1,
}


def desktop_ipc_candidates() -> list[str]:
    """Return Codex Desktop IPC endpoints for Windows and macOS/Linux."""
    if os.name == "nt":
        return [r"\\.\pipe\codex-ipc"]
    codex_home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
    uid = os.getuid() if hasattr(os, "getuid") else None
    candidates = [
        codex_home / "ipc" / "ipc.sock",
        Path.home() / "Library" / "Application Support" / "Codex" / "ipc" / "ipc.sock",
        Path(tempfile.gettempdir()) / "codex-ipc" / (f"ipc-{uid}.sock" if uid else "ipc.sock"),
    ]
    return [str(item) for item in dict.fromkeys(candidates)]


class _UnixStream:
    def __init__(self, endpoint: str, timeout_seconds: float):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout_seconds)
        self.socket.connect(endpoint)

    def read(self, size: int) -> bytes:
        return self.socket.recv(size)

    def write(self, data: bytes) -> int:
        self.socket.sendall(data)
        return len(data)

    def flush(self) -> None:
        return

    def close(self) -> None:
        self.socket.close()


class CodexDesktopIpc:
    """Client for Codex Desktop's same-user thread-owner IPC router.

    This IPC is versioned by Codex Desktop but is not part of the public
    app-server API. Keep protocol versions explicit and fail closed when a
    Desktop update no longer accepts them.
    """

    def __init__(self, timeout_seconds: float = 10.0):
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()

    def find_thread_owner(self, host_id: str, conversation_id: str) -> str | None:
        with self._session() as session:
            response = session.request(
                "thread-owner-discovery",
                {"hostId": host_id, "conversationId": conversation_id},
            )
        if response.get("resultType") == "error":
            if response.get("error") == "no-client-found":
                return None
            raise CodexDesktopIpcError(str(response.get("error") or "owner discovery failed"))
        owner_id = str(response.get("handledByClientId") or "")
        return owner_id or None

    def start_turn(
        self,
        owner_id: str,
        conversation_id: str,
        text: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        params = {
            "conversationId": conversation_id,
            "turnStart": {
                "request": {
                    "threadId": conversation_id,
                    "input": [{"type": "text", "text": text}],
                    "clientUserMessageId": client_message_id,
                },
                "context": {
                    "inheritThreadSettings": True,
                    "useAppServerPermissionDefault": True,
                },
            },
        }
        return self._owner_request("thread-follower-start-turn", params, owner_id)

    def steer_turn(
        self,
        owner_id: str,
        conversation_id: str,
        text: str,
        client_message_id: str,
    ) -> dict[str, Any]:
        params = {
            "conversationId": conversation_id,
            "input": [{"type": "text", "text": text}],
            "restoreMessage": None,
            "serviceTier": None,
            "attachments": [],
            "clientUserMessageId": client_message_id,
            "additionalContext": None,
        }
        return self._owner_request("thread-follower-steer-turn", params, owner_id)

    def _owner_request(
        self,
        method: str,
        params: dict[str, Any],
        owner_id: str,
    ) -> dict[str, Any]:
        with self._session() as session:
            response = session.request(method, params, owner_id)
        if response.get("resultType") != "success":
            raise CodexDesktopIpcError(str(response.get("error") or f"{method} failed"))
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        # Desktop's follower handler wraps the underlying app-server result in
        # ``{result: ...}``. Keep the adapter's public shape equal to the
        # official app-server result so callers do not depend on IPC framing.
        nested = result.get("result")
        return nested if isinstance(nested, dict) else result

    def _session(self) -> "_IpcSessionContext":
        return _IpcSessionContext(self, self.timeout_seconds)

    def _connect(self) -> BinaryIO | _UnixStream:
        errors: list[str] = []
        for endpoint in desktop_ipc_candidates():
            try:
                if os.name == "nt":
                    return open(endpoint, "r+b", buffering=0)
                if not Path(endpoint).is_socket():
                    continue
                return _UnixStream(endpoint, self.timeout_seconds)
            except OSError as exc:
                errors.append(f"{endpoint}: {exc}")
        raise CodexDesktopIpcError("Codex Desktop IPC 不可用：" + "; ".join(errors))


class _IpcSessionContext:
    def __init__(self, owner: CodexDesktopIpc, timeout_seconds: float):
        self.owner = owner
        self.timeout_seconds = timeout_seconds
        self.stream: BinaryIO | _UnixStream | None = None
        self.client_id = "initializing-client"

    def __enter__(self) -> "_IpcSessionContext":
        self.owner._lock.acquire()
        try:
            self.stream = self.owner._connect()
            initialized = self.request("initialize", {"clientType": "muxiva-codex-relay"})
            if initialized.get("resultType") != "success":
                raise CodexDesktopIpcError(str(initialized.get("error") or "IPC initialize failed"))
            result = initialized.get("result")
            self.client_id = str(result.get("clientId") or "") if isinstance(result, dict) else ""
            if not self.client_id:
                raise CodexDesktopIpcError("Codex Desktop IPC 未返回 clientId")
            return self
        except Exception:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.owner._lock.release()
            raise

    def __exit__(self, *_: object) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
        finally:
            self.stream = None
            self.owner._lock.release()

    def request(
        self,
        method: str,
        params: dict[str, Any],
        target_client_id: str | None = None,
    ) -> dict[str, Any]:
        stream = self.stream
        if stream is None:
            raise CodexDesktopIpcError("IPC session is closed")
        request_id = str(uuid.uuid4())
        message: dict[str, Any] = {
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "version": _METHOD_VERSIONS.get(method, 0),
            "method": method,
            "params": params,
            "timeoutMs": int(self.timeout_seconds * 1000),
        }
        if target_client_id:
            message["targetClientId"] = target_client_id
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stream.write(struct.pack("<I", len(payload)) + payload)
        stream.flush()
        while True:
            response = self._read_message(stream)
            if response.get("type") == "response" and response.get("requestId") == request_id:
                return response

    @staticmethod
    def _read_message(stream: BinaryIO | _UnixStream) -> dict[str, Any]:
        header = _read_exact(stream, 4)
        size = struct.unpack("<I", header)[0]
        if size <= 0 or size > 256 * 1024 * 1024:
            raise CodexDesktopIpcError(f"无效的 IPC frame 长度：{size}")
        payload = _read_exact(stream, size)
        message = json.loads(payload.decode("utf-8"))
        if not isinstance(message, dict):
            raise CodexDesktopIpcError("Codex Desktop IPC 返回了非对象消息")
        return message


def _read_exact(stream: BinaryIO | _UnixStream, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise CodexDesktopIpcError("Codex Desktop IPC 连接提前关闭")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)
