from __future__ import annotations

from collections.abc import Callable
import glob
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
from typing import Any


class CodexProtocolError(RuntimeError):
    pass


def discover_codex_binary(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_file():
            return path
        raise FileNotFoundError(f"Codex binary not found: {path}")

    candidates: list[Path] = []
    local = os.getenv("LOCALAPPDATA")
    if local:
        candidates.extend(
            Path(p) for p in glob.glob(str(Path(local) / "OpenAI" / "Codex" / "bin" / "*" / "codex.exe"))
        )
    path_value = os.getenv("PATH", "")
    for directory in path_value.split(os.pathsep):
        candidate = Path(directory) / ("codex.exe" if os.name == "nt" else "codex")
        if candidate.is_file():
            candidates.append(candidate)
    if not candidates:
        raise FileNotFoundError("Could not discover a runnable Codex CLI")
    return max(candidates, key=lambda item: item.stat().st_mtime)


class CodexAppServer:
    """Small JSONL client for the official local Codex app-server protocol."""

    def __init__(self, binary: Path, timeout_seconds: float = 20):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    def start(self) -> None:
        if self._process and self._process.poll() is None:
            return
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [str(self.binary), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_stdout, name="codex-jsonl", daemon=True)
        self._stderr_reader = threading.Thread(target=self._drain_stderr, name="codex-stderr", daemon=True)
        self._reader.start()
        self._stderr_reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {"name": "muxiva-codex-relay", "title": "Muxiva ESP32 Relay", "version": "0.1.0"},
                "capabilities": {"experimentalApi": False},
            },
        )
        self.notify("initialized", None)

    def close(self) -> None:
        process, self._process = self._process, None
        if not process:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._process is None or self._process.poll() is not None:
            self.start()
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        self._send({"id": request_id, "method": method, "params": params or {}})
        try:
            response = response_queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError(f"Codex app-server timed out: {method}") from exc
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
        if "error" in response:
            raise CodexProtocolError(f"{method}: {response['error']}")
        return response.get("result", {})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        if self._process is None or self._process.poll() is not None:
            self.start()
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def list_threads(self, limit: int = 8) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {
                "limit": limit,
                "sortKey": "recency_at",
                "sortDirection": "desc",
                "useStateDbOnly": True,
            },
        )
        return list(result.get("data", []))

    def submit_task(
        self,
        text: str,
        target: str,
        cwd: Path,
        sandbox: str,
        approval_policy: str,
    ) -> dict[str, Any]:
        thread_id: str | None = None
        if target == "latest":
            threads = self.list_threads(limit=12)
            idle = [item for item in threads if item.get("status", {}).get("type") in {"idle", "notLoaded"}]
            chosen = idle[0] if idle else (threads[0] if threads else None)
            thread_id = chosen.get("id") if chosen else None
        elif target not in {"", "new"}:
            thread_id = target

        if thread_id:
            try:
                resumed = self.request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "excludeTurns": True,
                        "sandbox": sandbox,
                        "approvalPolicy": approval_policy,
                    },
                )
                thread_id = resumed["thread"]["id"]
            except (CodexProtocolError, KeyError):
                thread_id = None

        if not thread_id:
            started = self.request(
                "thread/start",
                {
                    "cwd": str(cwd),
                    "sandbox": sandbox,
                    "approvalPolicy": approval_policy,
                },
            )
            thread_id = started["thread"]["id"]

        turn = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": approval_policy,
            },
        )
        return {"threadId": thread_id, "turn": turn.get("turn", {})}

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            raise CodexProtocolError("Codex app-server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()

    def _read_stdout(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            request_id = message.get("id")
            if request_id is not None and ("result" in message or "error" in message):
                with self._pending_lock:
                    destination = self._pending.get(request_id)
                if destination:
                    destination.put(message)
                continue
            if request_id is not None and "method" in message:
                self._send({"id": request_id, "error": {"code": -32601, "message": "Relay cannot handle server request"}})
                continue
            for listener in tuple(self._listeners):
                try:
                    listener(message)
                except Exception:
                    pass

    def _drain_stderr(self) -> None:
        process = self._process
        if process and process.stderr:
            for _ in process.stderr:
                pass
