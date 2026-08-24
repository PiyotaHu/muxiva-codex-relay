import json

from muxiva_codex_relay.status_publisher import StatusPublisher, build_hub_payload, extract_recent_messages


class FakeCodex:
    def __init__(self, selected: str | None = None) -> None:
        self.session_thread_id = selected
        self.selected: list[str | None] = []

    def select_session_thread(self, thread_id: str | None) -> None:
        self.session_thread_id = thread_id
        self.selected.append(thread_id)

    def read_thread(self, thread_id: str, include_turns: bool = True):
        return {"id": thread_id, "name": "恢复的会话"}


class FakeDispatcher:
    def snapshot(self):
        return {"stage": "idle", "queue_size": 0}


def make_publisher(codex: FakeCodex, target: str = "latest", display_state_path=None) -> StatusPublisher:
    return StatusPublisher(
        codex,  # type: ignore[arg-type]
        FakeDispatcher(),  # type: ignore[arg-type]
        1,
        target=target,
        display_state_path=display_state_path,
    )


def test_build_hub_payload_maps_codex_states() -> None:
    threads = [
        {
            "id": "thread-busy",
            "status": {"type": "active", "activeFlags": []},
            "name": "修复音频队列",
            "updatedAt": 10,
            "cwd": "C:/repo/muxiva",
        },
        {
            "id": "thread-wait",
            "status": {"type": "active", "activeFlags": ["waitingOnUserInput"]},
            "preview": "等待用户选择",
            "updatedAt": 9,
            "cwd": "C:/repo/client",
        },
    ]
    payload = build_hub_payload(threads, {"stage": "idle", "queue_size": 0})
    assert payload["type"] == "status"
    assert payload["active_count"] == 1
    assert payload["all_agents"][0]["state"] == "busy"
    assert len(payload["all_agents"]) == 1


def test_relay_job_is_folded_into_the_single_session_card() -> None:
    payload = build_hub_payload([], {"stage": "normalizing", "detail": "清洗中", "updated_at": 1, "queue_size": 1})
    assert payload["latest"]["agent_id"] == "relay"
    assert payload["latest"]["state"] == "busy"


def test_payload_stays_inside_esp32_hub_limit() -> None:
    threads = [
        {
            "id": f"thread-{index}",
            "status": {"type": "idle"},
            "name": "很长的会话名称" * 20,
            "updatedAt": 10,
            "cwd": f"C:/very/long/workspace/project-{index}",
        }
        for index in range(12)
    ]
    payload = build_hub_payload(threads, {"stage": "idle", "queue_size": 0})
    assert len(payload["all_agents"]) == 1
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 2048


def test_extract_recent_messages_uses_latest_turn_content() -> None:
    thread = {
        "turns": [
            {
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "旧问题"}]},
                    {"type": "agentMessage", "text": "旧回答"},
                ]
            },
            {
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "  新问题\n第二行 "}]},
                    {"type": "agentMessage", "text": "新回答"},
                ]
            },
        ]
    }
    assert extract_recent_messages(thread) == {
        "turn_id": "",
        "last_user": "新问题 第二行",
        "last_assistant": "新回答",
    }


def test_new_user_turn_never_reuses_previous_turn_answer() -> None:
    thread = {
        "turns": [
            {
                "id": "turn-old",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "第一个问题"}]},
                    {"type": "agentMessage", "text": "第一个问题的回答"},
                ],
            },
            {
                "id": "turn-new",
                "status": "inProgress",
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "第二个问题"}]},
                ],
            },
        ]
    }

    assert extract_recent_messages(thread) == {
        "turn_id": "turn-new",
        "last_user": "第二个问题",
        "last_assistant": "",
    }


def test_long_answer_fills_scrollable_display_without_exceeding_http_limit() -> None:
    answer = "这是用于填满屏幕并验证自动滚动的较长回答。" * 200
    thread = {
        "turns": [
            {
                "items": [
                    {"type": "userMessage", "content": [{"type": "text", "text": "请详细解释"}]},
                    {"type": "agentMessage", "text": answer},
                ]
            }
        ]
    }
    recent = extract_recent_messages(thread)
    assert 3000 <= len(recent["last_assistant"]) <= 3501

    threads = [{"id": "thread-1", "status": {"type": "idle"}, "name": "会话", "cwd": "C:/repo"}]
    payload = build_hub_payload(
        threads,
        {"stage": "idle", "queue_size": 0},
        {"thread-1": recent},
    )
    assert "last_assistant" not in payload["latest"]
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 16384


def test_payload_includes_recent_conversation() -> None:
    threads = [{"id": "thread-1", "status": {"type": "idle"}, "name": "会话", "cwd": "C:/repo"}]
    payload = build_hub_payload(
        threads,
        {"stage": "idle", "queue_size": 0},
        {"thread-1": {"last_user": "帮我修复", "last_assistant": "已经修复"}},
    )
    assert payload["all_agents"][0]["last_user"] == "帮我修复"
    assert payload["all_agents"][0]["last_assistant"] == "已经修复"
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 2048


def test_entering_display_selects_latest_thread_and_latches_it() -> None:
    codex = FakeCodex("thread-old")
    publisher = make_publisher(codex)
    listed = [
        {"id": "thread-new", "name": "配置树莓派SSH登录"},
        {"id": "thread-old", "name": "帮我看一下今天的新闻。"},
    ]

    publisher.set_display_active(True)
    assert publisher._select_threads(listed) == listed[:1]
    assert codex.session_thread_id == "thread-new"

    # A newer background thread must not change the selected conversation
    # while the board remains on the Codex page.
    newer = [{"id": "thread-background", "name": "后台任务"}, *listed]
    assert publisher._select_threads(newer) == [listed[0]]
    assert codex.session_thread_id == "thread-new"


def test_reentering_display_refreshes_latest_thread() -> None:
    codex = FakeCodex("thread-old")
    publisher = make_publisher(codex)

    publisher.set_display_active(True)
    publisher._select_threads([{"id": "thread-first"}])
    publisher.set_display_active(False)
    publisher.set_display_active(True)

    latest = [{"id": "thread-latest", "name": "配置树莓派SSH登录"}]
    assert publisher._select_threads(latest) == latest
    assert codex.session_thread_id == "thread-latest"


def test_explicit_page_refresh_recovers_when_sleep_notification_was_lost() -> None:
    codex = FakeCodex("thread-old")
    publisher = make_publisher(codex)
    publisher.set_display_active(True)
    assert publisher._select_threads([{"id": "thread-old"}])[0]["id"] == "thread-old"

    # The relay may still think the page is active if the previous false POST
    # was lost. A new page-entry refresh must nevertheless select newest.
    publisher.set_display_active(True, refresh_latest=True)
    latest = [{"id": "thread-new"}, {"id": "thread-old"}]
    assert publisher._select_threads(latest)[0]["id"] == "thread-new"
    assert codex.session_thread_id == "thread-new"


def test_display_active_survives_relay_restart(tmp_path) -> None:
    state_path = tmp_path / "display-active.json"
    first = make_publisher(FakeCodex(), display_state_path=state_path)
    first.set_display_active(True)

    restarted = make_publisher(FakeCodex(), display_state_path=state_path)

    assert restarted.display_active is True
    restarted.set_display_active(False)
    assert make_publisher(FakeCodex(), display_state_path=state_path).display_active is False
