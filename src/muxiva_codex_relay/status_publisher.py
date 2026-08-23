from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable

from .codex_client import CodexAppServer
from .dispatcher import TaskDispatcher


def _display_text(thread: dict[str, Any]) -> str:
    value = thread.get("name") or thread.get("preview") or "未命名会话"
    return " ".join(str(value).split())[:48]


def _project_name(cwd: object) -> str:
    normalized = str(cwd or "Codex").replace("\\", "/").rstrip("/")
    return normalized.rsplit("/", 1)[-1] or "Codex"


def _map_state(thread: dict[str, Any]) -> str:
    status = thread.get("status") or {}
    kind = status.get("type", "notLoaded")
    if kind == "active":
        return "waiting" if status.get("activeFlags") else "busy"
    if kind == "systemError":
        return "waiting"
    return "idle"


def build_hub_payload(threads: list[dict[str, Any]], relay: dict[str, object]) -> dict[str, Any]:
    agents = [
        {
            "agent_id": str(item.get("id", "unknown")),
            "state": _map_state(item),
            "detail": _display_text(item),
            "ts": int(item.get("updatedAt") or time.time()),
            "cwd": _project_name(item.get("cwd")),
        }
        for item in threads[:5]
    ]
    if relay.get("stage") not in {None, "idle"}:
        agents.insert(
            0,
            {
                "agent_id": "relay",
                "state": "busy" if relay.get("stage") in {"queued", "normalizing", "submitting", "running"} else "waiting",
                "detail": str(relay.get("detail") or "")[:48],
                "ts": int(float(relay.get("updated_at") or time.time())),
                "cwd": "语音任务",
            },
        )
    latest = agents[0] if agents else {
        "agent_id": "codex",
        "state": "idle",
        "detail": "暂无会话",
        "ts": int(time.time()),
        "cwd": "Codex",
    }
    active_count = sum(item["state"] in {"busy", "waiting"} for item in agents)
    return {
        "type": "status",
        "ts": int(time.time()),
        "latest": latest,
        "all_agents": agents,
        "active_count": active_count,
        "groups": [
            {
                "name": "PC",
                "items": [
                    {"label": "Relay", "value": str(relay.get("stage", "idle"))},
                    {"label": "会话", "value": str(len(threads))},
                    {"label": "队列", "value": str(relay.get("queue_size", 0))},
                ],
            }
        ],
    }


class StatusPublisher:
    def __init__(
        self,
        codex: CodexAppServer,
        dispatcher: TaskDispatcher,
        hub_url: str,
        hub_token: str,
        interval_seconds: int,
        secondary_publish: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.codex = codex
        self.dispatcher = dispatcher
        self.hub_url = hub_url
        self.hub_token = hub_token
        self.interval_seconds = interval_seconds
        self.secondary_publish = secondary_publish
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="status-publisher", daemon=True)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                threads = self.codex.list_threads(limit=8)
                payload = build_hub_payload(threads, self.dispatcher.snapshot())
                if self.secondary_publish:
                    self.secondary_publish(payload)
                request = urllib.request.Request(
                    self.hub_url,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {self.hub_token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    method="POST",
                )
                with self._opener.open(request, timeout=4) as response:
                    response.read(256)
            except Exception:
                # The ESP32 may be sleeping or rebooting; next tick retries.
                pass
            self._stop.wait(self.interval_seconds)
