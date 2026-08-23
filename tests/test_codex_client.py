from pathlib import Path

import pytest

from muxiva_codex_relay import codex_client
from muxiva_codex_relay.codex_client import CodexAppServer, CodexProtocolError, app_server_initialize_params


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
        if method == "thread/resume":
            raise CodexProtocolError("missing")
        if method == "thread/queue/add":
            raise CodexProtocolError("queue missing")
        raise AssertionError(method)

    client.request = request
    with pytest.raises(CodexProtocolError, match="恢复错误.*排队错误"):
        client.submit_task("hello", "thread-fixed", Path.cwd(), "workspace-write", "never")
    assert [method for method, _ in calls] == ["thread/resume", "thread/queue/add"]


def test_busy_session_uses_native_codex_queue() -> None:
    client = object.__new__(CodexAppServer)
    client._session_thread_id = "thread-busy"
    calls = []

    def request(method, params):
        calls.append((method, params))
        if method == "thread/resume":
            raise CodexProtocolError("thread has an active turn")
        if method == "thread/queue/add":
            return {"queuedSubmission": {"id": "queued-1"}}
        if method == "thread/queue/start":
            raise CodexProtocolError("active turn is still running")
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

    assert result["queued"] is True
    assert result["queuedSubmissionId"] == "queued-1"
    add_params = next(params for method, params in calls if method == "thread/queue/add")
    assert add_params["clientUserMessageId"] == "voice-job-1"
    assert not any(method == "thread/start" for method, _ in calls)
