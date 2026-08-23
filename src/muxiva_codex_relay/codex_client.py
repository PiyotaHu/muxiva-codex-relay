from __future__ import annotations

from collections.abc import Callable
from collections import deque
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

from .desktop_ipc import CodexDesktopIpc, CodexDesktopIpcError


class CodexProtocolError(RuntimeError):
    pass


def app_server_initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {"name": "muxiva-codex-relay", "title": "Muxiva ESP32 Relay", "version": "0.1.0"},
        # Keep experimental discovery available for older Codex Desktop builds;
        # voice submission itself uses the stable turn/start and turn/steer APIs.
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

    def __init__(
        self,
        binary: Path,
        timeout_seconds: float = 20,
        desktop_ipc: CodexDesktopIpc | None = None,
        host_id: str = "local",
    ):
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[str] | None = None
        self._lifecycle_lock = threading.RLock()
        self._startup_event = threading.Event()
        self._starting = False
        self._initialized = False
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._last_stderr: deque[str] = deque(maxlen=20)
        self._desktop_ipc = desktop_ipc or CodexDesktopIpc(timeout_seconds=min(timeout_seconds, 10))
        self._host_id = host_id
        # ``latest`` is selected when the board enters its Codex page and is
        # then kept stable for that page session.
        self._session_thread_id: str | None = None

    def start(self) -> None:
        starter = False
        with self._lifecycle_lock:
            if self._process and self._process.poll() is None and self._initialized:
                return
            if not self._starting:
                self._starting = True
                self._initialized = False
                self._startup_event.clear()
                starter = True

        if not starter:
            if not self._startup_event.wait(timeout=self.timeout_seconds):
                raise TimeoutError("Timed out waiting for Codex app-server startup")
            with self._lifecycle_lock:
                if self._process and self._process.poll() is None and self._initialized:
                    return
            raise CodexProtocolError("Codex app-server failed to initialize")

        process: subprocess.Popen[str] | None = None
        try:
            self._fail_pending("Codex app-server restarted before replying")
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            process = subprocess.Popen(
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
            with self._lifecycle_lock:
                self._process = process
            self._reader = threading.Thread(
                target=self._read_stdout,
                args=(process,),
                name="codex-jsonl",
                daemon=True,
            )
            self._stderr_reader = threading.Thread(
                target=self._drain_stderr,
                args=(process,),
                name="codex-stderr",
                daemon=True,
            )
            self._reader.start()
            self._stderr_reader.start()
            self._request_started("initialize", app_server_initialize_params())
            self._send({"method": "initialized"})
            with self._lifecycle_lock:
                self._initialized = True
        except Exception:
            if process:
                self._terminate_process(process)
            with self._lifecycle_lock:
                if self._process is process:
                    self._process = None
                self._initialized = False
            raise
        finally:
            with self._lifecycle_lock:
                self._starting = False
                self._startup_event.set()

    def close(self) -> None:
        with self._lifecycle_lock:
            process, self._process = self._process, None
            self._initialized = False
            self._starting = False
            self._startup_event.set()
            self._fail_pending("Codex app-server closed")
            if process:
                self._terminate_process(process)

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
        self.start()
        return self._request_started(method, params)

    def _request_started(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
            self._pending[request_id] = response_queue
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
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
            # Do not infer "busy" from a failed resume. The app-server exposes
            # the actual active turn in thread/read; only that concrete turn
            # may receive turn/steer.
            steered = self.steer_active_task(thread_id, text)
            if steered:
                return steered
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
                if self._is_active_writer_error(exc):
                    return self._submit_through_desktop_owner(
                        thread_id,
                        text,
                        client_message_id,
                    )
                # The active turn may have started between thread/read and
                # thread/resume. Re-read once and steer it. If the thread is
                # still idle, surface the real resume error instead of leaving
                # a voice command stranded in an experimental queue.
                steered = self.steer_active_task(thread_id, text)
                if steered:
                    return steered
                # A turn can finish between resume and the status recheck.
                # Retry resume once now that the thread is observably idle.
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
                except (CodexProtocolError, KeyError) as retry_exc:
                    if self._is_active_writer_error(retry_exc):
                        return self._submit_through_desktop_owner(
                            thread_id,
                            text,
                            client_message_id,
                        )
                    raise CodexProtocolError(
                        f"无法恢复空闲 Codex 会话 {thread_id}：{retry_exc}"
                    ) from retry_exc

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

        try:
            turn = self.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": text}],
                    "approvalPolicy": approval_policy,
                },
            )
        except CodexProtocolError as exc:
            # A turn can start after the idle check. Steering that exact turn
            # is safe; otherwise retain the original protocol failure.
            steered = self.steer_active_task(thread_id, text)
            if steered:
                return steered
            raise CodexProtocolError(f"无法启动 Codex turn：{exc}") from exc
        return {"threadId": thread_id, "turn": turn.get("turn", {})}

    @staticmethod
    def _is_active_writer_error(exc: BaseException) -> bool:
        return "already has an active writer" in str(exc).lower()

    @staticmethod
    def _is_no_active_turn_error(exc: BaseException) -> bool:
        message = str(exc).lower()
        return "no active turn" in message or "without an active turn" in message

    @staticmethod
    def _desktop_turn(result: dict[str, Any]) -> dict[str, Any]:
        turn = result.get("turn")
        if isinstance(turn, dict):
            return turn
        turn_id = str(result.get("turnId") or result.get("id") or "")
        return {"id": turn_id, "status": "inProgress"} if turn_id else {"status": "inProgress"}

    def _submit_through_desktop_owner(
        self,
        thread_id: str,
        text: str,
        client_message_id: str | None,
    ) -> dict[str, Any]:
        """Forward a task to the Codex Desktop process that owns the thread.

        A second app-server process cannot resume a thread with an active
        Desktop writer. Desktop's same-user IPC router is therefore used only
        as a compatibility adapter for this ownership conflict. It is not a
        replacement for the public app-server protocol.
        """
        desktop_ipc = getattr(self, "_desktop_ipc", None)
        if desktop_ipc is None:
            raise CodexProtocolError(
                f"Codex 会话 {thread_id} 正由 Desktop 持有，但 Desktop IPC 未启用"
            )
        message_id = client_message_id or str(uuid.uuid4())
        try:
            owner_id = desktop_ipc.find_thread_owner(
                getattr(self, "_host_id", "local"),
                thread_id,
            )
        except (CodexDesktopIpcError, OSError) as ipc_exc:
            raise CodexProtocolError(
                f"Codex 会话 {thread_id} 正由 Desktop 持有，但无法连接 Desktop IPC：{ipc_exc}"
            ) from ipc_exc
        if not owner_id:
            raise CodexProtocolError(
                f"Codex 会话 {thread_id} 有活动 writer，但 Desktop 未报告会话 owner"
            )

        try:
            result = desktop_ipc.steer_turn(owner_id, thread_id, text, message_id)
            return {
                "threadId": thread_id,
                "turn": self._desktop_turn(result),
                "steered": True,
                "transport": "desktop-ipc",
            }
        except CodexDesktopIpcError as steer_exc:
            if not self._is_no_active_turn_error(steer_exc):
                raise CodexProtocolError(
                    f"无法向 Desktop 持有的 Codex 会话 {thread_id} 追加任务：{steer_exc}"
                ) from steer_exc

        try:
            result = desktop_ipc.start_turn(owner_id, thread_id, text, message_id)
        except CodexDesktopIpcError as start_exc:
            raise CodexProtocolError(
                f"无法在 Desktop 持有的 Codex 会话 {thread_id} 启动任务：{start_exc}"
            ) from start_exc
        return {
            "threadId": thread_id,
            "turn": self._desktop_turn(result),
            "transport": "desktop-ipc",
        }

    def active_turn_id(self, thread_id: str) -> str | None:
        """Return the real in-flight turn id reported by app-server."""
        thread = self.read_thread(thread_id, include_turns=True)
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        for turn in reversed(turns):
            if not isinstance(turn, dict) or turn.get("status") != "inProgress":
                continue
            turn_id = str(turn.get("id") or "")
            if turn_id:
                return turn_id
        return None

    def steer_task(self, thread_id: str, expected_turn_id: str, text: str) -> dict[str, Any]:
        """Append input to an active turn without creating or queuing a turn."""
        result = self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
                "expectedTurnId": expected_turn_id,
            },
        )
        accepted_turn_id = str(result.get("turnId") or "")
        if not accepted_turn_id:
            raise CodexProtocolError("turn/steer returned no turnId")
        return {
            "threadId": thread_id,
            "turn": {"id": accepted_turn_id, "status": "inProgress"},
            "steered": True,
        }

    def steer_active_task(self, thread_id: str, text: str) -> dict[str, Any] | None:
        """Steer the current turn, tolerating a turn that just completed.

        The expected turn can change between thread/read and turn/steer. Retry
        once for a different active turn; return None when the thread became
        idle so the caller can start a normal turn.
        """
        expected_turn_id = self.active_turn_id(thread_id)
        if not expected_turn_id:
            return None
        try:
            return self.steer_task(thread_id, expected_turn_id, text)
        except CodexProtocolError:
            refreshed_turn_id = self.active_turn_id(thread_id)
            if not refreshed_turn_id:
                return None
            if refreshed_turn_id == expected_turn_id:
                raise
            return self.steer_task(thread_id, refreshed_turn_id, text)

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if not process or process.poll() is not None or not process.stdin:
            raise CodexProtocolError("Codex app-server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        if not process.stdout:
            return
        try:
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
        finally:
            self._handle_process_exit(process)

    def _drain_stderr(self, process: subprocess.Popen[str]) -> None:
        if process.stderr:
            for line in process.stderr:
                self._last_stderr.append(line.rstrip())

    def diagnostics(self) -> dict[str, Any]:
        process = self._process
        return {
            "running": bool(process and process.poll() is None),
            "pid": process.pid if process and process.poll() is None else None,
            "pending_requests": len(self._pending),
            "stderr_tail": list(self._last_stderr),
            "transport": "stdio-jsonl",
        }

    def _handle_process_exit(self, process: subprocess.Popen[str]) -> None:
        with self._lifecycle_lock:
            # A reader belonging to an older process can finish after a new
            # app-server has already started. It must not tear down the new
            # connection or fail its requests.
            if self._process is not process:
                return
            self._process = None
            self._initialized = False
            self._startup_event.set()
            return_code = process.poll()
            self._fail_pending(f"Codex app-server exited unexpectedly (code={return_code})")

    def _fail_pending(self, message: str) -> None:
        failure = {"error": {"code": -32098, "message": message}}
        with self._pending_lock:
            destinations = list(self._pending.values())
        for destination in destinations:
            try:
                destination.put_nowait(failure)
            except queue.Full:
                pass

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
