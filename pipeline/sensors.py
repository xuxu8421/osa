"""
Sensor abstraction layer.

Every concrete data source (chest band, oximeter, headset IMU, or a replay
file) implements `Sensor`. When started, a sensor publishes events to an
`EventBus`; the rest of the pipeline subscribes by topic — no subscriber ever
depends on a specific driver.

Topics published:
  * chestband.data      payload = DataPacket (per-second)
  * oximeter.reading    payload = OxiReading
  * headset.imu         payload = dict{accel, gyro} (TBD when we get HW)
  * sensor.status       payload = {name, status, detail}

Status strings: 'idle' | 'connecting' | 'streaming' | 'stopped' | 'error'.
"""

from __future__ import annotations

import asyncio
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .events import EventBus


@dataclass
class SensorStatus:
    name: str
    status: str
    detail: str = ''


class Sensor(ABC):
    """Abstract sensor. Implementations are free to use their own threads."""

    name: str = 'sensor'

    def __init__(self):
        self.bus: Optional[EventBus] = None
        self.status: str = 'idle'

    # ── lifecycle ──

    def attach(self, bus: EventBus):
        self.bus = bus

    @abstractmethod
    def start(self) -> None:
        """Begin streaming. Returns immediately; work happens in a bg thread."""

    @abstractmethod
    def stop(self) -> None:
        """Stop streaming and release resources."""

    # ── helpers ──

    def _set_status(self, status: str, detail: str = ''):
        self.status = status
        if self.bus:
            self.bus.emit('sensor.status',
                          SensorStatus(self.name, status, detail),
                          src=self.name)

    def _emit(self, kind: str, payload: Any):
        if self.bus:
            self.bus.emit(kind, payload, src=self.name)


# ─────────────────────────── Chest band ───────────────────────────────────


class ChestBandSensor(Sensor):
    """Adapter: wraps a connected ChestBandBLE into the Sensor API."""

    name = 'chestband'

    def __init__(self, ble, loop: asyncio.AbstractEventLoop):
        """
        Args:
          ble:  an already-constructed devices.chestband.ChestBandBLE
          loop: the asyncio loop it runs on (we don't create one here —
                the caller is responsible for the BLE event loop)
        """
        super().__init__()
        self.ble = ble
        self.loop = loop

    def start(self):
        if self.bus is None:
            raise RuntimeError("attach(bus) before start()")

        def _on_data(dp):
            self._emit('chestband.data', dp)

        self._set_status('connecting')
        fut = asyncio.run_coroutine_threadsafe(
            self.ble.start_receiving(_on_data), self.loop)

        def _done(f):
            try:
                f.result()
                self._set_status('streaming')
            except Exception as e:
                self._set_status('error', str(e))

        fut.add_done_callback(_done)

    def stop(self):
        async def _disc():
            try:
                await self.ble.disconnect()
            except Exception:
                pass
        if self.loop:
            asyncio.run_coroutine_threadsafe(_disc(), self.loop)
        self._set_status('stopped')


# ─────────────────────────── Oximeter ─────────────────────────────────────


class OximeterSensor(Sensor):
    """Adapter for OximeterBLE. Vendor protocol not solved yet, so if the
    device doesn't push any bytes, we just stay 'connecting' and emit nothing.
    Offline CSV import (PC software) can feed the same topic via `inject`."""

    name = 'oximeter'

    def __init__(self, ble, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.ble = ble
        self.loop = loop

    def start(self):
        if self.bus is None:
            raise RuntimeError("attach(bus) before start()")

        def _on_reading(r):
            self._emit('oximeter.reading', r)

        self.ble.on_reading = _on_reading
        self._set_status('streaming')

    def stop(self):
        async def _disc():
            try:
                await self.ble.disconnect()
            except Exception:
                pass
        if self.loop:
            asyncio.run_coroutine_threadsafe(_disc(), self.loop)
        self._set_status('stopped')

    def inject(self, reading):
        """Push a reading obtained from offline USB export."""
        self._emit('oximeter.reading', reading)


# ─────────────────────────── Headset (stub) ───────────────────────────────


class HeadsetSensor(Sensor):
    """Placeholder for the headset IMU/audio platform. Spec is TBD. When
    the real SDK arrives we keep this class name and swap the internals."""

    name = 'headset'

    def start(self):
        self._set_status('error', '耳机平台尚未集成 (留桩位)')

    def stop(self):
        self._set_status('stopped')


# ─────────────────────────── Mock / test ──────────────────────────────────


class MockSensor(Sensor):
    """Emits synthetic chest-band-like frames at 1 Hz for development."""

    name = 'mock'

    def __init__(self, period_s: float = 1.0):
        super().__init__()
        self.period = period_s
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None

    def start(self):
        if self.bus is None:
            raise RuntimeError("attach(bus) before start()")
        self._stop.clear()
        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()
        self._set_status('streaming')

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(timeout=1.0)
        self._set_status('stopped')

    def _run(self):
        i = 0
        while not self._stop.is_set():
            self._emit('mock.tick', {'i': i, 't': time.time()})
            i += 1
            self._stop.wait(self.period)
