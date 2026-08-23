from __future__ import annotations

from contextlib import nullcontext
from pathlib import PurePosixPath

import pytest

from muxiva_codex_relay import desktop_ipc
from muxiva_codex_relay.desktop_ipc import CodexDesktopIpc, CodexDesktopIpcError


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, params, target_client_id=None):
        self.calls.append((method, params, target_client_id))
        return self.response


def test_unix_candidates_use_codex_home_socket_first(monkeypatch, tmp_path) -> None:
    class PosixPathFactory:
        def __new__(cls, value):
            return PurePosixPath(value)

        @staticmethod
        def home():
            return PurePosixPath("/Users/tester")

    codex_home = tmp_path / ".codex-test"
    monkeypatch.setattr(desktop_ipc.os, "name", "posix")
    monkeypatch.setattr(desktop_ipc, "Path", PosixPathFactory)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    candidates = desktop_ipc.desktop_ipc_candidates()

    assert candidates[0] == str(PurePosixPath(str(codex_home)) / "ipc" / "ipc.sock")
    assert any("codex-ipc" in candidate for candidate in candidates[1:])


def test_windows_candidate_uses_named_pipe(monkeypatch) -> None:
    monkeypatch.setattr(desktop_ipc.os, "name", "nt")
    assert desktop_ipc.desktop_ipc_candidates() == [r"\\.\pipe\codex-ipc"]


def test_owner_discovery_returns_desktop_client_id(monkeypatch) -> None:
    session = FakeSession(
        {
            "resultType": "success",
            "handledByClientId": "desktop-owner",
            "result": {},
        }
    )
    client = CodexDesktopIpc()
    monkeypatch.setattr(client, "_session", lambda: nullcontext(session))

    assert client.find_thread_owner("local", "thread-1") == "desktop-owner"
    assert session.calls == [
        (
            "thread-owner-discovery",
            {"hostId": "local", "conversationId": "thread-1"},
            None,
        )
    ]


def test_owner_discovery_propagates_transport_failure(monkeypatch) -> None:
    client = CodexDesktopIpc()

    def fail_session():
        raise CodexDesktopIpcError("Permission denied")

    monkeypatch.setattr(client, "_session", fail_session)
    with pytest.raises(CodexDesktopIpcError, match="Permission denied"):
        client.find_thread_owner("local", "thread-1")


def test_follower_result_is_unwrapped_to_app_server_shape(monkeypatch) -> None:
    session = FakeSession(
        {
            "resultType": "success",
            "result": {"result": {"turnId": "turn-1"}},
        }
    )
    client = CodexDesktopIpc()
    monkeypatch.setattr(client, "_session", lambda: nullcontext(session))

    result = client.steer_turn(
        "desktop-owner",
        "thread-1",
        "继续任务",
        "voice-job-1",
        "/Users/tester/project",
    )

    assert result == {"turnId": "turn-1"}
    method, params, owner_id = session.calls[0]
    assert method == "thread-follower-steer-turn"
    assert owner_id == "desktop-owner"
    assert params["conversationId"] == "thread-1"
    assert params["clientUserMessageId"] == "voice-job-1"
    assert params["restoreMessage"]["cwd"] == "/Users/tester/project"
    assert params["restoreMessage"]["context"]["workspaceRoots"] == [
        "/Users/tester/project"
    ]
    assert params["restoreMessage"]["context"]["fileAttachments"] == []
