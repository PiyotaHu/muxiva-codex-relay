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

    def publish_status(self, payload: dict[str, Any]) -> None:
        loop, client = self._loop, self._client
        if not loop or not client or not getattr(client, "is_connected", False):
            return
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        asyncio.run_coroutine_threadsafe(self._write_status(data), loop)

    async def _write_status(self, data: bytes) -> None:
        if not self._client or not self._client.is_connected:
            return
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        async with self._write_lock:
            for offset in range(0, len(data), 180):
                await self._client.write_gatt_char(STATUS_UUID, data[offset : offset + 180], response=False)

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
