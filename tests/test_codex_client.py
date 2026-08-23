from pathlib import Path
import queue

import pytest

from muxiva_codex_relay import codex_client
from muxiva_codex_relay.codex_client import CodexAppServer, CodexProtocolError, app_server_initialize_params
from muxiva_codex_relay.desktop_ipc import CodexDesktopIpcError


def test_app_server_enables_native_thread_queue_capability() -> None:
    assert app_server_initialize_params()["capabilities"]["experimentalApi"] is True


def test_discover_codex_prefers_executable_from_path(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("placeholder", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty-local-appdata"))
    monkeypatch.setattr(codex_client.shutil, "which", lambda _name: str(binary))
    assert codex_client.discover_codex_binary() == binary.resolve()


def test_discover_codex_explicit_override_is_platform_neutral(tmp_path) -> None:
    binary = tmp_path / "custom-codex"
    binary.write_text("placeholder", encoding="utf-8")
    assert codex_client.discover_codex_binary(str(binary)) == binary.resolve()


@pytest.mark.skipif(codex_client.os.name != "nt", reason="Windows-specific executable discovery")
def test_discover_codex_prefers_desktop_copy_over_windowsapps_path(monkeypatch, tmp_path) -> None:
    local_root = tmp_path / "OpenAI" / "Codex" / "bin" / "desktop-version"
    local_root.mkdir(parents=True)
    desktop_binary = local_root / "codex.exe"
    desktop_binary.write_text("desktop", encoding="utf-8")
    store_binary = tmp_path / "WindowsApps" / "codex.exe"
    store_binary.parent.mkdir()
    store_binary.write_text("store", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(codex_client.shutil, "which", lambda _name: str(store_binary))

    assert codex_client.discover_codex_binary() == desktop_binary.resolve()


def test_read_thread_uses_current_app_server_shape() -> None:
    client = object.__new__(CodexAppServer)
    calls = []

    def request(method, params):
        calls.append((method, params))
        return {"thread": {"id": "thread-1", "turns": []}}

    client.request = request
    assert client.read_thread("thread-1") == {"id": "thread-1", "turns": []}
    assert calls == [("thread/read", {"threadId": "thread-1", "includeTurns": True})]


def test_select_session_thread_updates_latest_runtime_target() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-old"
    client.select_session_thread("thread-latest")
    assert client.session_thread_id == "thread-latest"


def test_latest_target_is_resolved_once_and_reused_without_experimental_resume_flags() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = None
    calls = []

    def request(method, params):
        calls.append((method, params))
        if method == "thread/list":
            return {"data": [{"id": "thread-1"}]}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": f"turn-{len(calls)}"}}
        raise AssertionError(method)

    client.request = request
    first = client.submit_task("first", "latest", Path.cwd(), "workspace-write", "never")
    second = client.submit_task("second", "latest", Path.cwd(), "workspace-write", "never")

    assert first["threadId"] == second["threadId"] == "thread-1"
    assert [method for method, _ in calls].count("thread/list") == 1
    resume_calls = [params for method, params in calls if method == "thread/resume"]
    assert len(resume_calls) == 2
    assert all("excludeTurns" not in params for params in resume_calls)
    assert not any(method == "thread/start" for method, _ in calls)


def test_fixed_session_resume_failure_never_creates_a_new_thread() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-fixed"
    calls = []

    def request(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "status": {"type": "idle"}, "turns": []}}
        if method == "thread/resume":
            raise CodexProtocolError("missing")
        raise AssertionError(method)

    client.request = request
    with pytest.raises(CodexProtocolError, match="无法恢复空闲 Codex 会话"):
        client.submit_task("hello", "thread-fixed", Path.cwd(), "workspace-write", "never")
    assert [method for method, _ in calls] == [
        "thread/read",
        "thread/resume",
        "thread/read",
        "thread/resume",
    ]
    assert not any(method.startswith("thread/queue") for method, _ in calls)


def test_busy_session_steers_the_real_active_turn_without_queueing() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-busy"
    calls = []

    def request(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {
                "thread": {
                    "id": params["threadId"],
                    "status": {"type": "active", "activeFlags": []},
                    "turns": [{"id": "turn-active", "status": "inProgress"}],
                }
            }
        if method == "turn/steer":
            return {"turnId": "turn-active"}
        raise AssertionError(method)

    client.request = request
    result = client.submit_task(
        "稍后执行这个任务",
        "latest",
        Path.cwd(),
        "workspace-write",
        "never",
        "voice-job-1",
    )

    assert result["steered"] is True
    assert result["turn"]["id"] == "turn-active"
    steer_params = next(params for method, params in calls if method == "turn/steer")
    assert steer_params == {
        "threadId": "thread-busy",
        "input": [{"type": "text", "text": "稍后执行这个任务"}],
        "expectedTurnId": "turn-active",
    }
    assert not any(method.startswith("thread/queue") for method, _ in calls)


def test_resume_race_rechecks_and_steers_new_active_turn() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-race"
    calls = []
    reads = 0

    def request(method, params):
        nonlocal reads
        calls.append((method, params))
        if method == "thread/read":
            reads += 1
            turns = [] if reads == 1 else [{"id": "turn-race", "status": "inProgress"}]
            return {"thread": {"id": params["threadId"], "turns": turns}}
        if method == "thread/resume":
            raise CodexProtocolError("turn started concurrently")
        if method == "turn/steer":
            return {"turnId": "turn-race"}
        raise AssertionError(method)

    client.request = request
    result = client.submit_task("补充这个要求", "latest", Path.cwd(), "workspace-write", "never")

    assert result["steered"] is True
    assert [method for method, _ in calls] == [
        "thread/read",
        "thread/resume",
        "thread/read",
        "turn/steer",
    ]


def test_stale_active_turn_that_finishes_falls_back_to_normal_start() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-stale"
    calls = []
    reads = 0

    def request(method, params):
        nonlocal reads
        calls.append((method, params))
        if method == "thread/read":
            reads += 1
            turns = [{"id": "turn-old", "status": "inProgress"}] if reads == 1 else []
            return {"thread": {"id": params["threadId"], "turns": turns}}
        if method == "turn/steer":
            raise CodexProtocolError("no active turn")
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"]}}
        if method == "turn/start":
            return {"turn": {"id": "turn-new", "status": "inProgress"}}
        raise AssertionError(method)

    client.request = request
    result = client.submit_task("开始新任务", "latest", Path.cwd(), "workspace-write", "never")

    assert result["turn"]["id"] == "turn-new"
    assert not result.get("steered")
    assert [method for method, _ in calls] == [
        "thread/read",
        "turn/steer",
        "thread/read",
        "thread/resume",
        "turn/start",
    ]


class FakeDesktopIpc:
    def __init__(self, steer_result=None, steer_error: str | None = None):
        self.steer_result = steer_result or {"turnId": "desktop-turn"}
        self.steer_error = steer_error
        self.calls = []

    def find_thread_owner(self, host_id, conversation_id):
        self.calls.append(("discover", host_id, conversation_id))
        return "desktop-owner"

    def steer_turn(self, owner_id, conversation_id, text, client_message_id, cwd):
        self.calls.append(("steer", owner_id, conversation_id, text, client_message_id, cwd))
        if self.steer_error:
            raise CodexDesktopIpcError(self.steer_error)
        return self.steer_result

    def start_turn(self, owner_id, conversation_id, text, client_message_id):
        self.calls.append(("start", owner_id, conversation_id, text, client_message_id))
        return {"turn": {"id": "desktop-new-turn", "status": "inProgress"}}


def test_active_writer_is_forwarded_to_desktop_owner_without_resume_retry() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-owned"
    client._desktop_ipc = FakeDesktopIpc()
    client._host_id = "local"
    calls = []

    def request(method, params):
        calls.append((method, params))
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": []}}
        if method == "thread/resume":
            raise CodexProtocolError(
                "thread/resume: {'code': -32600, 'message': 'thread already has an active writer'}"
            )
        raise AssertionError(method)

    client.request = request
    result = client.submit_task(
        "继续刚才的任务",
        "latest",
        Path.cwd(),
        "workspace-write",
        "never",
        "voice-job-7",
    )

    assert result == {
        "threadId": "thread-owned",
        "turn": {"id": "desktop-turn", "status": "inProgress"},
        "steered": True,
        "transport": "desktop-ipc",
        "detached": True,
    }
    assert [method for method, _ in calls] == ["thread/read", "thread/resume"]
    assert client._desktop_ipc.calls == [
        ("discover", "local", "thread-owned"),
        (
            "steer",
            "desktop-owner",
            "thread-owned",
            "继续刚才的任务",
            "voice-job-7",
            str(Path.cwd()),
        ),
    ]


def test_idle_desktop_owner_falls_back_from_steer_to_follower_start() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-owned"
    client._desktop_ipc = FakeDesktopIpc(steer_error="no active turn to steer")
    client._host_id = "local"

    def request(method, params):
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": []}}
        if method == "thread/resume":
            raise CodexProtocolError("thread already has an active writer")
        raise AssertionError(method)

    client.request = request
    result = client.submit_task(
        "启动下一轮",
        "latest",
        Path.cwd(),
        "workspace-write",
        "never",
        "voice-job-8",
    )

    assert result["turn"]["id"] == "desktop-new-turn"
    assert not result.get("steered")
    assert [call[0] for call in client._desktop_ipc.calls] == ["discover", "steer", "start"]


def test_desktop_ipc_failure_is_not_misreported_as_idle_session() -> None:
    class BrokenDesktopIpc:
        def find_thread_owner(self, _host_id, _conversation_id):
            raise CodexDesktopIpcError("Permission denied")

    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-owned"
    client._desktop_ipc = BrokenDesktopIpc()
    client._host_id = "local"

    def request(method, params):
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": []}}
        if method == "thread/resume":
            raise CodexProtocolError("thread already has an active writer")
        raise AssertionError(method)

    client.request = request
    with pytest.raises(CodexProtocolError, match="无法连接 Desktop IPC.*Permission denied"):
        client.submit_task("继续", "latest", Path.cwd(), "workspace-write", "never")


def test_process_exit_wakes_pending_requests_immediately() -> None:
    client = CodexAppServer(Path("codex"))
    destination: queue.Queue[dict] = queue.Queue(maxsize=1)
    client._pending[17] = destination

    client._fail_pending("app-server exited")

    response = destination.get_nowait()
    assert response["error"]["code"] == -32098
    assert "exited" in response["error"]["message"]


def test_stale_reader_cannot_tear_down_replacement_process() -> None:
    client = CodexAppServer(Path("codex"))
    stale_process = object()
    replacement_process = object()
    client._process = replacement_process  # type: ignore[assignment]
    client._initialized = True

    client._handle_process_exit(stale_process)  # type: ignore[arg-type]

    assert client._process is replacement_process
    assert client._initialized is True


def test_request_removes_pending_entry_when_pipe_write_fails(monkeypatch) -> None:
    client = CodexAppServer(Path("codex"))
    monkeypatch.setattr(client, "start", lambda: None)

    def fail_send(_message):
        raise CodexProtocolError("broken pipe")

    monkeypatch.setattr(client, "_send", fail_send)
    with pytest.raises(CodexProtocolError, match="broken pipe"):
        client.request("thread/list", {})
    assert client._pending == {}
