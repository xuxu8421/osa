"""
BLE connector + protocol parser for Creative PC-68B (and family) pulse oximeter.

Shenzhen Creative's PC-60NW / PC-68B family use Bluetooth LE with a handful of
well-known service UUIDs; on macOS the device advertises itself with a name
like `PC-68B`, `CMS50`, `Creative`, etc. (varies by firmware).

Because we don't yet have the exact protocol document, this module:

  1. Tries a list of known Creative service UUIDs first.
  2. Falls back to subscribing to every notify-capable characteristic.
  3. Emits every raw frame via `on_raw_frame(hex_str)` so we can inspect.
  4. Runs a best-effort parser covering the most common Creative frame layouts
     (AA 55 … checksum,  0x81 real-time, 0x80 pleth). Values that don't match
     known patterns are left as None.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional, List

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.characteristic import BleakGATTCharacteristic


# Device name hints (upper-cased, partial match)
DEVICE_NAME_HINTS = ['PC-68', 'PC68', 'CMS50', 'CREATIVE', 'CHOICEMMED',
                     'POD', 'SP-20', 'PULSEOX']

# Well-known Creative / PC-60NW / Nordic-UART-style service UUIDs
KNOWN_SERVICE_UUIDS = [
    'cdeacb80-5235-4c07-8846-93a37ee6b86d',  # Creative PC-60NW/PC-66/PC-68
    '6e400001-b5a3-f393-e0a9-e50e24dcca9e',  # Nordic UART
    '0000fff0-0000-1000-8000-00805f9b34fb',  # Some CMS50 variants
    '0000ffe0-0000-1000-8000-00805f9b34fb',  # HC-05/HM-10 compatible
    '49535343-fe7d-4ae5-8fa9-9fafd205e455',  # Microchip BM71 (Feasycom-like)
]

KNOWN_NOTIFY_UUIDS = [
    'cdeacb81-5235-4c07-8846-93a37ee6b86d',
    '6e400003-b5a3-f393-e0a9-e50e24dcca9e',
    '0000fff1-0000-1000-8000-00805f9b34fb',
    '0000ffe1-0000-1000-8000-00805f9b34fb',
    '49535343-1e4d-4bd9-ba61-23c647249616',
]

KNOWN_WRITE_UUIDS = [
    'cdeacb82-5235-4c07-8846-93a37ee6b86d',
    '6e400002-b5a3-f393-e0a9-e50e24dcca9e',
    '0000fff2-0000-1000-8000-00805f9b34fb',
    '0000ffe1-0000-1000-8000-00805f9b34fb',  # HC-05 shared chan
    '49535343-8841-43f4-a8d4-ecbe34729bb3',
]


@dataclass
class OxiReading:
    """One latched reading. All fields optional — any parser leaves unknowns None."""
    spo2_pct: Optional[int] = None       # 0-100
    pulse_rate: Optional[int] = None     # bpm
    pi: Optional[float] = None           # perfusion index %
    pleth: Optional[int] = None          # instantaneous pleth sample 0-100
    finger_out: bool = False
    probe_error: bool = False
    battery_low: bool = False


@dataclass
class OxiFrame:
    """A single raw BLE notification frame."""
    t: float
    hex: str


class OximeterBLE:
    """BLE connector + parser for PC-68B family."""

    def __init__(self):
        self.client: Optional[BleakClient] = None
        self.device: Optional[BLEDevice] = None
        self._notify_char: Optional[BleakGATTCharacteristic] = None
        self._write_char: Optional[BleakGATTCharacteristic] = None
        self._extra_notify: List[BleakGATTCharacteristic] = []
        self._all_write_chars: List[BleakGATTCharacteristic] = []

        # Streaming parse state
        self._buf = bytearray()

        # Callbacks
        self.on_reading: Optional[Callable[[OxiReading], None]] = None
        self.on_raw_frame: Optional[Callable[[bytes], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None

        # Latched most recent reading
        self.latest = OxiReading()
        self.pkt_count = 0
        self.byte_count = 0           # total raw bytes received
        self.gatt_summary: str = ''    # human-readable GATT dump
        self.subscribed_uuids: list[str] = []

    # ── scanning ──

    @staticmethod
    async def scan(timeout: float = 8.0,
                   named_only: bool = True,
                   oximeter_only: bool = False
                   ) -> list[tuple[BLEDevice, int]]:
        pairs = await BleakScanner.discover(
            timeout=timeout, return_adv=True)
        out: list[tuple[BLEDevice, int]] = []
        for addr, (dev, adv) in pairs.items():
            name = adv.local_name or dev.name or ''
            if named_only and not name.strip():
                continue
            if oximeter_only:
                up = name.upper()
                if not any(h in up for h in DEVICE_NAME_HINTS):
                    continue
            rssi = adv.rssi if adv.rssi is not None else -127
            out.append((dev, rssi))
        out.sort(key=lambda t: -t[1])
        return out

    # ── connect ──

    async def connect(self, device: BLEDevice):
        self.device = device
        print(f"[OXI] 连接 {device.name} ({device.address})...")
        self.client = BleakClient(device, timeout=15.0)
        await self.client.connect()
        print("[OXI] 已连接, 枚举 GATT...")

        await self._discover()

    async def _discover(self):
        """Pick the best notify & write characteristics."""
        notify_candidates: list[BleakGATTCharacteristic] = []
        write_candidates: list[BleakGATTCharacteristic] = []

        lines: list[str] = []
        for svc in self.client.services:
            lines.append(f"Svc {svc.uuid}")
            for ch in svc.characteristics:
                props = ','.join(ch.properties)
                # abbreviate standard 16-bit UUID
                short = ch.uuid
                if short.endswith('-0000-1000-8000-00805f9b34fb'):
                    short = short[4:8]
                lines.append(f"  Chr {short}  [{props}]")
                if 'notify' in ch.properties or 'indicate' in ch.properties:
                    notify_candidates.append(ch)
                if 'write' in ch.properties or 'write-without-response' in ch.properties:
                    write_candidates.append(ch)
        self.gatt_summary = '\n'.join(lines)
        self._log(self.gatt_summary)

        # Prefer known UUIDs
        self._notify_char = self._pick_best(notify_candidates, KNOWN_NOTIFY_UUIDS)
        self._write_char = self._pick_best(write_candidates, KNOWN_WRITE_UUIDS)
        self._all_write_chars = list(write_candidates)

        if self._notify_char:
            self._extra_notify = [c for c in notify_candidates
                                  if c.uuid != self._notify_char.uuid]
        else:
            self._extra_notify = notify_candidates

        self._log(f"主 notify: {self._notify_char.uuid if self._notify_char else '(无)'}")
        self._log(f"主 write : {self._write_char.uuid if self._write_char else '(无)'}")
        self._log(f"额外 notify: {len(self._extra_notify)} 个")
        self._log(f"可写特征共 {len(self._all_write_chars)} 个")

    def list_write_uuids(self) -> list[str]:
        return [c.uuid for c in self._all_write_chars]

    def _log(self, msg: str):
        if self.on_log:
            try: self.on_log(msg)
            except Exception: pass

    @staticmethod
    def _pick_best(chars: list[BleakGATTCharacteristic],
                   known_uuids: list[str]) -> Optional[BleakGATTCharacteristic]:
        known_lc = [u.lower() for u in known_uuids]
        for c in chars:
            if c.uuid.lower() in known_lc:
                return c
        return chars[0] if chars else None

    # ── receive ──

    async def start_receiving(self):
        """Subscribe to ALL notify/indicate characteristics for discovery."""
        all_notify = (
            [self._notify_char] if self._notify_char else []
        ) + list(self._extra_notify)

        self.subscribed_uuids = []
        for c in all_notify:
            if c is None:
                continue
            try:
                await self.client.start_notify(c.uuid, self._on_ble_data)
                self.subscribed_uuids.append(c.uuid)
                self._log(f"已订阅 {c.uuid}")
            except Exception as e:
                self._log(f"订阅失败 {c.uuid}: {e}")

        if not self.subscribed_uuids:
            self._log("没有可订阅的 notify/indicate 特征")

        # Some Creative devices require a wake/start command
        await self._send_start_command()

    async def _send_start_command(self):
        """Try known vendor wake/start commands on ALL writable characteristics."""
        if not self._all_write_chars:
            self._log("无可写特征, 跳过唤醒命令")
            return
        cmds = [
            # ── FFB0-family (Contec CMS50D/E/F) ──
            ('FFB0 start msmt',      bytes.fromhex('7d 81 a1 80 80 80 80 80 80 a7')),
            ('FFB0 keep-alive A5',   bytes.fromhex('a5')),
            ('FFB0 broadcast req',   bytes.fromhex('55 aa 00 01 02 01 00 03')),
            ('Contec open',          bytes.fromhex('55 aa 00 02 04 02 00 00 04')),
            # ── PC-60NW family ──
            ('PC-60NW get status',   bytes.fromhex('7d 81 a2 80 80 80 80 80 80 a8')),
            ('PC-60NW real-time',    bytes.fromhex('7d 81 a7 80 80 80 80 80 80 ad')),
            ('PC-60NW start alt',    bytes.fromhex('7d 81 af 80 80 80 80 80 80 b5')),
            # ── Lepu / Viatom: AA header with CRC-8 ──
            ('Lepu INFO req',        self._lepu_cmd(0x14)),
            ('Lepu real-time',       self._lepu_cmd(0x17)),
            # ── Simple pings ──
            ('wake 00',              bytes.fromhex('00')),
            ('wake FF',              bytes.fromhex('ff')),
        ]
        for ch in self._all_write_chars:
            self._log(f"-- 尝试向 {ch.uuid} 写 {len(cmds)} 条命令 --")
            for label, cmd in cmds:
                try:
                    await self.client.write_gatt_char(
                        ch.uuid, cmd, response=False)
                    self._log(f"  [{label}] -> {cmd.hex(' ')}")
                    await asyncio.sleep(0.15)
                except Exception as e:
                    self._log(f"  [{label}] 写失败: {e}")

    @staticmethod
    def _lepu_cmd(cmd: int, block_id: int = 0, payload: bytes = b'') -> bytes:
        """Lepu/Viatom AA-header packet with CRC-8."""
        pkt = bytearray([0xAA, cmd, cmd ^ 0xFF])
        pkt.extend(block_id.to_bytes(2, 'little'))
        pkt.extend(len(payload).to_bytes(2, 'little'))
        pkt.extend(payload)
        pkt.append(OximeterBLE._crc8(bytes(pkt)))
        return bytes(pkt)

    @staticmethod
    def _crc8(data: bytes) -> int:
        crc = 0
        for b in data:
            chk = crc ^ b
            crc = 0
            if chk & 0x01: crc = 0x07
            if chk & 0x02: crc ^= 0x0e
            if chk & 0x04: crc ^= 0x1c
            if chk & 0x08: crc ^= 0x38
            if chk & 0x10: crc ^= 0x70
            if chk & 0x20: crc ^= 0xe0
            if chk & 0x40: crc ^= 0xc7
            if chk & 0x80: crc ^= 0x89
        return crc

    async def write_raw(self, hex_or_bytes,
                        target_uuid: Optional[str] = None,
                        with_response: bool = False):
        """Manual write from UI — accepts hex string or bytes, optional target char."""
        if not self._all_write_chars:
            self._log("无可写特征")
            return
        target = target_uuid or (self._write_char.uuid if self._write_char else None)
        if target is None:
            self._log("无目标特征")
            return
        if isinstance(hex_or_bytes, str):
            s = hex_or_bytes.replace(' ', '').replace(',', '')
            data = bytes.fromhex(s)
        else:
            data = bytes(hex_or_bytes)
        try:
            await self.client.write_gatt_char(
                target, data, response=with_response)
            self._log(f"手动发送 -> {target[4:8]}: {data.hex(' ')}")
        except Exception as e:
            self._log(f"手动发送失败 {target}: {e}")

    def _on_ble_data(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Called on each BLE notification."""
        b = bytes(data)
        self.pkt_count += 1
        self.byte_count += len(b)
        if self.on_raw_frame:
            try:
                self.on_raw_frame(b)
            except Exception:
                pass
        self._feed(b)

    # ── parse ──

    def _feed(self, data: bytes):
        """
        Streaming parser. Creative family frames all start with 0x80 or 0x81
        (bit 7 set); subsequent bytes have bit 7 clear. So a frame boundary is
        'next byte with bit 7 set'.
        """
        self._buf.extend(data)
        # Split into frames at boundary bytes (>= 0x80)
        i = 0
        frames = []
        cur_start = None
        while i < len(self._buf):
            if self._buf[i] & 0x80:
                if cur_start is not None:
                    frames.append(bytes(self._buf[cur_start:i]))
                cur_start = i
            i += 1
        # Keep last unfinished frame in buffer
        if cur_start is not None:
            self._buf = bytearray(self._buf[cur_start:])
        else:
            self._buf.clear()

        for f in frames:
            self._parse_frame(f)

    def _parse_frame(self, f: bytes):
        """Best-effort Creative frame parser."""
        if not f:
            return
        head = f[0]
        # Creative PC-60NW real-time frame: 0x81 + 7 data bytes
        # Known layout (approximate):
        #   [0] 0x81 | flags_bit0=probe_error, bit1=finger_out, bit2=searching
        #   [1] pleth (0-100, 7 bits)
        #   [2] bar/intensity
        #   [3] pulse_rate (low 7 bits) | bit6 = pulse beep, bit5 = PR high bit
        #   [4] SpO2 (7 bits)
        #   [5] PI low 7 bits (÷10 = %)
        #   [6] PI high bits / status
        # Different revisions vary; we pattern-match what we can.
        if head == 0x81 and len(f) >= 5:
            r = OxiReading()
            flags = head & 0x0F
            r.probe_error = bool(flags & 0x01)
            r.finger_out = bool(flags & 0x02)
            r.pleth = f[1] & 0x7F
            pr_lo = f[3] & 0x7F
            pr_hi = (f[3] >> 6) & 0x01  # bit 6 of byte 3 may be PR MSB
            pr = (pr_hi << 7) | pr_lo
            if pr == 0 or pr == 0x7F:
                pr = None
            r.pulse_rate = pr
            spo2 = f[4] & 0x7F
            if spo2 == 0x7F or spo2 == 0:
                spo2 = None
            r.spo2_pct = spo2
            if len(f) >= 6:
                pi_raw = f[5] & 0x7F
                r.pi = pi_raw / 10.0 if pi_raw > 0 and pi_raw < 0x7F else None

            # Latch non-null
            if r.spo2_pct is not None:
                self.latest.spo2_pct = r.spo2_pct
            if r.pulse_rate is not None:
                self.latest.pulse_rate = r.pulse_rate
            if r.pi is not None:
                self.latest.pi = r.pi
            if r.pleth is not None:
                self.latest.pleth = r.pleth
            self.latest.finger_out = r.finger_out
            self.latest.probe_error = r.probe_error

            if self.on_reading:
                self.on_reading(self.latest)

        # 0x80 pleth-only frames (high-rate waveform) — ignored for now

    # ── disconnect ──

    async def disconnect(self):
        try:
            if self.client and self.client.is_connected:
                if self._notify_char:
                    try:
                        await self.client.stop_notify(self._notify_char.uuid)
                    except Exception:
                        pass
                await self.client.disconnect()
                print("[OXI] 已断开")
        except Exception as e:
            print(f"[OXI] 断开出错: {e}")
