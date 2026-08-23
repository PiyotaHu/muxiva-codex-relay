from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import urllib.request
from typing import Any, Callable

from .codex_client import CodexAppServer
from .dispatcher import TaskDispatcher


MAX_DISPLAY_USER_CHARS = 256
MAX_DISPLAY_ASSISTANT_CHARS = 3500


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


def _compact_text(value: object, limit: int = 96) -> str:
    return " ".join(str(value or "").split())[:limit]


def _compact_sentence(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip()
    boundary = max(clipped.rfind(mark) for mark in "。！？；.!?;")
    if boundary >= limit // 2:
        clipped = clipped[: boundary + 1]
    elif " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip("，、,:：") + "…"


def extract_recent_messages(thread: dict[str, Any]) -> dict[str, str]:
    """Extract one coherent user/assistant pair from the newest Codex turn.

    A newly submitted turn may contain only ``userMessage`` while Codex is
    still working.  Never combine that user text with an assistant message
    from an older turn: the ESP must show an empty answer until this same turn
    produces one.
    """
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return {"turn_id": "", "last_user": "", "last_assistant": ""}
    for turn in reversed(turns):
        if not isinstance(turn, dict):
            continue
        latest_user = ""
        latest_assistant = ""
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "userMessage":
                parts: list[str] = []
                for part in item.get("content") or []:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(str(part.get("text") or ""))
                if parts:
                    latest_user = _compact_sentence(" ".join(parts), MAX_DISPLAY_USER_CHARS)
            elif kind == "agentMessage" and item.get("text"):
                latest_assistant = _compact_sentence(
                    item.get("text"), MAX_DISPLAY_ASSISTANT_CHARS
                )
        if latest_user or latest_assistant:
            return {
                "turn_id": str(turn.get("id") or ""),
                "last_user": latest_user,
                "last_assistant": latest_assistant,
            }
    return {"turn_id": "", "last_user": "", "last_assistant": ""}


def build_hub_payload(
    threads: list[dict[str, Any]],
    relay: dict[str, object],
    recent_messages: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    recent_messages = recent_messages or {}
    # The ESP32 is a 400x300 single-conversation terminal. Only publish the
    # selected session; relay progress is folded into that same card.
    agents = [
        {
            "agent_id": str(item.get("id", "unknown")),
            "state": _map_state(item),
            "detail": _display_text(item),
            "ts": int(item.get("updatedAt") or time.time()),
            "cwd": _project_name(item.get("cwd")),
            **recent_messages.get(str(item.get("id", "")), {}),
        }
        for item in threads[:1]
    ]
    if relay.get("stage") not in {None, "idle"}:
        stage = str(relay.get("stage") or "idle")
        relay_state = "busy" if stage in {"queued", "transcribing", "normalizing", "submitting", "running"} else (
            "waiting" if stage in {"failed", "interrupted"} else "idle"
        )
        if agents:
            agents[0]["state"] = relay_state
            agents[0]["detail"] = str(relay.get("detail") or agents[0]["detail"])[:48]
            agents[0]["ts"] = int(float(relay.get("updated_at") or time.time()))
        else:
            agents.append({
                "agent_id": "relay",
                "state": relay_state,
                "detail": str(relay.get("detail") or "")[:48],
                "ts": int(float(relay.get("updated_at") or time.time())),
                "cwd": "语音任务",
            })
    latest = agents[0] if agents else {
        "agent_id": "codex",
        "state": "idle",
        "detail": "暂无会话",
        "ts": int(time.time()),
        "cwd": "Codex",
    }
    active_count = sum(item["state"] in {"busy", "waiting"} for item in agents)
    # ``latest`` and ``all_agents[0]`` refer to the same conversation. The
    # firmware renders the full message from all_agents; avoid duplicating the
    # potentially long answer in the HTTP frame (the ESP endpoint has a small
    # bounded request buffer).
    latest_summary = dict(latest)
    if agents:
        latest_summary.pop("last_user", None)
        latest_summary.pop("last_assistant", None)
    return {
        "type": "status",
        "ts": int(time.time()),
        "latest": latest_summary,
        "all_agents": agents,
        "active_count": active_count,
        "groups": [
            {
                "name": "PC",
                "items": [
                    {"label": "Relay", "value": str(relay.get("stage", "idle"))},
                    {"label": "会话", "value": "1" if agents else "0"},
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
        target: str = "latest",
        display_state_path: Path | None = None,
    ):
        self.codex = codex
        self.dispatcher = dispatcher
        self.hub_url = hub_url
        self.hub_token = hub_token
        self.interval_seconds = interval_seconds
        self.secondary_publish = secondary_publish
        self.target = target
        self.display_state_path = display_state_path
        self._stop = threading.Event()
        # ESP32 starts on the Xiaozhi/weather page. Do not continuously push
        # Codex snapshots over its real-time audio Wi-Fi path until the user
        # explicitly opens the Codex page.
        self._display_active = threading.Event()
        # ``latest`` is resolved when the user enters the Codex page. Keep the
        # resolved conversation stable while that page remains open so an
        # in-flight voice task cannot jump to a different desktop thread.
        self._refresh_latest = threading.Event()
        if self._load_display_active():
            self._display_active.set()
            self._refresh_latest.set()
        self._thread = threading.Thread(target=self._run, name="status-publisher", daemon=True)
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._thread_cache: dict[str, tuple[object, dict[str, str]]] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_display_active(self, active: bool) -> None:
        if active:
            if not self._display_active.is_set():
                self._refresh_latest.set()
            self._display_active.set()
        else:
            self._display_active.clear()
        self._persist_display_active(active)

    @property
    def display_active(self) -> bool:
        return self._display_active.is_set()

    def _load_display_active(self) -> bool:
        path = self.display_state_path
        if path is None:
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("active") is True
        except (OSError, ValueError, TypeError):
            return False

    def _persist_display_active(self, active: bool) -> None:
        path = self.display_state_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"active": active, "updated_at": time.time()}, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            # Status delivery must continue even when the runtime directory is
            # temporarily read-only.
            pass

    def _select_threads(self, listed_threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected_id = self.codex.session_thread_id

        if self.target == "latest" and (self._refresh_latest.is_set() or not selected_id):
            threads = listed_threads[:1]
            if threads:
                selected_id = str(threads[0].get("id") or "") or None
                self.codex.select_session_thread(selected_id)
            self._refresh_latest.clear()
            return threads

        if not selected_id and self.target not in {"", "latest", "new"}:
            selected_id = self.target
        if selected_id:
            threads = [item for item in listed_threads if str(item.get("id") or "") == selected_id]
            if not threads:
                selected = self.codex.read_thread(selected_id, include_turns=False)
                threads = [selected] if selected else []
            return threads
        return listed_threads[:1]

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._display_active.is_set():
                self._stop.wait(self.interval_seconds)
                continue
            try:
                listed_threads = self.codex.list_threads(limit=8)
                threads = self._select_threads(listed_threads)
                recent_messages: dict[str, dict[str, str]] = {}
                # The 400x300 display shows the latest conversation. Avoid
                # repeatedly transferring a whole thread when it has not changed.
                for item in threads[:1]:
                    thread_id = str(item.get("id") or "")
                    if not thread_id:
                        continue
                    revision = item.get("updatedAt")
                    cached = self._thread_cache.get(thread_id)
                    if cached and cached[0] == revision:
                        recent_messages[thread_id] = cached[1]
                        continue
                    detail = self.codex.read_thread(thread_id, include_turns=True)
                    messages = extract_recent_messages(detail)
                    self._thread_cache[thread_id] = (revision, messages)
                    recent_messages[thread_id] = messages
                payload = build_hub_payload(threads, self.dispatcher.snapshot(), recent_messages)
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
