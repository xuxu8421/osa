"""
Binary protocol parser for the HSR 1A2.0 chest band.

Packet format:
  [0x55][0xAA][len_H][len_L][DID x4][FrameType][payload...][checksum]

Length = total bytes after header (including the 2 length bytes themselves).
Checksum = low byte of sum of all bytes before it.

Periodic data (FrameType 0x03) is split into 4 sub-packets per second,
each with sub_sn 0-3, sent at 100ms intervals.

Reference: 1A2.0 无线数据传输协议说明 V1.05
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional, Callable

import numpy as np

HEADER = b'\x55\xAA'

# Frame types
FT_REGISTER_REQ  = 0x01
FT_REGISTER_RESP = 0x02
FT_DATA          = 0x03
FT_RTC_SET       = 0x09
FT_STATUS_CTRL   = 0x0B
FT_UNREGISTER    = 0x0C
FT_BLE_CTRL      = 0x0D


@dataclass
class VitalSigns:
    """Parsed vital signs from sub-packet 2."""
    spo2_pct: int = 0           # SpO2 percentage
    pulse_rate: int = 0         # beats/min
    heart_rate: int = 0         # beats/min
    resp_rate: int = 0          # breaths/min
    gesture: int = 0            # 0=unknown, posture byte
    temperature: float = 0.0
    battery_voltage_mv: int = 0
    device_status: int = 0


@dataclass
class DataPacket:
    """One complete second of chest band data (assembled from 4 sub-packets)."""
    timestamp: float = 0.0       # device unix timestamp
    timestamp_ms: int = 0        # milliseconds part
    device_id: int = 0
    packet_sn: int = 0

    # Sub-packet 0: chest respiration + ECG ch1-2
    chest_resp: Optional[np.ndarray] = None      # (25,) int16
    ecg_ch1: Optional[np.ndarray] = None         # (50,) int16 (10-bit)
    ecg_ch2: Optional[np.ndarray] = None         # (50,) int16

    # Sub-packet 1: ECG ch3-4 + abdominal respiration
    ecg_ch3: Optional[np.ndarray] = None
    ecg_ch4: Optional[np.ndarray] = None
    abd_resp: Optional[np.ndarray] = None        # (25,) int16

    # Sub-packet 2: accel + spo2 + vitals
    accel_x: Optional[np.ndarray] = None         # (25,) int16 (10-bit)
    accel_y: Optional[np.ndarray] = None
    accel_z: Optional[np.ndarray] = None
    spo2_wave: Optional[np.ndarray] = None       # (50,) uint8 (7-bit + pulse flag)
    vitals: VitalSigns = field(default_factory=VitalSigns)

    # Sub-packet 3: spirometer (optional)
    spirometer: Optional[np.ndarray] = None

    # Respiration coefficients (from sub-packet 2)
    chest_resp_coeff: int = 0
    abd_resp_coeff: int = 0

    _received_subs: set = field(default_factory=set)

    @property
    def complete(self) -> bool:
        return {0, 1, 2}.issubset(self._received_subs)


def compute_checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def build_register_response(device_id: bytes, success: bool = True) -> bytes:
    """Build a registration response packet to send to the device."""
    ts = int(time.time())
    payload = bytearray()
    payload.append(0xFF if success else 0x00)
    payload += struct.pack('>I', ts)
    return _build_packet(device_id, FT_REGISTER_RESP, payload)


def build_rtc_set(device_id: bytes) -> bytes:
    """Build an RTC time-set packet."""
    ts = int(time.time())
    payload = struct.pack('>I', ts)
    return _build_packet(device_id, FT_RTC_SET, payload)


def _build_packet(device_id: bytes, frame_type: int, payload: bytes) -> bytes:
    did = device_id[:4].ljust(4, b'\x00')
    length = 2 + 4 + 1 + len(payload) + 1  # len_field + did + type + payload + checksum
    pkt = bytearray(HEADER)
    pkt += struct.pack('>H', length)
    pkt += did
    pkt.append(frame_type)
    pkt += payload
    pkt.append(compute_checksum(pkt))
    return bytes(pkt)


class PacketParser:
    """Streaming parser: feed raw BLE bytes, get parsed packets out."""

    def __init__(self):
        self._buf = bytearray()
        self._assembler: dict[int, DataPacket] = {}  # packet_sn -> DataPacket
        self.on_registration: Optional[Callable] = None
        self.on_data: Optional[Callable[[DataPacket], None]] = None

    def feed(self, data: bytes):
        """Feed raw bytes from BLE notification. Parses all complete packets."""
        self._buf.extend(data)
        while True:
            idx = self._buf.find(HEADER)
            if idx < 0:
                self._buf.clear()
                break
            if idx > 0:
                self._buf = self._buf[idx:]

            if len(self._buf) < 4:
                break
            length = struct.unpack('>H', self._buf[2:4])[0]
            total = 2 + length  # header + rest
            if len(self._buf) < total:
                break

            pkt = bytes(self._buf[:total])
            self._buf = self._buf[total:]

            expected_cs = compute_checksum(pkt[:-1])
            if pkt[-1] != expected_cs:
                continue

            self._handle_packet(pkt)

    def _handle_packet(self, pkt: bytes):
        did = struct.unpack('>I', pkt[4:8])[0]
        ft = pkt[8]

        if ft == FT_REGISTER_REQ:
            if self.on_registration:
                self.on_registration(pkt[4:8])

        elif ft == FT_DATA:
            self._parse_data(pkt, did)

    def _parse_data(self, pkt: bytes, did: int):
        payload = pkt[9:-1]  # skip header(2)+len(2)+did(4)+type(1), drop checksum
        if len(payload) < 5:
            return
        sn = struct.unpack('>I', payload[0:4])[0]
        sub_sn = payload[4]

        if sn not in self._assembler:
            self._assembler[sn] = DataPacket(device_id=did, packet_sn=sn)
        dp = self._assembler[sn]

        if sub_sn == 0:
            self._parse_sub0(dp, payload)
        elif sub_sn == 1:
            self._parse_sub1(dp, payload)
        elif sub_sn == 2:
            self._parse_sub2(dp, payload)
        elif sub_sn == 3:
            self._parse_sub3(dp, payload)

        dp._received_subs.add(sub_sn)

        if dp.complete and self.on_data:
            self.on_data(dp)
            self._assembler.pop(sn, None)

        # Clean old incomplete packets (keep last 5)
        if len(self._assembler) > 5:
            oldest = sorted(self._assembler.keys())[:-5]
            for k in oldest:
                self._assembler.pop(k, None)

    def _parse_sub0(self, dp: DataPacket, p: bytes):
        """Sub-packet 0: time + chest resp (25 x 16bit) + ECG ch1-2 (50 x 10bit each)."""
        if len(p) < 5 + 6 + 50 + 63:
            return
        off = 5  # skip sn(4) + sub_sn(1)

        # Device time: 4 bytes unix + 2 bytes ms
        dp.timestamp = struct.unpack('>I', p[off:off+4])[0]
        dp.timestamp_ms = struct.unpack('>H', p[off+4:off+6])[0]
        off += 6

        # Chest respiration: 25 samples × 2 bytes, big-endian UNSIGNED
        # (protocol formula applies `65535 - raw`; treating as signed
        # flips the high half of the range, producing bogus -20000 spikes
        # in the plot.)
        dp.chest_resp = np.array(
            [struct.unpack('>H', p[off+i*2:off+i*2+2])[0] for i in range(25)],
            dtype=np.uint16)
        off += 50

        # ECG ch1: 10-bit packed (13 bytes high + 50 bytes low)
        dp.ecg_ch1 = _unpack_10bit(p, off, 50)
        off += 13 + 50

        # ECG ch2
        dp.ecg_ch2 = _unpack_10bit(p, off, 50)

    def _parse_sub1(self, dp: DataPacket, p: bytes):
        """Sub-packet 1: ECG ch3-4 + abdominal resp."""
        off = 5

        dp.ecg_ch3 = _unpack_10bit(p, off, 50)
        off += 13 + 50

        dp.ecg_ch4 = _unpack_10bit(p, off, 50)
        off += 13 + 50

        # Abdominal respiration: 25 samples × 2 bytes unsigned
        if off + 50 <= len(p):
            dp.abd_resp = np.array(
                [struct.unpack('>H', p[off+i*2:off+i*2+2])[0] for i in range(25)],
                dtype=np.uint16)

    def _parse_sub2(self, dp: DataPacket, p: bytes):
        """Sub-packet 2: accel XYZ + SpO2 wave + vitals."""
        off = 5

        # Accelerometer XYZ: 25 samples x 10-bit each
        dp.accel_x = _unpack_10bit(p, off, 25)
        off += 7 + 25
        dp.accel_y = _unpack_10bit(p, off, 25)
        off += 7 + 25
        dp.accel_z = _unpack_10bit(p, off, 25)
        off += 7 + 25

        # SpO2 waveform: 50 bytes (D7=pulse flag, D6-D0=value 0-127)
        if off + 50 <= len(p):
            dp.spo2_wave = np.array(list(p[off:off+50]), dtype=np.uint8)
        off += 50

        # Parameters section
        if off + 27 > len(p):
            return

        # Temperature time: 4 bytes (skip)
        off += 4

        # Byte 164: flags
        param_hi = p[off]; off += 1
        # Byte 165: spo2 signal strength
        off += 1
        # Byte 166: chest resp coeff
        dp.chest_resp_coeff = p[off]; off += 1
        # Byte 167: abd resp coeff
        dp.abd_resp_coeff = p[off]; off += 1
        # Byte 168: temperature
        temp_low = p[off]; off += 1
        temp_hi_bit = (param_hi >> 4) & 0x01
        temp_raw = (temp_hi_bit << 8) | temp_low
        dp.vitals.temperature = temp_raw * 0.1 if temp_raw > 0 else 0

        # Byte 169: SpO2%
        dp.vitals.spo2_pct = p[off]; off += 1
        # Byte 170: device status
        dp.vitals.device_status = p[off]; off += 1
        # Byte 171: battery alerts (skip)
        off += 1
        # Byte 172: status switches (skip)
        off += 1
        # Byte 173: battery type
        off += 1
        # Byte 174: battery voltage
        batt_v = p[off]; off += 1
        if 15 <= batt_v <= 255:
            dp.vitals.battery_voltage_mv = 3200 + (batt_v - 15) * 5
        # Byte 175: wifi (skip)
        off += 1
        # Byte 176: pulse rate low 8 bits
        pr_lo = p[off]; off += 1
        pr_hi = (param_hi >> 3) & 0x01
        dp.vitals.pulse_rate = (pr_hi << 8) | pr_lo

        # Skip MAC address (5 bytes) + firmware (1 byte) + event (1 byte)
        off += 7

        # Byte 184: heart rate bit8 + resp rate
        if off < len(p):
            b184 = p[off]; off += 1
            hr_hi = (b184 >> 7) & 0x01
            dp.vitals.resp_rate = b184 & 0x7F
            dp.vitals.heart_rate = dp.vitals.pulse_rate  # approximate

        # Byte 185: gesture
        if off < len(p):
            dp.vitals.gesture = p[off]

    def _parse_sub3(self, dp: DataPacket, p: bytes):
        """Sub-packet 3: spirometer data (optional, skip for now)."""
        pass


def _unpack_10bit(data: bytes, offset: int, n_samples: int) -> np.ndarray:
    """Unpack 10-bit samples packed as high-2-bits (4 per byte) + low-8-bits."""
    n_hi_bytes = (n_samples + 3) // 4
    hi_start = offset
    lo_start = offset + n_hi_bytes

    result = np.zeros(n_samples, dtype=np.int16)
    for i in range(n_samples):
        hi_byte_idx = i // 4
        hi_bit_shift = (3 - (i % 4)) * 2

        if hi_start + hi_byte_idx < len(data) and lo_start + i < len(data):
            hi_val = (data[hi_start + hi_byte_idx] >> hi_bit_shift) & 0x03
            lo_val = data[lo_start + i]
            result[i] = (hi_val << 8) | lo_val

    return result


def apply_chest_resp_formula(raw: np.ndarray, coeff: int) -> np.ndarray:
    """Convert raw chest respiration to physical values.

    Formula from doc: value[i] = (coeff * 10000 + (65535 - raw[i])) * 1.25
    """
    return (coeff * 10000 + (65535 - raw.astype(np.float64))) * 1.25
