from __future__ import annotations

import asyncio
import hmac
import json
import threading
from typing import Any

from .dispatcher import TaskDispatcher


SERVICE_UUID = "7e400001-b5a3-f393-e0a9-e50e24dcca9e"
TRANSCRIPT_UUID = "7e400002-b5a3-f393-e0a9-e50e24dcca9e"
STATUS_UUID = "7e400003-b5a3-f393-e0a9-e50e24dcca9e"


class BleCodexTransport:
    """BLE central: ESP notifies transcripts; the relay writes status snapshots."""

    def __init__(self, enabled: bool, device_name: str, token: str, dispatcher: TaskDispatcher):
        self.enabled = enabled
        self.device_name = device_name
        self.token = token
        self.dispatcher = dispatcher
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, name="ble-codex", daemon=True)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Any = None
        self._buffer = bytearray()
        self._write_lock: asyncio.Lock | None = None
        self._write_pending = threading.Event()
        self._status_ready_logged = False

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop:
            self._loop.call_soon_threadsafe(lambda: None)

    def feed_notification(self, data: bytes) -> None:
        self._buffer.extend(data)
        if len(self._buffer) > 64 * 1024:
            self._buffer.clear()
            return
        while b"\n" in self._buffer:
            raw, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            try:
                payload = json.loads(raw.decode("utf-8"))
                if not hmac.compare_digest(str(payload.get("token", "")), self.token):
                    continue
                self.dispatcher.enqueue(
                    str(payload.get("transcript", "")),
                    str(payload.get("source", "esp32-ble"))[:64],
                    str(payload.get("request_id", "")).strip() or None,
                )
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                continue

    @staticmethod
    def _encode_status(payload: dict[str, Any]) -> bytes:
        # The HTTP payload retains compatibility metadata, but BLE only needs
        # the single conversation rendered by the 400x300 display. Avoid
        # sending the same agent twice through ``latest`` and ``all_agents``.
        agents = payload.get("all_agents")
        compact = {
            "type": "status",
            "ts": payload.get("ts"),
            "all_agents": list(agents[:1]) if isinstance(agents, list) else [],
            "active_count": payload.get("active_count", 0),
        }
        data = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) <= 1024:
            return data

        # Defensive fallback for unusually long multi-byte text. Prefer
        # keeping the user's request and trim only the answer shown on screen.
        agent = compact["all_agents"][0] if compact["all_agents"] else None
        if isinstance(agent, dict):
            agent = dict(agent)
            compact["all_agents"] = [agent]
            answer = str(agent.get("last_assistant") or "")
            while answer and len(data) > 1024:
                answer = answer[:-8].rstrip() + "…"
                agent["last_assistant"] = answer
                data = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        return data

    def publish_status(self, payload: dict[str, Any]) -> None:
        loop, client = self._loop, self._client
        if (not loop or not client or not getattr(client, "is_connected", False)
                or self._write_pending.is_set()):
            return
        data = self._encode_status(payload)
        if len(data) > 1024:
            print(f"BLE status frame too large: {len(data)} bytes")
            return
        self._write_pending.set()
        try:
            future = asyncio.run_coroutine_threadsafe(self._write_status(data), loop)
        except Exception:
            self._write_pending.clear()
            raise

        def completed(result: Any) -> None:
            self._write_pending.clear()
            try:
                result.result()
            except Exception as exc:
                print(f"BLE status write failed: {exc}")

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
            # ATT writes can carry at most MTU-3 bytes. The ESP advertises an
            # MTU of 128, while Windows may temporarily report the default 23.
            chunk_size = max(20, min(120, mtu_size - 3))
            for offset in range(0, len(data), chunk_size):
                await client.write_gatt_char(
                    STATUS_UUID,
                    data[offset : offset + chunk_size],
                    response=True,
                )
                await asyncio.sleep(0.005)
            if not self._status_ready_logged:
                self._status_ready_logged = True
                print(f"BLE status channel ready: mtu={mtu_size}, frame={len(data)} bytes")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except Exception as exc:
            print(f"BLE transport stopped: {exc}")

    async def _run(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            print("BLE enabled but bleak is not installed; run: pip install -e .[ble]")
            return
        self._loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                device = await BleakScanner.find_device_by_filter(
                    lambda d, ad: d.name == self.device_name
                    or SERVICE_UUID in [uuid.lower() for uuid in ad.service_uuids],
                    timeout=8,
                )
                if device is None:
                    await asyncio.sleep(2)
                    continue
                async with BleakClient(device, timeout=10) as client:
                    self._client = client
                    await client.start_notify(TRANSCRIPT_UUID, lambda _sender, data: self.feed_notification(bytes(data)))
                    print(f"BLE connected: {self.device_name}")
                    while client.is_connected and not self._stop.is_set():
                        await asyncio.sleep(1)
            except Exception as exc:
                print(f"BLE reconnecting: {exc}")
                await asyncio.sleep(3)
            finally:
                self._client = None
