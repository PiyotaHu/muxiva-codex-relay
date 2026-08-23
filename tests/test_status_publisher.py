import json

from muxiva_codex_relay.status_publisher import build_hub_payload


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
    assert payload["active_count"] == 2
    assert payload["all_agents"][0]["state"] == "busy"
    assert payload["all_agents"][1]["state"] == "waiting"


def test_relay_job_is_shown_first() -> None:
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
    assert len(payload["all_agents"]) == 5
    assert len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) < 2048
