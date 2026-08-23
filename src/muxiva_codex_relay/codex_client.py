from __future__ import annotations

from collections.abc import Callable
import glob
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import uuid
from typing import Any


class CodexProtocolError(RuntimeError):
    pass


def app_server_initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {"name": "muxiva-codex-relay", "title": "Muxiva ESP32 Relay", "version": "0.1.0"},
        # Codex's persistent thread queue is currently gated by this protocol
        # capability. It is required when the desktop app owns the active turn.
        "capabilities": {"experimentalApi": True},
    }


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
    # The Windows Store app adds a protected WindowsApps executable to PATH.
    # It is visible to ``shutil.which`` but cannot be spawned by a background
    # relay (WinError 5). Prefer the runnable copy installed by Codex Desktop.
    local_candidates = [candidate for candidate in candidates if candidate.is_file()]
    if os.name == "nt" and local_candidates:
        return max(local_candidates, key=lambda item: item.stat().st_mtime)

    discovered = shutil.which("codex")
    if discovered:
        return Path(discovered).expanduser().resolve()

    if os.name != "nt":
        home = Path.home()
        candidates.extend(
            [
                home / ".local" / "bin" / "codex",
                home / ".npm-global" / "bin" / "codex",
                home / ".volta" / "bin" / "codex",
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
                Path("/Applications/Codex.app/Contents/Resources/bin/codex"),
                home / "Applications" / "Codex.app" / "Contents" / "Resources" / "bin" / "codex",
            ]
        )
        candidates.extend(home.glob(".nvm/versions/node/*/bin/codex"))
    candidates = [candidate for candidate in candidates if candidate.is_file()]
    if not candidates:
        raise FileNotFoundError(
            "Could not discover a runnable Codex CLI; install `codex` or set MUXIVA_CODEX_BIN"
        )
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
        # ``latest`` is selected when the board enters its Codex page and is
        # then kept stable for that page session.
        self._session_thread_id: str | None = None

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
            app_server_initialize_params(),
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

    @property
    def session_thread_id(self) -> str | None:
        return self._session_thread_id

    def configure_session_target(self, target: str) -> None:
        if target not in {"", "latest", "new"}:
            self._session_thread_id = target

    def select_session_thread(self, thread_id: str | None) -> None:
        """Select the conversation reused by subsequent ``latest`` tasks."""
        self._session_thread_id = thread_id or None

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

    def read_thread(self, thread_id: str, include_turns: bool = True) -> dict[str, Any]:
        """Read one Codex thread, optionally including its recorded turns."""
        result = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        thread = result.get("thread")
        return thread if isinstance(thread, dict) else {}

    def submit_task(
        self,
        text: str,
        target: str,
        cwd: Path,
        sandbox: str,
        approval_policy: str,
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        thread_id: str | None = self._session_thread_id
        create_new = target in {"", "new"}
        if target == "latest" and not thread_id:
            threads = self.list_threads(limit=1)
            chosen = threads[0] if threads else None
            thread_id = str(chosen.get("id") or "") if chosen else None
            self._session_thread_id = thread_id or None
        elif target not in {"", "latest", "new"}:
            thread_id = target
            self._session_thread_id = target

        if thread_id:
            try:
                resumed = self.request(
                    "thread/resume",
                    {
                        "threadId": thread_id,
                        "sandbox": sandbox,
                        "approvalPolicy": approval_policy,
                    },
                )
                thread_id = resumed["thread"]["id"]
                self._session_thread_id = thread_id
            except (CodexProtocolError, KeyError) as exc:
                # A desktop Codex turn may already own this conversation. Use
                # Codex's persistent native queue instead of dropping the
                # user's voice task or silently creating another thread.
                queued_id = client_message_id or str(uuid.uuid4())
                try:
                    added = self.request(
                        "thread/queue/add",
                        {
                            "threadId": thread_id,
                            "clientUserMessageId": queued_id,
                            "input": [{"type": "text", "text": text}],
                        },
                    )
                    queued = added["queuedSubmission"]
                    submission_id = str(queued["id"])
                except (CodexProtocolError, KeyError) as queue_exc:
                    raise CodexProtocolError(
                        f"无法访问 Codex 会话 {thread_id}；恢复错误：{exc}；排队错误：{queue_exc}"
                    ) from queue_exc
                try:
                    turn = self.start_queued_task(thread_id, submission_id)
                    return {"threadId": thread_id, "turn": turn, "queued": False}
                except CodexProtocolError as busy_exc:
                    return {
                        "threadId": thread_id,
                        "turn": {},
                        "queued": True,
                        "queuedSubmissionId": submission_id,
                        "queueDetail": str(busy_exc),
                    }

        if not thread_id:
            # `latest` may create exactly one conversation only when Codex has
            # no existing conversation. A configured `new` target retains the
            # explicit multi-session behavior for generic users.
            started = self.request(
                "thread/start",
                {
                    "cwd": str(cwd),
                    "sandbox": sandbox,
                    "approvalPolicy": approval_policy,
                },
            )
            thread_id = started["thread"]["id"]
            if not create_new:
                self._session_thread_id = thread_id

        turn = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "approvalPolicy": approval_policy,
            },
        )
        return {"threadId": thread_id, "turn": turn.get("turn", {})}

    def start_queued_task(self, thread_id: str, queued_submission_id: str) -> dict[str, Any]:
        result = self.request(
            "thread/queue/start",
            {"threadId": thread_id, "queuedSubmissionId": queued_submission_id},
        )
        turn = result.get("turn")
        if not isinstance(turn, dict):
            raise CodexProtocolError("thread/queue/start returned no turn")
        return turn

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
