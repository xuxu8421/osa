"""
BLE connector for the HSR 1A2.0 chest band.

Uses bleak to scan, connect, discover services, subscribe to notifications,
and feed raw bytes into the protocol parser.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Optional, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic

from .chestband_protocol import (
    PacketParser, DataPacket, build_register_response, build_rtc_set,
)


# Common name patterns for HSR chest band devices
DEVICE_NAME_HINTS = ['1A2', 'HSR', 'SRG', 'SRHEALTH', 'sleepace']


class ChestBandBLE:
    """Manages BLE connection to the HSR chest band."""

    def __init__(self):
        self.parser = PacketParser()
        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self._write_char = None
        self._notify_char = None

        self.parser.on_registration = self._on_registration

    @staticmethod
    async def scan(timeout: float = 8.0,
                   named_only: bool = True,
                   chestband_only: bool = False
                   ) -> list[tuple[BLEDevice, int]]:
        """
        Scan for BLE devices.

        Returns list of (device, rssi) sorted by RSSI (strongest first).

        named_only:     drop devices without a broadcast/local name
        chestband_only: further restrict to names matching HSR hints
        """
        print(f"扫描 BLE 设备中 ({timeout}s)...")
        pairs = await BleakScanner.discover(
            timeout=timeout, return_adv=True)
        results: list[tuple[BLEDevice, int]] = []
        for addr, (dev, adv) in pairs.items():
            name = adv.local_name or dev.name or ''
            if named_only and not name.strip():
                continue
            if chestband_only:
                up = name.upper()
                if not any(h in up for h in DEVICE_NAME_HINTS):
                    continue
            rssi = adv.rssi if adv.rssi is not None else -127
            results.append((dev, rssi))
        results.sort(key=lambda t: -t[1])
        return results

    async def connect(self, device: BLEDevice):
        """Connect to a device, discover services, find data characteristic."""
        self.device = device
        print(f"连接到 {device.name} ({device.address})...")
        self.client = BleakClient(device, timeout=15.0)
        await self.client.connect()
        print("已连接")

        await self._discover_characteristics()

    async def _discover_characteristics(self):
        """Find writable and notify characteristics."""
        print("\n服务和特征:")
        for svc in self.client.services:
            print(f"  Service: {svc.uuid}")
            for char in svc.characteristics:
                props = ','.join(char.properties)
                print(f"    Char: {char.uuid}  [{props}]")

                if 'notify' in char.properties or 'indicate' in char.properties:
                    if self._notify_char is None:
                        self._notify_char = char
                        print(f"      -> 选为数据接收特征")

                if 'write' in char.properties or 'write-without-response' in char.properties:
                    if self._write_char is None:
                        self._write_char = char
                        print(f"      -> 选为数据发送特征")

        if self._notify_char is None:
            print("\n警告: 未找到 notify 特征, 尝试订阅所有可通知特征")

    async def start_receiving(self, on_data: Callable[[DataPacket], None]):
        """Subscribe to notifications and start receiving data."""
        self.parser.on_data = on_data

        if self._notify_char:
            await self.client.start_notify(
                self._notify_char.uuid, self._on_ble_data)
            print(f"\n已订阅通知: {self._notify_char.uuid}")
        else:
            # Subscribe to ALL notify-capable characteristics
            for svc in self.client.services:
                for char in svc.characteristics:
                    if 'notify' in char.properties:
                        try:
                            await self.client.start_notify(
                                char.uuid, self._on_ble_data)
                            print(f"已订阅: {char.uuid}")
                        except Exception as e:
                            print(f"订阅失败 {char.uuid}: {e}")

        print("等待数据...\n")

    def _on_ble_data(self, _sender: BleakGATTCharacteristic, data: bytearray):
        """BLE notification callback — feed raw bytes to parser."""
        self.parser.feed(bytes(data))

    async def _on_registration(self, device_id_bytes: bytes):
        """Respond to device registration request."""
        if self._write_char and self.client and self.client.is_connected:
            resp = build_register_response(device_id_bytes)
            try:
                await self.client.write_gatt_char(
                    self._write_char.uuid, resp)
                print(f"已响应设备注册: DID={device_id_bytes.hex()}")
                # Also set RTC
                rtc = build_rtc_set(device_id_bytes)
                await self.client.write_gatt_char(
                    self._write_char.uuid, rtc)
                print("已同步 RTC 时间")
            except Exception as e:
                print(f"写入失败: {e}")

    async def disconnect(self):
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            print("已断开连接")
