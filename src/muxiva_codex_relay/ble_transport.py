from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
from typing import Any, Callable

from .dispatcher import TaskDispatcher


SERVICE_UUID = "7e400001-b5a3-f393-e0a9-e50e24dcca9e"
CONTROL_UUID = "7e400002-b5a3-f393-e0a9-e50e24dcca9e"
STATUS_UUID = "7e400003-b5a3-f393-e0a9-e50e24dcca9e"
AUDIO_UUID = "7e400004-b5a3-f393-e0a9-e50e24dcca9e"
MAX_STATUS_FRAME_BYTES = 2048
MAX_AUDIO_BYTES = 16_000 * 2 * 25


class BleCodexTransport:
    """The only ESP32 transport: BLE discovery, audio upload and status output."""

    def __init__(
        self,
        device_name: str,
        dispatcher: TaskDispatcher,
        selection_path: Path | None = None,
    ):
        self.device_name = device_name
        self.dispatcher = dispatcher
        self.selection_path = selection_path
        self._display_handler: Callable[[bool, bool], None] = lambda _active, _refresh: None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, name="ble-codex", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._control_buffer = bytearray()
        self._audio_lock = threading.Lock()
        self._audio_request_id: str | None = None
        self._audio_buffer = bytearray()
        self._write_lock: asyncio.Lock | None = None
        self._status_write_pending = threading.Event()
        self._status_ready_logged = False

    def set_display_handler(self, handler: Callable[[bool, bool], None]) -> None:
        self._display_handler = handler

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def feed_control_notification(self, data: bytes) -> None:
        self._control_buffer.extend(data)
        if len(self._control_buffer) > 16 * 1024:
            self._control_buffer.clear()
            return
        while b"\n" in self._control_buffer:
            raw, _, remainder = self._control_buffer.partition(b"\n")
            self._control_buffer = bytearray(remainder)
            try:
                payload = json.loads(raw.decode("utf-8"))
                if isinstance(payload, dict):
                    self._handle_control(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._publish_event({"type": "protocol_error", "error": str(exc)})

    def feed_audio_notification(self, data: bytes) -> None:
        with self._audio_lock:
            if self._audio_request_id is None:
                return
            if len(self._audio_buffer) + len(data) > MAX_AUDIO_BYTES:
                request_id = self._audio_request_id
                self._audio_request_id = None
                self._audio_buffer.clear()
                self._publish_event({
                    "type": "preview_result",
                    "ok": False,
                    "request_id": request_id,
                    "error": "录音超过 25 秒限制",
                })
                return
            self._audio_buffer.extend(data)

    def _handle_control(self, payload: dict[str, Any]) -> None:
        message_type = str(payload.get("type") or "")
        request_id = str(payload.get("request_id") or "").strip()[:128]
        if message_type == "display":
            self._display_handler(
                payload.get("active") is True,
                payload.get("refresh_latest") is True,
            )
            return
        if message_type == "audio_start":
            if not request_id:
                raise ValueError("audio_start requires request_id")
            with self._audio_lock:
                self._audio_request_id = request_id
                self._audio_buffer.clear()
            return
        if message_type == "audio_abort":
            with self._audio_lock:
                self._audio_request_id = None
                self._audio_buffer.clear()
            return
        if message_type == "audio_end":
            with self._audio_lock:
                if request_id != self._audio_request_id:
                    raise ValueError("audio_end request_id does not match active recording")
                audio = bytes(self._audio_buffer)
                self._audio_request_id = None
                self._audio_buffer.clear()
            threading.Thread(
                target=self._finish_preview,
                args=(request_id, audio),
                name="ble-asr-preview",
                daemon=True,
            ).start()
            return
        if message_type in {"confirm", "cancel"}:
            threading.Thread(
                target=self._pending_action,
                args=(message_type, request_id),
                name=f"ble-{message_type}",
                daemon=True,
            ).start()
            return
        if message_type == "transcript":
            self.dispatcher.enqueue(
                str(payload.get("transcript") or ""),
                "esp32-ble",
                request_id or None,
            )

    def _finish_preview(self, request_id: str, audio: bytes) -> None:
        try:
            preview = self.dispatcher.preview_audio(audio, "esp32-ble", request_id)
            self._publish_event({
                "type": "preview_result",
                "ok": True,
                "request_id": preview.id,
                "transcript": preview.transcript,
                "normalizer": preview.normalizer,
            })
        except Exception as exc:
            self._publish_event({
                "type": "preview_result",
                "ok": False,
                "request_id": request_id,
                "error": str(exc),
            })

    def _pending_action(self, action: str, request_id: str) -> None:
        try:
            if action == "confirm":
                self.dispatcher.confirm_preview(request_id)
            else:
                self.dispatcher.cancel_preview(request_id)
            payload: dict[str, Any] = {
                "type": "action_result",
                "action": action,
                "ok": True,
                "request_id": request_id,
            }
        except Exception as exc:
            payload = {
                "type": "action_result",
                "action": action,
                "ok": False,
                "request_id": request_id,
                "error": str(exc),
            }
        self._publish_event(payload)

    @staticmethod
    def _encode_status(payload: dict[str, Any]) -> bytes:
        agents = payload.get("all_agents")
        compact = {
            "type": "status",
            "ts": payload.get("ts"),
            "all_agents": list(agents[:1]) if isinstance(agents, list) else [],
            "active_count": payload.get("active_count", 0),
        }
        data = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) <= MAX_STATUS_FRAME_BYTES:
            return data
        agent = compact["all_agents"][0] if compact["all_agents"] else None
        if isinstance(agent, dict):
            agent = dict(agent)
            compact["all_agents"] = [agent]
            for key in ("last_assistant", "last_user", "detail", "cwd"):
                value = str(agent.get(key) or "")
                while value and len(data) > MAX_STATUS_FRAME_BYTES:
                    value = value[:-8].rstrip()
                    agent[key] = value + ("…" if value else "")
                    data = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
            if len(data) > MAX_STATUS_FRAME_BYTES:
                agent = {key: agent[key] for key in ("agent_id", "state", "detail") if key in agent}
                compact["all_agents"] = [agent]
                data = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        return data

    def publish_status(self, payload: dict[str, Any]) -> None:
        if self._status_write_pending.is_set():
            return
        self._submit_frame(self._encode_status(payload), coalesce_status=True)

    def _publish_event(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) <= MAX_STATUS_FRAME_BYTES:
            self._submit_frame(data, coalesce_status=False)

    def _submit_frame(self, data: bytes, coalesce_status: bool) -> None:
        loop, client = self._loop, self._client
        if not loop or not client or not getattr(client, "is_connected", False):
            return
        if coalesce_status:
            self._status_write_pending.set()
        try:
            future = asyncio.run_coroutine_threadsafe(self._write_status(data), loop)
        except Exception:
            if coalesce_status:
                self._status_write_pending.clear()
            raise

        def completed(result: Any) -> None:
            if coalesce_status:
                self._status_write_pending.clear()
            try:
                result.result()
            except Exception as exc:
                print(f"BLE write failed: {exc}")

        future.add_done_callback(completed)

    async def _write_status(self, data: bytes) -> None:
        client = self._client
        if not client or not client.is_connected:
            return
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            try:
                mtu_size = int(getattr(client, "mtu_size", 23) or 23)
            except (TypeError, ValueError):
                mtu_size = 23
            chunk_size = max(20, min(244, mtu_size - 3))
            for offset in range(0, len(data), chunk_size):
                await client.write_gatt_char(STATUS_UUID, data[offset : offset + chunk_size], response=True)
            if not self._status_ready_logged:
                self._status_ready_logged = True
                print(f"BLE status channel ready: mtu={mtu_size}, frame={len(data)} bytes")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            print(f"BLE transport stopped: {exc}")

    @staticmethod
    def _candidate_id(device: Any) -> str:
        return str(getattr(device, "address", "") or getattr(device, "name", ""))

    async def _discover_candidates(self, scanner: Any) -> list[Any]:
        discovered = await scanner.discover(timeout=8, return_adv=True)
        entries = discovered.values() if isinstance(discovered, dict) else discovered
        candidates: dict[str, Any] = {}
        for entry in entries:
            if isinstance(entry, tuple):
                device, advertisement = entry
            else:
                device, advertisement = entry, None
            name = str(getattr(device, "name", "") or getattr(advertisement, "local_name", "") or "")
            service_uuids = [str(item).lower() for item in getattr(advertisement, "service_uuids", []) or []]
            if name == self.device_name or SERVICE_UUID in service_uuids:
                candidates[self._candidate_id(device)] = device
        return list(candidates.values())

    def _load_selected_id(self) -> str | None:
        if self.selection_path is None:
            return None
        try:
            payload = json.loads(self.selection_path.read_text(encoding="utf-8"))
            value = str(payload.get("device_id") or "").strip()
            return value or None
        except (OSError, ValueError, TypeError):
            return None

    def _save_selected_id(self, device_id: str) -> None:
        if self.selection_path is None:
            return
        try:
            self.selection_path.parent.mkdir(parents=True, exist_ok=True)
            self.selection_path.write_text(
                json.dumps({"device_id": device_id, "device_name": self.device_name}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Unable to remember BLE device: {exc}")

    async def _select_device(self, candidates: list[Any]) -> Any | None:
        if not candidates:
            return None
        if len(candidates) == 1:
            device = candidates[0]
            self._save_selected_id(self._candidate_id(device))
            print(f"Found one {self.device_name}; connecting automatically")
            return device

        print(f"Found {len(candidates)} ESP32 devices named {self.device_name}:")
        for index, device in enumerate(candidates, 1):
            print(f"  {index}. {getattr(device, 'name', self.device_name)}  {self._candidate_id(device)}")

        remembered = self._load_selected_id()
        if not sys.stdin.isatty():
            selected = next((item for item in candidates if self._candidate_id(item) == remembered), None)
            if selected is not None:
                print(f"Non-interactive startup: using previously confirmed device {remembered}")
                return selected
            print("Multiple ESP32 devices require one interactive launch to confirm the device")
            return None

        while True:
            answer = await asyncio.to_thread(input, f"Select device [1-{len(candidates)}]: ")
            try:
                selected = candidates[int(answer.strip()) - 1]
            except (ValueError, IndexError):
                print("Please enter one of the displayed numbers")
                continue
            self._save_selected_id(self._candidate_id(selected))
            return selected

    async def _run(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            print("Bluetooth support is missing; reinstall with: pip install -e .")
            return
        self._loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                candidates = await self._discover_candidates(BleakScanner)
                device = await self._select_device(candidates)
                if device is None:
                    await asyncio.sleep(2)
                    continue
                async with BleakClient(device, timeout=10) as client:
                    self._client = client
                    self._status_ready_logged = False
                    await client.start_notify(
                        CONTROL_UUID,
                        lambda _sender, data: self.feed_control_notification(bytes(data)),
                    )
                    await client.start_notify(
                        AUDIO_UUID,
                        lambda _sender, data: self.feed_audio_notification(bytes(data)),
                    )
                    print(f"BLE connected: {self._candidate_id(device)}")
                    self._publish_event({"type": "connected"})
                    while client.is_connected and not self._stop.is_set():
                        await asyncio.sleep(1)
            except Exception as exc:
                print(f"BLE reconnecting: {exc}")
                await asyncio.sleep(3)
            finally:
                self._client = None
                with self._audio_lock:
                    self._audio_request_id = None
                    self._audio_buffer.clear()
