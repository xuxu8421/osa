"""
OSA Sound Designer — interactive GUI for designing, previewing and exporting
intervention sound stimuli, with integrated chest band BLE data reception.
"""

import asyncio
import json
import os
import random
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import numpy as np
import dearpygui.dearpygui as dpg
import sounddevice as sd
import soundfile as sf

from sounds.strategies import (
    STRATEGY_REGISTRY, synthesize, get_default_params,
)
from sounds.generator import DEFAULT_SR
from devices.chestband import ChestBandBLE
from devices.chestband_protocol import DataPacket
from devices.oximeter import OximeterBLE, OxiReading

from pipeline import (
    EventBus, ChestBandSensor, OximeterSensor, LocalAudioSink,
    SessionRecorder, SessionMeta, new_session_id,
    PostureAnalyzer, ClosedLoopController, ControllerConfig,
    MicSnoreDetector,
)
from pipeline.audio import PlaybackRequest

PRESETS_DIR = Path(__file__).resolve().parent.parent / 'presets'
OUTPUT_DIR = Path(__file__).resolve().parent.parent / 'output'
STRATEGY_ORDER = ['P1', 'P2', 'P3', 'L1', 'L2']

# ── Colors ──
C_BG       = (24, 24, 28)
C_PANEL    = (34, 34, 40)
C_BORDER   = (52, 52, 60)
C_TEXT     = (225, 225, 230)
C_DIM      = (130, 130, 145)
C_BLUE     = (82, 148, 255)
C_BLUE_H   = (105, 168, 255)
C_BLUE_A   = (62, 128, 235)
C_GREEN    = (72, 199, 115)
C_GREEN_H  = (92, 219, 135)
C_GREEN_A  = (52, 179, 95)
C_RED      = (235, 75, 75)
C_RED_H    = (255, 95, 95)
C_RED_A    = (215, 55, 55)
C_AMBER    = (245, 175, 45)
C_AMBER_H  = (255, 190, 65)
C_AMBER_A  = (225, 155, 25)
C_CARD     = (44, 44, 52)
C_CARD_H   = (54, 54, 64)
C_SLIDER   = (50, 50, 58)
C_GRAB     = C_BLUE

# ── Per-strategy UI labels ──
STRAT_BTN = {
    'P1': 'P1 Whisper Sweep',
    'P2': 'P2 Double Pulse',
    'P3': 'P3 Low-Freq Rumble',
    'L1': 'L1 Smooth Burst',
    'L2': 'L2 Rough Burst',
}


def _find_chinese_font() -> Optional[str]:
    for p in [
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/System/Library/Fonts/STHeiti Medium.ttc',
        'C:/Windows/Fonts/msyh.ttc',
        'C:/Windows/Fonts/simhei.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    ]:
        if os.path.exists(p):
            return p
    return None


class SoundDesigner:

    PLAY_REPEATS = 3
    PLAY_GAP = 0.5

    def __init__(self):
        self.sr = DEFAULT_SR
        self.cur_strat: str = 'P1'
        self.cur_dir: str = 'left'
        self.params: Dict[str, float] = get_default_params('P1')
        self.waveform: Optional[np.ndarray] = None
        self._noise_seed: int = 42
        self._playing = False
        self._play_t0 = 0.0
        self._play_dur = 0.0

        self._slider_tags: Dict[str, int] = {}
        self._param_groups: list = []
        self._presets: list = []

        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # BLE state  (all DPG calls happen ONLY in tick methods on main thread)
        self._ble: Optional[ChestBandBLE] = None
        self._ble_thread: Optional[threading.Thread] = None
        self._ble_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_loop_started = threading.Event()
        self._ble_connected = False
        self._ble_scanning = False
        self._ble_devices: list = []
        self._ble_latest: Optional[DataPacket] = None
        self._ble_pkt_count = 0
        self._ble_last_valid: Dict[str, float] = {
            'spo2': 0, 'pulse': 0, 'resp': 0,
            'batt': 0, 'temp': 0.0, 'gesture': 0,
        }
        # Chest respiration rolling buffer (25 Hz × 30 s = 750 samples)
        self._chest_buf: list[float] = []
        self._chest_buf_max = 25 * 30
        # Pending status messages (written from bg thread, consumed in tick)
        self._ble_msg_pending: Optional[str] = None
        self._ble_scan_done: bool = False
        self._ble_conn_state: str = 'idle'  # idle / connecting / connected / error
        self._ble_conn_err: str = ''

        # Oximeter (PC-68B family) — separate BLE link
        self._oxi: Optional[OximeterBLE] = None
        self._oxi_connected = False
        self._oxi_conn_state = 'idle'
        self._oxi_scanning = False
        self._oxi_scan_done = False
        self._oxi_devices: list = []
        self._oxi_latest: Optional[OxiReading] = None
        self._oxi_pkt_count = 0
        self._oxi_msg_pending: Optional[str] = None
        self._oxi_raw_frames: list = []
        self._oxi_raw_dirty = False
        self._oxi_log_lines: list = []
        self._oxi_log_dirty = False
        self._oxi_write_uuids: list = []
        self._oxi_target_populated = False
        self._oxi_last_valid: Dict[str, float] = {
            'spo2': 0, 'pulse': 0, 'pi': 0.0,
        }

        # ── Pipeline (event bus, analyzer, controller, recorder) ──
        self._bus = EventBus()
        self._audio_sink = LocalAudioSink()
        self._posture: Optional[PostureAnalyzer] = None
        self._controller: Optional[ClosedLoopController] = None
        self._recorder: Optional[SessionRecorder] = None
        self._session_start_t: float = 0.0
        self._cb_sensor: Optional[ChestBandSensor] = None
        self._oxi_sensor: Optional[OximeterSensor] = None

        # UI-visible state mirrored from bus (main-thread reads these)
        self._live_posture: str = '—'
        self._live_posture_conf: float = 0.0
        self._live_ctrl_state: str = 'idle'
        self._live_last_strategy: str = ''
        self._live_last_dir: str = ''
        self._live_events: list = []   # recent intervention log lines
        self._live_dirty = False

        self._bus.subscribe('posture.sample',        self._bus_posture_sample)
        self._bus.subscribe('posture.change',        self._bus_posture_change)
        self._bus.subscribe('intervention.state',    self._bus_ctrl_state)
        self._bus.subscribe('intervention.triggered', self._bus_triggered)
        self._bus.subscribe('intervention.response', self._bus_response)

        # Posture analyzer runs continuously, regardless of session state,
        # so the chest-band panel always shows a live fine-grained posture.
        self._posture = PostureAnalyzer(self._bus)

        # Snore detector also runs continuously so the user can see live
        # mic activity even outside a session. Started lazily after build()
        # so the first frame is rendered before sounddevice opens the input.
        self._snore = MicSnoreDetector(self._bus)
        self._snore_started = False

        self._start_ble_loop()

    # ── BLE background event loop ──

    def _start_ble_loop(self):
        """Start a single persistent asyncio loop in a bg thread."""
        def _runner():
            self._ble_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ble_loop)
            self._ble_loop_started.set()
            self._ble_loop.run_forever()

        self._ble_thread = threading.Thread(target=_runner, daemon=True)
        self._ble_thread.start()
        self._ble_loop_started.wait(timeout=2.0)

    def _ble_submit(self, coro):
        """Submit a coroutine to the bg loop."""
        if self._ble_loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._ble_loop)

    # ── BLE actions ──

    def _ble_scan(self):
        if self._ble_scanning or self._ble_connected:
            return
        self._ble_scanning = True
        self._ble_scan_done = False
        self._ble_msg_pending = "扫描 BLE 设备中..."
        dpg.configure_item(self._btn_ble_scan, label="  扫描中...  ")

        chestband_only = bool(dpg.get_value(self._chk_cb_only))
        named_only = bool(dpg.get_value(self._chk_named)) or chestband_only

        async def _do_scan():
            try:
                pairs = await ChestBandBLE.scan(
                    timeout=8.0,
                    named_only=named_only,
                    chestband_only=chestband_only,
                )
                self._ble_devices = pairs
                self._ble_msg_pending = (
                    f"扫描完成, 发现 {len(pairs)} 个设备  "
                    f"({'胸带' if chestband_only else ('有名字' if named_only else '全部')})"
                )
            except Exception as e:
                self._ble_devices = []
                self._ble_msg_pending = f"扫描出错: {e}"
            finally:
                self._ble_scanning = False
                self._ble_scan_done = True

        self._ble_submit(_do_scan())

    def _ble_tick_scan(self):
        """Consume scan results on main thread."""
        if self._ble_scan_done:
            self._ble_scan_done = False
            dpg.configure_item(self._btn_ble_scan, label="  扫描  ")
            names = []
            for dev, rssi in self._ble_devices:
                n = dev.name or '(unknown)'
                names.append(f"{rssi:>4} dBm   {n}   [{dev.address}]")
            dpg.configure_item(self._combo_ble, items=names)
            if names:
                dpg.set_value(self._combo_ble, names[0])

        if self._ble_msg_pending is not None:
            dpg.set_value(self._txt_ble_msg, self._ble_msg_pending)
            self._ble_msg_pending = None

    def _ble_connect(self):
        if self._ble_connected or self._ble_conn_state == 'connecting':
            return
        sel = dpg.get_value(self._combo_ble)
        if not sel:
            self._ble_msg_pending = "请先扫描并选择设备"
            return

        device = None
        for dev, rssi in self._ble_devices:
            n = dev.name or '(unknown)'
            if f"{rssi:>4} dBm   {n}   [{dev.address}]" == sel:
                device = dev
                break
        if not device:
            self._ble_msg_pending = "未找到选中的设备"
            return

        self._ble_conn_state = 'connecting'
        self._ble_msg_pending = f"连接到 {device.name}..."
        dpg.configure_item(self._btn_ble_conn, label="  连接中...  ")

        async def _do_connect():
            try:
                self._ble = ChestBandBLE()
                await self._ble.connect(device)
                self._ble_pkt_count = 0

                def _on_data(dp: DataPacket):
                    self._ble_latest = dp
                    self._ble_pkt_count += 1
                    # always publish to pipeline; recorder/analyzer only act
                    # while subscribed
                    try:
                        self._bus.emit('chestband.data', dp, src='chestband')
                    except Exception:
                        pass

                await self._ble.start_receiving(_on_data)
                self._ble_connected = True
                self._ble_conn_state = 'connected'
                self._ble_msg_pending = "连接成功, 等待数据..."
            except Exception as e:
                self._ble_connected = False
                self._ble_conn_state = 'error'
                self._ble_conn_err = str(e)
                self._ble_msg_pending = f"连接失败: {e}"

        self._ble_submit(_do_connect())

    def _ble_disconnect(self):
        if not self._ble_connected and self._ble_conn_state != 'connected':
            return
        self._ble_connected = False
        self._ble_conn_state = 'idle'

        async def _do_disc():
            try:
                if self._ble:
                    await self._ble.disconnect()
            except Exception as e:
                self._ble_msg_pending = f"断开时出错: {e}"

        self._ble_submit(_do_disc())
        self._ble_msg_pending = f"已断开  (共收到 {self._ble_pkt_count} 包)"
        dpg.configure_item(self._btn_ble_conn, label="  连接  ")
        dpg.configure_item(self._txt_ble_state, color=C_DIM)
        dpg.set_value(self._txt_ble_state, "未连接")

    def _ble_tick_data(self):
        """Update UI from BLE state  (main thread only)."""
        if self._ble_conn_state == 'connected' and \
                dpg.get_item_label(self._btn_ble_conn) != "  已连接  ":
            dpg.configure_item(self._btn_ble_conn, label="  已连接  ")
            dpg.configure_item(self._txt_ble_state, color=C_GREEN)
            dpg.set_value(self._txt_ble_state, "已连接")
        elif self._ble_conn_state == 'error' and \
                dpg.get_item_label(self._btn_ble_conn) == "  连接中...  ":
            dpg.configure_item(self._btn_ble_conn, label="  连接  ")

        dp = self._ble_latest
        if dp is None:
            return
        self._ble_latest = None

        v = dp.vitals
        # Latch last valid reading to avoid display flicker when a particular
        # sub-packet doesn't carry that field. Reject device sentinel values.
        lv = self._ble_last_valid
        if 70 <= (v.spo2_pct or 0) <= 100:
            lv['spo2'] = v.spo2_pct
        if 30 <= (v.pulse_rate or 0) <= 220:    # 256 is "invalid" sentinel
            lv['pulse'] = v.pulse_rate
        if 4 <= (v.resp_rate or 0) <= 60:
            lv['resp'] = v.resp_rate
        if v.battery_voltage_mv > 0:
            lv['batt'] = v.battery_voltage_mv
        if 28 <= (v.temperature or 0) <= 42:    # body temp sanity window
            lv['temp'] = v.temperature
        lv['gesture'] = v.gesture

        dpg.set_value(self._txt_spo2,   f"{lv['spo2']}%" if lv['spo2'] else "--")
        dpg.set_value(self._txt_pulse,  f"{lv['pulse']} bpm" if lv['pulse'] else "--")
        dpg.set_value(self._txt_resp,   f"{lv['resp']}" if lv['resp'] else "--")
        # Posture: prefer analyzer's fine-grained classification over the
        # device's coarse gesture byte.
        posture_zh = {
            'supine': '仰卧', 'prone': '俯卧',
            'left': '左侧卧', 'right': '右侧卧',
            'upright': '直立/坐', 'unknown': '—',
        }
        live = self._live_posture or 'unknown'
        conf = self._live_posture_conf
        suffix = f" ({conf:.2f})" if conf > 0 else ''
        dpg.set_value(self._txt_gesture,
                      posture_zh.get(live, live) + suffix)
        dpg.set_value(self._txt_batt,   f"{lv['batt']} mV" if lv['batt'] else "--")
        dpg.set_value(self._txt_temp,   f"{lv['temp']:.1f} C" if lv['temp'] else "--")
        dpg.set_value(self._txt_ble_pkt, f"#{self._ble_pkt_count}")

        if dp.chest_resp is not None and len(dp.chest_resp) > 0:
            # Append to rolling buffer
            self._chest_buf.extend([float(x) for x in dp.chest_resp])
            if len(self._chest_buf) > self._chest_buf_max:
                self._chest_buf = self._chest_buf[-self._chest_buf_max:]
            buf = np.asarray(self._chest_buf, dtype=np.float32)
            # Downsample for plotting (target ~200 points)
            step = max(1, len(buf) // 200)
            plot = buf[::step]
            t = np.arange(len(plot)) * step / 25.0  # seconds
            dpg.set_value(self._line_chest,
                          [t.tolist(), plot.tolist()])
            y_min, y_max = float(plot.min()), float(plot.max())
            # Avoid degenerate axis
            if y_max - y_min < 10:
                y_max = y_min + 10
            dpg.set_axis_limits(self._y_chest,
                                y_min - 20, y_max + 20)
            dpg.set_axis_limits(self._x_chest, 0, len(plot) * step / 25.0)
            # Estimate respiration rate from zero-crossings over the buffer
            amp = y_max - y_min
            rr_est = self._estimate_rr(buf)
            rr_str = f"{rr_est:.1f}/min" if rr_est > 0 else "计算中..."
            dpg.set_value(
                self._txt_chest,
                f"窗 30s  |  幅度 {amp:.0f}  |  估计呼吸率 {rr_str}  |  "
                f"本包 {dp.chest_resp.min()}→{dp.chest_resp.max()}"
            )
        if dp.accel_x is not None:
            dpg.set_value(self._txt_accel,
                          f"X[{dp.accel_x.min()},{dp.accel_x.max()}] "
                          f"Y[{dp.accel_y.min()},{dp.accel_y.max()}] "
                          f"Z[{dp.accel_z.min()},{dp.accel_z.max()}]")
        self._ble_msg_pending = f"接收中  #{self._ble_pkt_count}"

    # ── Oximeter actions ──

    def _oxi_scan(self):
        if self._oxi_scanning or self._oxi_connected:
            return
        self._oxi_scanning = True
        self._oxi_scan_done = False
        self._oxi_msg_pending = "扫描血氧仪中..."
        dpg.configure_item(self._btn_oxi_scan, label="  扫描中...  ")

        only_oxi = bool(dpg.get_value(self._chk_oxi_only))
        named = bool(dpg.get_value(self._chk_oxi_named)) or only_oxi

        async def _do_scan():
            try:
                pairs = await OximeterBLE.scan(
                    timeout=8.0,
                    named_only=named,
                    oximeter_only=only_oxi,
                )
                self._oxi_devices = pairs
                self._oxi_msg_pending = (
                    f"扫描完成, {len(pairs)} 个设备  "
                    f"({'血氧仪' if only_oxi else ('有名字' if named else '全部')})"
                )
            except Exception as e:
                self._oxi_devices = []
                self._oxi_msg_pending = f"扫描出错: {e}"
            finally:
                self._oxi_scanning = False
                self._oxi_scan_done = True

        self._ble_submit(_do_scan())

    def _oxi_tick_scan(self):
        if self._oxi_scan_done:
            self._oxi_scan_done = False
            dpg.configure_item(self._btn_oxi_scan, label="  扫描  ")
            names = []
            for dev, rssi in self._oxi_devices:
                n = dev.name or '(unknown)'
                names.append(f"{rssi:>4} dBm   {n}   [{dev.address}]")
            dpg.configure_item(self._combo_oxi, items=names)
            if names:
                dpg.set_value(self._combo_oxi, names[0])

        if self._oxi_msg_pending is not None:
            dpg.set_value(self._txt_oxi_msg, self._oxi_msg_pending)
            self._oxi_msg_pending = None

    def _oxi_connect(self):
        if self._oxi_connected or self._oxi_conn_state == 'connecting':
            return
        sel = dpg.get_value(self._combo_oxi)
        if not sel:
            self._oxi_msg_pending = "请先扫描并选择设备"
            return
        device = None
        for dev, rssi in self._oxi_devices:
            n = dev.name or '(unknown)'
            if f"{rssi:>4} dBm   {n}   [{dev.address}]" == sel:
                device = dev
                break
        if not device:
            self._oxi_msg_pending = "未找到选中的设备"
            return

        self._oxi_conn_state = 'connecting'
        self._oxi_msg_pending = f"连接到 {device.name}..."
        dpg.configure_item(self._btn_oxi_conn, label="  连接中...  ")

        async def _do_connect():
            try:
                self._oxi = OximeterBLE()
                self._oxi_pkt_count = 0
                self._oxi_raw_frames = []
                self._oxi_log_lines = []

                def _on_reading(r: OxiReading):
                    self._oxi_latest = r
                    try:
                        self._bus.emit('oximeter.reading', r, src='oximeter')
                    except Exception:
                        pass

                def _on_raw(b: bytes):
                    # Every BLE notification — record even if parser rejects it
                    self._oxi_pkt_count += 1
                    h = b.hex(' ')
                    self._oxi_raw_frames.append(h)
                    if len(self._oxi_raw_frames) > 80:
                        self._oxi_raw_frames = self._oxi_raw_frames[-80:]
                    self._oxi_raw_dirty = True

                def _on_log(msg: str):
                    for line in msg.splitlines():
                        self._oxi_log_lines.append(line)
                    if len(self._oxi_log_lines) > 80:
                        self._oxi_log_lines = self._oxi_log_lines[-80:]
                    self._oxi_log_dirty = True

                self._oxi.on_reading = _on_reading
                self._oxi.on_raw_frame = _on_raw
                self._oxi.on_log = _on_log

                await self._oxi.connect(device)
                await self._oxi.start_receiving()
                self._oxi_connected = True
                self._oxi_conn_state = 'connected'
                # Cache writable UUIDs for the target combo
                self._oxi_write_uuids = self._oxi.list_write_uuids()
                self._oxi_msg_pending = (
                    f"已连接. 订阅 {len(self._oxi.subscribed_uuids)} 个特征, "
                    "请确保血氧仪在测量画面 (插指)"
                )
            except Exception as e:
                self._oxi_connected = False
                self._oxi_conn_state = 'error'
                self._oxi_msg_pending = f"连接失败: {e}"

        self._ble_submit(_do_connect())

    def _oxi_manual_send(self):
        if not self._oxi_connected or self._oxi is None:
            self._oxi_msg_pending = "未连接, 不能发送"
            return
        s = dpg.get_value(self._inp_oxi_hex).strip()
        if not s:
            return
        try:
            bytes.fromhex(s.replace(' ', '').replace(',', ''))
        except Exception as e:
            self._oxi_msg_pending = f"HEX 格式错误: {e}"
            return
        target = dpg.get_value(self._combo_oxi_target)
        target_uuid = None if target == "(自动)" else target
        async def _run():
            await self._oxi.write_raw(s, target_uuid=target_uuid,
                                      with_response=False)
        self._ble_submit(_run())

    def _oxi_retry_wake(self):
        if not self._oxi_connected or self._oxi is None:
            self._oxi_msg_pending = "未连接"
            return
        async def _run():
            await self._oxi._send_start_command()
        self._ble_submit(_run())

    def _oxi_disconnect(self):
        if not self._oxi_connected and self._oxi_conn_state != 'connected':
            return
        self._oxi_connected = False
        self._oxi_conn_state = 'idle'

        async def _do_disc():
            try:
                if self._oxi:
                    await self._oxi.disconnect()
            except Exception as e:
                self._oxi_msg_pending = f"断开时出错: {e}"

        self._ble_submit(_do_disc())
        self._oxi_msg_pending = f"已断开  (共 {self._oxi_pkt_count} 帧)"
        dpg.configure_item(self._btn_oxi_conn, label="  连接  ")
        dpg.configure_item(self._txt_oxi_state, color=C_DIM)
        dpg.set_value(self._txt_oxi_state, "未连接")

    def _oxi_tick_data(self):
        """Update oximeter UI from state  (main thread only)."""
        if self._oxi_conn_state == 'connected' and \
                dpg.get_item_label(self._btn_oxi_conn) != "  已连接  ":
            dpg.configure_item(self._btn_oxi_conn, label="  已连接  ")
            dpg.configure_item(self._txt_oxi_state, color=C_GREEN)
            dpg.set_value(self._txt_oxi_state, "已连接")
            self._oxi_target_populated = False

        # Populate target char combo once after connect
        if self._oxi_conn_state == 'connected' and \
                not self._oxi_target_populated and self._oxi_write_uuids:
            items = ["(自动)"] + self._oxi_write_uuids
            dpg.configure_item(self._combo_oxi_target, items=items)
            self._oxi_target_populated = True
        if self._oxi_conn_state == 'error' and \
                dpg.get_item_label(self._btn_oxi_conn) == "  连接中...  ":
            dpg.configure_item(self._btn_oxi_conn, label="  连接  ")

        # Raw frames dump (for protocol reverse engineering)
        if self._oxi_raw_dirty:
            self._oxi_raw_dirty = False
            dpg.set_value(self._txt_oxi_raw,
                          '\n'.join(self._oxi_raw_frames))
            if self._oxi is not None:
                dpg.set_value(self._txt_oxi_bytes,
                              f"收到帧 {self._oxi.pkt_count}  |  "
                              f"字节 {self._oxi.byte_count}")

        # Log/GATT dump (full history, scrollable)
        if self._oxi_log_dirty:
            self._oxi_log_dirty = False
            dpg.set_value(self._txt_oxi_log,
                          '\n'.join(self._oxi_log_lines))

        r = self._oxi_latest
        if r is None:
            return
        self._oxi_latest = None

        lv = self._oxi_last_valid
        if r.spo2_pct is not None and r.spo2_pct > 0:
            lv['spo2'] = r.spo2_pct
        if r.pulse_rate is not None and r.pulse_rate > 0:
            lv['pulse'] = r.pulse_rate
        if r.pi is not None and r.pi > 0:
            lv['pi'] = r.pi

        dpg.set_value(self._txt_oxi_spo2,
                      f"{lv['spo2']}%" if lv['spo2'] else "--")
        dpg.set_value(self._txt_oxi_pr,
                      f"{lv['pulse']} bpm" if lv['pulse'] else "--")
        dpg.set_value(self._txt_oxi_pi,
                      f"{lv['pi']:.1f} %" if lv['pi'] else "--")
        flags = []
        if r.finger_out: flags.append('手指未插入')
        if r.probe_error: flags.append('探头异常')
        dpg.set_value(self._txt_oxi_flags,
                      ', '.join(flags) if flags else '正常')
        dpg.set_value(self._txt_oxi_pkt, f"#{self._oxi_pkt_count}")
        self._oxi_msg_pending = f"接收中  #{self._oxi_pkt_count}"

    @staticmethod
    def _estimate_rr(buf: np.ndarray, fs: float = 25.0) -> float:
        """Crude respiration rate from zero-crossings. Needs real signal."""
        if len(buf) < int(fs * 15):
            return 0.0
        x = buf - float(np.mean(buf))
        thr = max(20.0, 0.15 * (x.max() - x.min()))
        if thr < 40:
            return 0.0
        above = x > thr
        crossings = int(np.sum(np.diff(above.astype(int)) == 1))
        dur_s = len(x) / fs
        if dur_s < 1:
            return 0.0
        rr = crossings * 60.0 / dur_s
        if rr < 4 or rr > 60:
            return 0.0
        return rr

    # ── Pipeline: bus callbacks (may run on bg thread; only mutate ivars) ──

    def _bus_posture_sample(self, ev):
        s = ev.payload
        self._live_posture = s.cls
        self._live_posture_conf = s.confidence
        self._live_dirty = True

    def _bus_posture_change(self, ev):
        pc = ev.payload
        self._push_event(
            f"[{time.strftime('%H:%M:%S')}]  姿态 {pc.prev} → {pc.cur}  "
            f"(前次保持 {pc.prev_duration_s:.1f}s)"
        )

    def _bus_ctrl_state(self, ev):
        p = ev.payload or {}
        self._live_ctrl_state = p.get('state', self._live_ctrl_state)
        self._live_dirty = True

    def _bus_triggered(self, ev):
        p = ev.payload or {}
        strategy = p.get('strategy', '')
        direction = p.get('direction', '')
        self._live_last_strategy = strategy
        self._live_last_dir = direction
        self._push_event(
            f"[{time.strftime('%H:%M:%S')}]  >> 触发 {strategy}  "
            f"方向 {direction}  原因 {p.get('reason','')}"
        )
        # Dump ±5s of mic audio around this trigger for offline review.
        self._save_trigger_audio(ev.t, strategy, direction)

    def _bus_response(self, ev):
        p = ev.payload or {}
        ok = p.get('success')
        tag = 'OK' if ok else '--'
        self._push_event(
            f"[{time.strftime('%H:%M:%S')}]  {tag} 响应 "
            f"{p.get('reason','')}  潜伏 {p.get('latency_s', 0):.1f}s"
        )

    def _push_event(self, line: str):
        self._live_events.append(line)
        if len(self._live_events) > 40:
            self._live_events = self._live_events[-40:]
        self._live_dirty = True

    # ── Pipeline: session/controller actions ──

    def _session_start(self):
        if self._recorder is not None:
            return
        sid = new_session_id(dpg.get_value(self._inp_session_tag).strip())
        subject = dpg.get_value(self._inp_subject).strip()
        note = dpg.get_value(self._inp_sess_note).strip()
        meta = SessionMeta(
            session_id=sid,
            started_at=datetime.now().isoformat(timespec='seconds'),
            subject_id=subject,
            note=note,
            protocol='block_a_posture',
            config=asdict(self._controller_cfg()),
        )
        self._recorder = SessionRecorder(self._bus, meta)
        self._session_start_t = time.time()

        # Analyzer already runs globally; just apply the chosen debounce
        # for the session duration.
        self._posture.debounce_s = float(dpg.get_value(self._sl_debounce))
        self._controller = ClosedLoopController(
            self._bus, self._audio_sink, self._controller_cfg(),
            snoring_provider=self._snore.is_snoring)

        self._bus.emit('session.marker',
                       {'kind': 'start', 'session': sid}, src='ui')

        self._status(f"会话开始: {sid}")
        self._live_events = []
        self._live_dirty = True

    def _session_stop(self):
        if self._recorder is None:
            return
        self._bus.emit('session.marker', {'kind': 'stop'}, src='ui')
        try:
            # Keep posture analyzer alive across sessions
            if self._controller: self._controller.close()
            self._recorder.close()
        finally:
            self._controller = None
            rec = self._recorder
            self._recorder = None
            self._status(f"会话已结束 ({rec.packet_count} 胸带包 / "
                         f"{rec.intervention_count} 次干预)")
            try:
                self._refresh_history()
            except Exception:
                pass

    def _controller_cfg(self) -> ControllerConfig:
        return ControllerConfig(
            trigger_postures=('supine',),
            require_snoring=bool(dpg.get_value(self._chk_require_snoring)),
            trigger_hold_s=float(dpg.get_value(self._sl_hold)),
            strategy_pool=('P1', 'P2', 'P3'),
            direction_policy='opposite',
            level_db=float(dpg.get_value(self._sl_level)),
            response_window_s=float(dpg.get_value(self._sl_window)),
            cooldown_s=float(dpg.get_value(self._sl_cooldown)),
            enabled=bool(dpg.get_value(self._chk_ctrl_on)),
        )

    def _ctrl_apply_cfg(self):
        if self._controller is None:
            return
        self._controller.cfg = self._controller_cfg()

    def _refresh_history(self):
        root = Path('sessions')
        if not root.exists():
            dpg.set_value(self._txt_history, "(没有 sessions/ 目录)")
            return
        dirs = sorted([p for p in root.iterdir() if p.is_dir()],
                      key=lambda p: p.name, reverse=True)
        lines = []
        for d in dirs[:10]:
            meta = {}
            summ = {}
            try:
                mf = d / 'meta.json'
                if mf.exists():
                    meta = json.loads(mf.read_text(encoding='utf-8'))
                sf2 = d / 'summary.json'
                if sf2.exists():
                    summ = json.loads(sf2.read_text(encoding='utf-8'))
            except Exception:
                pass
            subject = meta.get('subject_id') or '-'
            note = meta.get('note') or ''
            pkts = summ.get('chestband_packets', '-')
            itv = summ.get('interventions', '-')
            dur = '-'
            try:
                t0 = datetime.fromisoformat(summ.get('started_at') or
                                            meta.get('started_at'))
                t1 = datetime.fromisoformat(summ.get('ended_at'))
                secs = int((t1 - t0).total_seconds())
                dur = (f"{secs // 60:02d}:{secs % 60:02d}" if secs < 3600
                       else f"{secs // 3600}:{(secs % 3600) // 60:02d}:"
                            f"{secs % 60:02d}")
            except Exception:
                if not summ:
                    dur = '进行中?'
            lines.append(
                f"{d.name}  |  时长 {dur}  |  干预 {itv}  |  "
                f"胸带 {pkts} 包  |  被试 {subject}"
                + (f"  |  {note}" if note else ''))
        if not lines:
            lines = ["(sessions/ 下还没有子目录)"]
        dpg.set_value(self._txt_history, '\n'.join(lines))

    def _open_sessions_dir(self):
        root = Path('sessions').resolve()
        root.mkdir(parents=True, exist_ok=True)
        try:
            import subprocess
            subprocess.Popen(['open', str(root)])
        except Exception as e:
            self._status(f"打开目录失败: {e}")

    def _apply_snore_cfg(self):
        try:
            self._snore.set_thresholds(
                energy_db=dpg.get_value(self._sl_snore_energy),
                band_ratio_min=dpg.get_value(self._sl_snore_band),
            )
        except Exception:
            pass

    def _save_trigger_audio(self, trigger_at: float, strategy: str,
                            direction: str, before_s: float = 5.0,
                            after_s: float = 5.0):
        """Snapshot ±Ns of mic audio around a trigger and dump to session dir.

        Called from the event-bus thread via `_bus_triggered`; actual write is
        dispatched to a daemon that waits `after_s` so the "after" half is in
        the ring buffer by the time we slice it.
        """
        rec = self._recorder
        if rec is None:
            return
        sess_dir = Path('sessions') / rec.meta.session_id

        def _do():
            time.sleep(after_s + 0.05)
            try:
                wav = self._snore.snapshot(before_s + after_s)
                if wav.size == 0:
                    return
                ts = datetime.fromtimestamp(trigger_at).strftime(
                    '%Y%m%d_%H%M%S')
                fn = f"snore_{ts}_{strategy}_{direction}.wav"
                sf.write(str(sess_dir / fn), wav, self._snore.sr)
            except Exception:
                pass

        threading.Thread(target=_do, daemon=True).start()

    def _ctrl_manual_trigger(self):
        # If a session is running, go through the controller so it gets
        # logged / cooldown-gated normally.
        if self._controller is not None:
            self._controller.manual_trigger()
            return
        # Otherwise, fire a one-shot local playback as a hardware sanity
        # check (headset/speaker test). Not recorded.
        try:
            strategy = random.choice(['P1', 'P2', 'P3'])
            direction = random.choice(['left', 'right'])
            params = get_default_params(strategy)
            params['level_db'] = float(dpg.get_value(self._sl_level))
            wave = synthesize(strategy, params, direction, self.sr,
                              seed=random.randint(0, 2 ** 31 - 1))
            self._audio_sink.play(PlaybackRequest(wave, self.sr, {
                'strategy': strategy, 'direction': direction,
                'manual': True,
            }))
            self._status(
                f"[手动触发 · 未开始会话] 播放 {strategy} → {direction}")
        except Exception as e:
            self._status(f"手动触发失败: {e}")

    def _live_tick(self):
        if not self._live_dirty:
            return
        self._live_dirty = False
        # Current posture & controller state
        dpg.set_value(self._txt_live_posture, self._live_posture)
        dpg.set_value(self._txt_live_ctrl, self._live_ctrl_state)
        dpg.set_value(self._txt_live_last,
                      f"{self._live_last_strategy} / {self._live_last_dir}"
                      if self._live_last_strategy else "—")
        dpg.set_value(self._txt_live_events,
                      '\n'.join(self._live_events[-20:]))
        if self._recorder is not None:
            dpg.set_value(self._txt_live_rec,
                          f"#{self._recorder.packet_count} pkts · "
                          f"{self._recorder.intervention_count} interv.")

    # ── synth ──

    def _new_seed(self):
        self._noise_seed = int(time.time() * 1000) % (2**31)

    def _synth(self):
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        d = self.cur_dir if sdef.has_direction else 'center'
        self.waveform = synthesize(self.cur_strat, self.params, d, self.sr,
                                   seed=self._noise_seed)
        self._update_plots()
        ms = len(self.waveform) / self.sr * 1000
        self._status(f"已生成  {ms:.0f} ms  /  {self.sr} Hz")

    def _play_wav(self):
        if self.waveform is None:
            self._synth()
        sd.stop()
        gap = np.zeros((int(self.PLAY_GAP * self.sr), 2))
        pad = np.zeros((int(0.12 * self.sr), 2))
        parts = [pad]
        for i in range(self.PLAY_REPEATS):
            parts.append(self.waveform)
            if i < self.PLAY_REPEATS - 1:
                parts.append(gap)
        parts.append(pad)
        full = np.concatenate(parts)
        self._play_dur = len(full) / self.sr
        sd.play(full, self.sr)
        self._playing = True
        self._play_t0 = time.time()
        dpg.configure_item(self._btn_play, label="  播放中...  ")
        dpg.show_item(self._progress)
        ms = len(self.waveform) / self.sr * 1000
        self._status(f"播放中  x{self.PLAY_REPEATS}  |  单次 {ms:.0f} ms")

    def _stop_wav(self):
        sd.stop()
        self._playing = False
        dpg.configure_item(self._btn_play, label="  播放  ")
        dpg.set_value(self._progress, 0)
        dpg.hide_item(self._progress)
        self._status("已停止")

    def _tick(self):
        if not self._playing:
            return
        t = time.time() - self._play_t0
        p = min(t / self._play_dur, 1.0)
        dpg.set_value(self._progress, p)
        if p >= 1.0:
            self._playing = False
            dpg.configure_item(self._btn_play, label="  播放  ")
            self._status("播放完成")

    # ── export ──

    def _export(self):
        if self.waveform is None:
            self._synth()
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        d = self.cur_dir if sdef.has_direction else 'center'
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f"{self.cur_strat}_{d}_{ts}.wav"
        sf.write(str(OUTPUT_DIR / name), self.waveform, self.sr)
        dpg.set_value(self._txt_status, f">>> 已导出  output/{name}")
        dpg.configure_item(self._txt_status, color=C_AMBER)
        self._export_flash_t = time.time()

    def _check_export_flash(self):
        if hasattr(self, '_export_flash_t') and self._export_flash_t > 0:
            if time.time() - self._export_flash_t > 3.0:
                dpg.configure_item(self._txt_status, color=C_GREEN)
                self._export_flash_t = 0

    def _batch(self):
        levels = [-30, -20, -10, -6]
        n = 0
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for sk in STRATEGY_ORDER:
            sd_ = STRATEGY_REGISTRY[sk]
            pr = get_default_params(sk)
            dirs = ['left', 'right'] if sd_.has_direction else ['center']
            for d in dirs:
                for lv in levels:
                    pr['level_db'] = lv
                    w = synthesize(sk, pr, d, self.sr,
                                   seed=self._noise_seed)
                    sf.write(str(OUTPUT_DIR / f"{sk}_{d}_{lv}dB_{ts}.wav"),
                             w, self.sr)
                    n += 1
        dpg.set_value(self._txt_status, f">>> 批量导出完成  共 {n} 个文件 -> output/")
        dpg.configure_item(self._txt_status, color=C_AMBER)
        self._export_flash_t = time.time()

    # ── presets ──

    def _save_preset(self):
        name = dpg.get_value(self._inp_name).strip()
        if not name:
            self._status("请输入预设名称")
            return
        note = dpg.get_value(self._inp_note).strip()
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        obj = {
            'name': name,
            'strategy': self.cur_strat,
            'direction': self.cur_dir if sdef.has_direction else 'center',
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'note': note,
            'params': dict(self.params),
            'seed': self._noise_seed,
        }
        safe = "".join(c if c.isalnum() or c in '-_' else '_' for c in name)
        with open(PRESETS_DIR / f"{safe}.json", 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        self._status(f"已保存预设: {name}")
        dpg.set_value(self._inp_name, '')
        dpg.set_value(self._inp_note, '')
        self._reload_presets()

    def _load_preset(self):
        sel = dpg.get_value(self._combo_preset)
        if not sel:
            return
        for p in self._presets:
            if self._pdisp(p) == sel:
                self.cur_strat = p['strategy']
                self.params = dict(p['params'])
                self._noise_seed = p.get('seed', 42)
                sdef = STRATEGY_REGISTRY[self.cur_strat]
                if sdef.has_direction:
                    self.cur_dir = p.get('direction', 'left')
                self._refresh_strat()
                self._refresh_params()
                self._synth()
                self._status(f"已加载: {p['name']}")
                return

    def _del_preset(self):
        sel = dpg.get_value(self._combo_preset)
        if not sel:
            return
        for p in self._presets:
            if self._pdisp(p) == sel:
                safe = "".join(
                    c if c.isalnum() or c in '-_' else '_' for c in p['name'])
                fp = PRESETS_DIR / f"{safe}.json"
                if fp.exists():
                    fp.unlink()
                self._status(f"已删除: {p['name']}")
                self._reload_presets()
                return

    def _reload_presets(self):
        self._presets = []
        for f in sorted(PRESETS_DIR.glob('*.json')):
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    self._presets.append(json.load(fh))
            except Exception:
                continue
        items = [self._pdisp(p) for p in self._presets]
        dpg.configure_item(self._combo_preset, items=items)
        dpg.set_value(self._combo_preset, items[0] if items else '')

    @staticmethod
    def _pdisp(p: dict) -> str:
        ts = p.get('created_at', '')[:16].replace('T', ' ')
        n = f" | {p['note']}" if p.get('note') else ''
        return f"[{p['strategy']}] {p['name']}  ({ts}){n}"

    # ── plots ──

    def _update_plots(self):
        if self.waveform is None:
            return
        n = len(self.waveform)
        step = max(1, n // 2000)
        t = np.arange(0, n, step) / self.sr * 1000
        L = self.waveform[::step, 0]
        R = self.waveform[::step, 1]
        m = min(len(t), len(L), len(R))
        t, L, R = t[:m], L[:m], R[:m]
        dpg.set_value(self._line_l, [t.tolist(), L.tolist()])
        dpg.set_value(self._line_r, [t.tolist(), R.tolist()])

        peak = max(np.max(np.abs(L)), np.max(np.abs(R)), 0.01)
        margin = peak * 1.15
        t_max = t[-1] if len(t) > 0 else 1.0

        dpg.set_axis_limits(self._x_ax, 0, t_max)
        dpg.set_axis_limits(self._y_ax, -margin, margin)

    # ── ui helpers ──

    def _status(self, msg: str):
        dpg.set_value(self._txt_status, msg)
        dpg.configure_item(self._txt_status, color=C_GREEN)
        if hasattr(self, '_export_flash_t'):
            self._export_flash_t = 0

    def _on_strat(self, _s, _v, key):
        if key == self.cur_strat:
            return
        self.cur_strat = key
        self.params = get_default_params(key)
        self._new_seed()
        sdef = STRATEGY_REGISTRY[key]
        if not sdef.has_direction:
            self.cur_dir = 'center'
        elif self.cur_dir == 'center':
            self.cur_dir = 'left'
        self._refresh_strat()
        self._refresh_params()
        self._synth()

    def _refresh_strat(self):
        for k, b in self._sbtns.items():
            dpg.bind_item_theme(b, self._th_sel if k == self.cur_strat
                                else self._th_card)
        sdef = STRATEGY_REGISTRY[self.cur_strat]
        dpg.set_value(self._txt_desc, sdef.description)
        if sdef.has_direction:
            dpg.show_item(self._grp_dir)
            self._refresh_dir()
        else:
            dpg.hide_item(self._grp_dir)

    def _on_dir(self, _s, _v, d):
        self.cur_dir = d
        self._refresh_dir()
        self._synth()

    def _refresh_dir(self):
        for d, b in self._dbtns.items():
            dpg.bind_item_theme(b, self._th_amber if d == self.cur_dir
                                else self._th_card)

    def _on_param(self, _s, val, key):
        self.params[key] = val
        self._synth()

    def _refresh_params(self):
        for t in self._param_groups:
            if dpg.does_item_exist(t):
                dpg.delete_item(t)
        self._param_groups.clear()
        self._slider_tags.clear()

        sdef = STRATEGY_REGISTRY[self.cur_strat]
        groups: Dict[str, list] = {}
        for ps in sdef.params:
            groups.setdefault(ps.group, []).append(ps)

        glabel = {'general': '基本参数', 'spatial': '空间化参数',
                  'roughness': '粗糙度调制'}

        for gk in ['general', 'spatial', 'roughness']:
            specs = groups.get(gk)
            if not specs:
                continue
            g = dpg.add_group(parent=self._params_box)
            self._param_groups.append(g)
            dpg.add_spacer(height=3, parent=g)
            dpg.add_text(glabel.get(gk, gk), color=C_BLUE, parent=g)

            for ps in specs:
                v = self.params.get(ps.key, ps.default)
                u = f" {ps.unit}" if ps.unit else ''
                fmt = f"%.0f{u}" if ps.step >= 1 else (
                    f"%.1f{u}" if ps.step >= 0.1 else f"%.2f{u}")
                tag = dpg.add_slider_float(
                    label=f"  {ps.label}",
                    default_value=v, min_value=ps.min_val,
                    max_value=ps.max_val, format=fmt,
                    callback=self._on_param, user_data=ps.key,
                    width=380, parent=g)
                self._slider_tags[ps.key] = tag

    # ── themes ──

    def _themes(self):
        with dpg.theme() as self._th_global:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(dpg.mvThemeCol_WindowBg, C_BG)
                dpg.add_theme_color(dpg.mvThemeCol_ChildBg, C_PANEL)
                dpg.add_theme_color(dpg.mvThemeCol_Text, C_TEXT)
                dpg.add_theme_color(dpg.mvThemeCol_Border, C_BORDER)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, C_SLIDER)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgHovered, (60, 60, 68))
                dpg.add_theme_color(dpg.mvThemeCol_FrameBgActive, (70, 70, 78))
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrab, C_GRAB)
                dpg.add_theme_color(dpg.mvThemeCol_SliderGrabActive, C_BLUE_A)
                dpg.add_theme_color(dpg.mvThemeCol_ScrollbarBg, (28, 28, 32))
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 10)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 18, 14)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 8, 5)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 5)

        def _btn(bg, bg_h, bg_a, fg=C_TEXT, pad=(14, 9), rnd=7):
            th = dpg.add_theme()
            with dpg.theme_component(dpg.mvButton, parent=th):
                dpg.add_theme_color(dpg.mvThemeCol_Button, bg)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, bg_h)
                dpg.add_theme_color(dpg.mvThemeCol_ButtonActive, bg_a)
                dpg.add_theme_color(dpg.mvThemeCol_Text, fg)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, rnd)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, *pad)
            return th

        self._th_card  = _btn(C_CARD, C_CARD_H, (64, 64, 74), C_DIM)
        self._th_sel   = _btn(C_BLUE, C_BLUE_H, C_BLUE_A, (255,255,255))
        self._th_amber = _btn(C_AMBER, C_AMBER_H, C_AMBER_A, (25,25,25))
        self._th_play  = _btn(C_GREEN, C_GREEN_H, C_GREEN_A, (255,255,255),
                              (30, 12), 8)
        self._th_stop  = _btn(C_RED, C_RED_H, C_RED_A, (255,255,255),
                              (30, 12), 8)
        self._th_sec   = _btn((48, 48, 56), (58, 58, 68), (68, 68, 78),
                              C_TEXT, (14, 9), 6)

        with dpg.theme() as self._th_prog:
            with dpg.theme_component(dpg.mvProgressBar):
                dpg.add_theme_color(dpg.mvThemeCol_PlotHistogram, C_GREEN)
                dpg.add_theme_color(dpg.mvThemeCol_FrameBg, C_SLIDER)
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)

        with dpg.theme() as self._th_wl:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, C_BLUE)
        with dpg.theme() as self._th_wr:
            with dpg.theme_component(dpg.mvLineSeries):
                dpg.add_theme_color(dpg.mvPlotCol_Line, C_AMBER)

    # ── header / status bar / tab builders ──

    def _build_header(self, f_title):
        with dpg.group(horizontal=True):
            tt = dpg.add_text("OSA Sound Designer", color=C_BLUE)
            if f_title:
                dpg.bind_item_font(tt, f_title)
            dpg.add_spacer(width=18)
            dpg.add_text("·  打鼾干预实验工作台", color=C_DIM)
        dpg.add_spacer(height=4)

    def _build_status_bar(self):
        """Always-visible row: device badges + closed-loop state + session ctl."""
        dpg.add_separator()
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            self._txt_badge_chest   = dpg.add_text("胸带  未连接", color=C_DIM)
            dpg.add_spacer(width=18)
            self._txt_badge_oxi     = dpg.add_text("血氧  未连接", color=C_DIM)
            dpg.add_spacer(width=18)
            self._txt_badge_mic     = dpg.add_text("麦克风  待启动", color=C_DIM)
            dpg.add_spacer(width=18)
            self._txt_badge_headset = dpg.add_text(
                "输出  系统默认 (AirPods 请在系统设置中选为默认)", color=C_DIM)
            dpg.add_spacer(width=22)
            self._txt_badge_ctrl    = dpg.add_text("闭环: idle", color=C_DIM)
            dpg.add_spacer(width=22)
            self._txt_badge_session = dpg.add_text("会话: 未开始", color=C_DIM)
            dpg.add_spacer(width=22)
            b_start = dpg.add_button(
                label="  开始会话  ",
                callback=lambda: self._session_start())
            dpg.bind_item_theme(b_start, self._th_play)
            b_stop = dpg.add_button(
                label="  结束会话  ",
                callback=lambda: self._session_stop())
            dpg.bind_item_theme(b_stop, self._th_stop)
            b_trig = dpg.add_button(
                label="  手动触发  ",
                callback=lambda: self._ctrl_manual_trigger())
            dpg.bind_item_theme(b_trig, self._th_amber)

    # ── tab: ① 声音设计 ──

    def _build_design_tab(self):
        dpg.add_spacer(height=6)
        dpg.add_text("选择声音策略", color=C_DIM)
        dpg.add_spacer(height=3)
        row = dpg.add_group(horizontal=True)
        self._sbtns: Dict[str, int] = {}
        for k in STRATEGY_ORDER:
            b = dpg.add_button(
                label=f" {STRAT_BTN[k]} ",
                callback=self._on_strat, user_data=k, parent=row)
            self._sbtns[k] = b
        dpg.add_spacer(height=4)
        self._txt_desc = dpg.add_text(
            STRATEGY_REGISTRY['P1'].description, color=C_DIM)

        dpg.add_spacer(height=10)
        self._grp_dir = dpg.add_group()
        dpg.add_text("播放方向", color=C_DIM, parent=self._grp_dir)
        dpg.add_spacer(height=3, parent=self._grp_dir)
        dr = dpg.add_group(horizontal=True, parent=self._grp_dir)
        self._dbtns: Dict[str, int] = {}
        for d, lb in [('left', '  左声道  '), ('right', '  右声道  ')]:
            b = dpg.add_button(label=lb, callback=self._on_dir,
                               user_data=d, parent=dr)
            self._dbtns[d] = b

        dpg.add_spacer(height=10)
        dpg.add_text("参数调节", color=C_DIM)
        self._params_box = dpg.add_group()

        dpg.add_spacer(height=10)
        dpg.add_separator()
        dpg.add_spacer(height=6)

        dpg.add_text("播放与导出", color=C_DIM)
        dpg.add_spacer(height=3)
        cr = dpg.add_group(horizontal=True)
        self._btn_play = dpg.add_button(
            label="  播放  ", callback=lambda: self._play_wav(),
            parent=cr)
        dpg.bind_item_theme(self._btn_play, self._th_play)
        b_stop = dpg.add_button(
            label="  停止  ", callback=lambda: self._stop_wav(),
            parent=cr)
        dpg.bind_item_theme(b_stop, self._th_stop)
        dpg.add_spacer(width=14, parent=cr)
        b_exp = dpg.add_button(
            label="  导出 WAV  ", callback=lambda: self._export(),
            parent=cr)
        dpg.bind_item_theme(b_exp, self._th_sec)
        b_bat = dpg.add_button(
            label="  批量导出  ", callback=lambda: self._batch(),
            parent=cr)
        dpg.bind_item_theme(b_bat, self._th_sec)
        dpg.add_spacer(width=14, parent=cr)
        b_reseed = dpg.add_button(
            label="  换一组噪声  ",
            callback=lambda: (self._new_seed(), self._synth()),
            parent=cr)
        dpg.bind_item_theme(b_reseed, self._th_sec)

        dpg.add_spacer(height=4)
        self._progress = dpg.add_progress_bar(
            default_value=0, width=-1, overlay="")
        dpg.bind_item_theme(self._progress, self._th_prog)
        dpg.hide_item(self._progress)

        dpg.add_spacer(height=10)
        dpg.add_text("波形预览  (蓝=L 左声道 · 橙=R 右声道)", color=C_DIM)
        dpg.add_spacer(height=2)
        with dpg.plot(height=200, width=-1, no_title=True,
                      no_mouse_pos=True):
            self._x_ax = dpg.add_plot_axis(dpg.mvXAxis, label="ms")
            self._y_ax = dpg.add_plot_axis(dpg.mvYAxis, label="")
            self._line_l = dpg.add_line_series([], [], label="L",
                                               parent=self._y_ax)
            dpg.bind_item_theme(self._line_l, self._th_wl)
            self._line_r = dpg.add_line_series([], [], label="R",
                                               parent=self._y_ax)
            dpg.bind_item_theme(self._line_r, self._th_wr)

        dpg.add_spacer(height=10)
        dpg.add_separator()
        dpg.add_spacer(height=6)
        dpg.add_text("预设管理", color=C_DIM)
        dpg.add_spacer(height=3)
        r1 = dpg.add_group(horizontal=True)
        self._inp_name = dpg.add_input_text(
            hint="预设名称", width=160, parent=r1)
        self._inp_note = dpg.add_input_text(
            hint="备注 (选填)", width=220, parent=r1)
        bs = dpg.add_button(
            label="  保存  ", callback=lambda: self._save_preset(),
            parent=r1)
        dpg.bind_item_theme(bs, self._th_sec)
        dpg.add_spacer(height=3)
        r2 = dpg.add_group(horizontal=True)
        self._combo_preset = dpg.add_combo(
            items=[], width=420, parent=r2)
        bl = dpg.add_button(
            label="  加载  ", callback=lambda: self._load_preset(),
            parent=r2)
        dpg.bind_item_theme(bl, self._th_sec)
        bd = dpg.add_button(
            label="  删除  ", callback=lambda: self._del_preset(),
            parent=r2)
        dpg.bind_item_theme(bd, self._th_sec)

    # ── tab: ② 设备连接 ──

    def _build_devices_tab(self):
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            with dpg.child_window(width=540, height=460, border=True):
                dpg.add_text("胸带  HSR 1A2.0", color=C_BLUE)
                dpg.add_spacer(height=6)
                ble_r1 = dpg.add_group(horizontal=True)
                self._btn_ble_scan = dpg.add_button(
                    label="  扫描  ", callback=lambda: self._ble_scan(),
                    parent=ble_r1)
                dpg.bind_item_theme(self._btn_ble_scan, self._th_sec)
                self._combo_ble = dpg.add_combo(
                    items=[], width=380, parent=ble_r1)
                dpg.add_spacer(height=2)
                fl_r = dpg.add_group(horizontal=True)
                self._chk_named = dpg.add_checkbox(
                    label="只显示有名字", default_value=True, parent=fl_r)
                dpg.add_spacer(width=10, parent=fl_r)
                self._chk_cb_only = dpg.add_checkbox(
                    label="仅胸带 (HSR/1A2/SRG…)",
                    default_value=False, parent=fl_r)
                dpg.add_spacer(height=4)
                ble_r2 = dpg.add_group(horizontal=True)
                self._btn_ble_conn = dpg.add_button(
                    label="  连接  ", callback=lambda: self._ble_connect(),
                    parent=ble_r2)
                dpg.bind_item_theme(self._btn_ble_conn, self._th_play)
                b_disc = dpg.add_button(
                    label="  断开  ",
                    callback=lambda: self._ble_disconnect(), parent=ble_r2)
                dpg.bind_item_theme(b_disc, self._th_stop)
                dpg.add_spacer(width=10, parent=ble_r2)
                self._txt_ble_state = dpg.add_text(
                    "未连接", color=C_DIM, parent=ble_r2)
                dpg.add_spacer(width=10, parent=ble_r2)
                self._txt_ble_pkt = dpg.add_text(
                    "", color=C_DIM, parent=ble_r2)
                dpg.add_spacer(height=4)
                self._txt_ble_msg = dpg.add_text("就绪", color=C_DIM)
                dpg.add_spacer(height=8)
                with dpg.table(header_row=True, borders_innerH=True,
                               borders_outerH=True, borders_innerV=True,
                               borders_outerV=True):
                    dpg.add_table_column(label="SpO2")
                    dpg.add_table_column(label="脉率")
                    dpg.add_table_column(label="呼吸率")
                    dpg.add_table_column(label="姿态")
                    dpg.add_table_column(label="体温")
                    dpg.add_table_column(label="电池")
                    with dpg.table_row():
                        self._txt_spo2 = dpg.add_text("--")
                        self._txt_pulse = dpg.add_text("--")
                        self._txt_resp = dpg.add_text("--")
                        self._txt_gesture = dpg.add_text("--")
                        self._txt_temp = dpg.add_text("--")
                        self._txt_batt = dpg.add_text("--")
                dpg.add_spacer(height=8)
                dpg.add_text("加速度", color=C_DIM)
                self._txt_accel = dpg.add_text("--")

            dpg.add_spacer(width=8)
            with dpg.child_window(width=540, height=460, border=True):
                dpg.add_text("血氧仪  PC-68B (可选)", color=C_BLUE)
                dpg.add_spacer(height=6)
                oxi_r1 = dpg.add_group(horizontal=True)
                self._btn_oxi_scan = dpg.add_button(
                    label="  扫描  ", callback=lambda: self._oxi_scan(),
                    parent=oxi_r1)
                dpg.bind_item_theme(self._btn_oxi_scan, self._th_sec)
                self._combo_oxi = dpg.add_combo(
                    items=[], width=380, parent=oxi_r1)
                dpg.add_spacer(height=2)
                of_r = dpg.add_group(horizontal=True)
                self._chk_oxi_named = dpg.add_checkbox(
                    label="只显示有名字", default_value=True, parent=of_r)
                dpg.add_spacer(width=10, parent=of_r)
                self._chk_oxi_only = dpg.add_checkbox(
                    label="仅血氧仪 (PC-68/CMS50…)",
                    default_value=True, parent=of_r)
                dpg.add_spacer(height=4)
                oxi_r2 = dpg.add_group(horizontal=True)
                self._btn_oxi_conn = dpg.add_button(
                    label="  连接  ", callback=lambda: self._oxi_connect(),
                    parent=oxi_r2)
                dpg.bind_item_theme(self._btn_oxi_conn, self._th_play)
                b_oxi_disc = dpg.add_button(
                    label="  断开  ",
                    callback=lambda: self._oxi_disconnect(),
                    parent=oxi_r2)
                dpg.bind_item_theme(b_oxi_disc, self._th_stop)
                dpg.add_spacer(width=10, parent=oxi_r2)
                self._txt_oxi_state = dpg.add_text(
                    "未连接", color=C_DIM, parent=oxi_r2)
                dpg.add_spacer(width=10, parent=oxi_r2)
                self._txt_oxi_pkt = dpg.add_text(
                    "", color=C_DIM, parent=oxi_r2)
                dpg.add_spacer(height=4)
                self._txt_oxi_msg = dpg.add_text(
                    "提示: 血氧仪菜单 → Wireless ON, 然后插指开始测量",
                    color=C_DIM)
                dpg.add_spacer(height=8)
                with dpg.table(header_row=True, borders_innerH=True,
                               borders_outerH=True, borders_innerV=True,
                               borders_outerV=True):
                    dpg.add_table_column(label="SpO2")
                    dpg.add_table_column(label="脉率")
                    dpg.add_table_column(label="灌注 PI")
                    dpg.add_table_column(label="状态")
                    with dpg.table_row():
                        self._txt_oxi_spo2 = dpg.add_text("--")
                        self._txt_oxi_pr = dpg.add_text("--")
                        self._txt_oxi_pi = dpg.add_text("--")
                        self._txt_oxi_flags = dpg.add_text("--")
                dpg.add_spacer(height=8)
                dpg.add_text(
                    "BLE 实时 SpO2 受厂商私有协议限制 — 详情见调试 tab",
                    color=C_DIM)

        dpg.add_spacer(height=10)
        with dpg.child_window(width=-1, height=140, border=True):
            dpg.add_text("AirPods  ·  音频输入 + 音频输出", color=C_BLUE)
            dpg.add_spacer(height=4)
            dpg.add_text(
                "本次实验用 AirPods 同时做两件事:  "
                "输出干预音 · 采集打鼾。",
                color=C_DIM)
            dpg.add_text(
                "请在  系统设置 → 声音  里把「输入」与「输出」都选成 AirPods; "
                "首次启动会弹出麦克风权限提示, 允许即可。",
                color=C_DIM)
            dpg.add_spacer(height=6)
            with dpg.group(horizontal=True):
                b_test_l = dpg.add_button(
                    label="  测试音 (左)  ",
                    callback=lambda: self._device_play_test('left'))
                dpg.bind_item_theme(b_test_l, self._th_sec)
                b_test_r = dpg.add_button(
                    label="  测试音 (右)  ",
                    callback=lambda: self._device_play_test('right'))
                dpg.bind_item_theme(b_test_r, self._th_sec)
                dpg.add_spacer(width=10)
                b_test_stop = dpg.add_button(
                    label="  停止  ",
                    callback=lambda: self._stop_wav())
                dpg.bind_item_theme(b_test_stop, self._th_stop)

    # ── tab: ③ 实时实验 ──

    def _build_experiment_tab(self):
        dpg.add_spacer(height=6)
        dpg.add_text("会话信息", color=C_DIM)
        dpg.add_spacer(height=3)
        sr = dpg.add_group(horizontal=True)
        dpg.add_text("会话标签:", color=C_DIM, parent=sr)
        self._inp_session_tag = dpg.add_input_text(
            hint="例如 pilot1", width=140, parent=sr)
        dpg.add_text("  被试:", color=C_DIM, parent=sr)
        self._inp_subject = dpg.add_input_text(
            hint="ID/姓名", width=130, parent=sr)
        dpg.add_text("  备注:", color=C_DIM, parent=sr)
        self._inp_sess_note = dpg.add_input_text(
            hint="任意", width=240, parent=sr)
        dpg.add_text(
            "  (开始/结束/手动触发请用顶部状态栏按钮)",
            color=C_DIM, parent=sr)

        dpg.add_spacer(height=10)
        with dpg.child_window(width=-1, height=140, border=True):
            dpg.add_text(
                "当前触发条件  ·  仰卧  +  检测到打鼾",
                color=C_BLUE)
            dpg.add_spacer(height=8)
            with dpg.group(horizontal=True):
                self._txt_trig_posture = dpg.add_text(
                    "姿态:  —", color=C_DIM)
                dpg.add_spacer(width=24)
                self._txt_trig_snoring = dpg.add_text(
                    "鼾声通道:  待麦克风启动",
                    color=C_DIM)
            dpg.add_spacer(height=8)
            self._prog_trig = dpg.add_progress_bar(
                default_value=0.0, width=-1, overlay="0.0 / 8.0 s")
            dpg.bind_item_theme(self._prog_trig, self._th_prog)
            dpg.add_spacer(height=6)
            self._txt_session_hint = dpg.add_text(
                "未开始会话 · 自动闭环不会触发 (请点顶部「开始会话」)",
                color=C_AMBER)

        dpg.add_spacer(height=8)
        with dpg.collapsing_header(
                label="鼾声检测阈值  (启发式 · 能量 + 低频带能比)",
                default_open=False):
            dpg.add_spacer(height=4)
            self._sl_snore_energy = dpg.add_slider_float(
                label="  能量阈值 (dB)",
                default_value=self._snore.energy_db,
                min_value=-70.0, max_value=-20.0, format="%.0f dB",
                width=380, callback=lambda: self._apply_snore_cfg())
            self._sl_snore_band = dpg.add_slider_float(
                label="  低频带能比下限 (80–500 Hz)",
                default_value=self._snore.band_ratio_min,
                min_value=0.20, max_value=0.90, format="%.2f",
                width=380, callback=lambda: self._apply_snore_cfg())
            dpg.add_text(
                "两项同时满足才算打鼾 · 默认只是启发式, "
                "后续会替换为训练模型",
                color=C_DIM)

        dpg.add_spacer(height=8)
        with dpg.collapsing_header(
                label="阈值参数  (默认 8s 触发  /  15s 观察  /  45s 冷却)",
                default_open=False):
            dpg.add_spacer(height=4)
            self._chk_ctrl_on = dpg.add_checkbox(
                label="启用自动闭环", default_value=True,
                callback=lambda: self._ctrl_apply_cfg())
            self._chk_require_snoring = dpg.add_checkbox(
                label="需要同时检测到打鼾 (取消即退化为只看姿态)",
                default_value=True,
                callback=lambda: self._ctrl_apply_cfg())
            self._sl_hold = dpg.add_slider_float(
                label="  仰卧持续 → 触发 (秒)",
                default_value=8.0, min_value=2.0, max_value=60.0,
                format="%.1f s", width=380,
                callback=lambda: self._ctrl_apply_cfg())
            self._sl_debounce = dpg.add_slider_float(
                label="  姿态 debounce (秒)",
                default_value=3.0, min_value=0.5, max_value=10.0,
                format="%.1f s", width=380)
            self._sl_window = dpg.add_slider_float(
                label="  响应观察窗 (秒)",
                default_value=15.0, min_value=3.0, max_value=60.0,
                format="%.1f s", width=380,
                callback=lambda: self._ctrl_apply_cfg())
            self._sl_cooldown = dpg.add_slider_float(
                label="  冷却时长 (秒)",
                default_value=45.0, min_value=5.0, max_value=300.0,
                format="%.1f s", width=380,
                callback=lambda: self._ctrl_apply_cfg())
            self._sl_level = dpg.add_slider_float(
                label="  播放响度",
                default_value=-15.0, min_value=-40.0, max_value=-3.0,
                format="%.0f dB", width=380,
                callback=lambda: self._ctrl_apply_cfg())

        dpg.add_spacer(height=10)
        dpg.add_text("胸呼吸  (近 30 秒)", color=C_DIM)
        self._txt_chest = dpg.add_text("--", color=C_DIM)
        with dpg.plot(height=160, width=-1, no_title=True,
                      no_mouse_pos=True):
            self._x_chest = dpg.add_plot_axis(
                dpg.mvXAxis, label="s", no_tick_labels=True)
            self._y_chest = dpg.add_plot_axis(dpg.mvYAxis, label="")
            self._line_chest = dpg.add_line_series(
                [], [], label="chest", parent=self._y_chest)
            dpg.bind_item_theme(self._line_chest, self._th_wl)

        dpg.add_spacer(height=10)
        with dpg.table(header_row=True, borders_innerH=True,
                       borders_outerH=True, borders_innerV=True,
                       borders_outerV=True):
            dpg.add_table_column(label="当前姿态")
            dpg.add_table_column(label="控制器状态")
            dpg.add_table_column(label="上次策略 / 方向")
            dpg.add_table_column(label="记录统计")
            with dpg.table_row():
                self._txt_live_posture = dpg.add_text("—")
                self._txt_live_ctrl = dpg.add_text("idle")
                self._txt_live_last = dpg.add_text("—")
                self._txt_live_rec = dpg.add_text("—")

        dpg.add_spacer(height=10)
        dpg.add_text("事件时间线", color=C_DIM)
        self._txt_live_events = dpg.add_input_text(
            multiline=True, readonly=True,
            default_value="(会话开始后显示)",
            height=180, width=-1)

        dpg.add_spacer(height=10)
        with dpg.collapsing_header(
                label="会话历史  (最近 10 次, sessions/ 目录)",
                default_open=False) as self._hdr_history:
            dpg.add_spacer(height=4)
            with dpg.group(horizontal=True):
                b_ref = dpg.add_button(
                    label="  刷新  ",
                    callback=lambda: self._refresh_history())
                dpg.bind_item_theme(b_ref, self._th_sec)
                b_open = dpg.add_button(
                    label="  打开 sessions/  ",
                    callback=lambda: self._open_sessions_dir())
                dpg.bind_item_theme(b_open, self._th_sec)
            dpg.add_spacer(height=4)
            self._txt_history = dpg.add_input_text(
                multiline=True, readonly=True,
                default_value="(点「刷新」读取)",
                height=170, width=-1)

    # ── tab: ④ 调试 ──

    def _build_debug_tab(self):
        dpg.add_spacer(height=6)
        dpg.add_text("血氧仪 · 手动 HEX 命令", color=C_BLUE)
        dpg.add_text(
            "用于 PC-68B 私有协议的盲试/抓包辅助, 不影响正常实验。",
            color=C_DIM)
        dpg.add_spacer(height=6)
        dpg.add_spacer(height=4)
        send_row = dpg.add_group(horizontal=True)
        dpg.add_text("目标特征:", color=C_DIM, parent=send_row)
        self._combo_oxi_target = dpg.add_combo(
            items=["(自动)"], default_value="(自动)",
            width=240, parent=send_row)
        self._inp_oxi_hex = dpg.add_input_text(
            hint="如: 7d 81 a1 80 80 80 80 80 80 a7", width=320,
            parent=send_row)
        b_oxi_send = dpg.add_button(
            label="  发送  ", parent=send_row,
            callback=lambda: self._oxi_manual_send())
        dpg.bind_item_theme(b_oxi_send, self._th_sec)
        b_oxi_retry = dpg.add_button(
            label="  重试全部预置  ", parent=send_row,
            callback=lambda: self._oxi_retry_wake())
        dpg.bind_item_theme(b_oxi_retry, self._th_sec)

        dpg.add_spacer(height=8)
        self._txt_oxi_bytes = dpg.add_text(
            "收到帧 0  |  字节 0", color=C_DIM)
        dpg.add_text("原始帧 (滚动显示):", color=C_DIM)
        self._txt_oxi_raw = dpg.add_input_text(
            multiline=True, readonly=True,
            default_value="(连接后显示, 若持续无更新说明无 notify 数据)",
            height=160, width=-1)

        dpg.add_spacer(height=8)
        dpg.add_text("GATT / 订阅日志:", color=C_DIM)
        self._txt_oxi_log = dpg.add_input_text(
            multiline=True, readonly=True,
            default_value="(连接后显示)",
            height=180, width=-1)

    # ── live UI ticks for status bar / trigger viz / device test ──

    def _device_play_test(self, direction: str):
        """Quick local audio test for headset/speaker check."""
        params = get_default_params('P1')
        params['level_db'] = -15.0
        wave = synthesize('P1', params, direction, self.sr,
                          seed=int(time.time() * 1000) % (2 ** 31))
        sd.stop()
        sd.play(wave, self.sr)
        self._playing = True
        self._play_t0 = time.time()
        self._play_dur = len(wave) / self.sr
        self._status(f"耳机测试: P1 {direction}")

    def _status_bar_tick(self):
        # Chest band badge
        s = self._ble_conn_state
        if s == 'connected':
            dpg.configure_item(self._txt_badge_chest, color=C_GREEN)
            dpg.set_value(self._txt_badge_chest,
                          f"胸带  已连接  #{self._ble_pkt_count}")
        elif s == 'connecting':
            dpg.configure_item(self._txt_badge_chest, color=C_AMBER)
            dpg.set_value(self._txt_badge_chest, "胸带  连接中...")
        elif s == 'error':
            dpg.configure_item(self._txt_badge_chest, color=C_RED)
            dpg.set_value(self._txt_badge_chest, "胸带  错误")
        else:
            dpg.configure_item(self._txt_badge_chest, color=C_DIM)
            dpg.set_value(self._txt_badge_chest, "胸带  未连接")

        # Oximeter badge
        s = self._oxi_conn_state
        if s == 'connected':
            dpg.configure_item(self._txt_badge_oxi, color=C_GREEN)
            dpg.set_value(self._txt_badge_oxi,
                          f"血氧  已连接  #{self._oxi_pkt_count}")
        elif s == 'connecting':
            dpg.configure_item(self._txt_badge_oxi, color=C_AMBER)
            dpg.set_value(self._txt_badge_oxi, "血氧  连接中...")
        elif s == 'error':
            dpg.configure_item(self._txt_badge_oxi, color=C_RED)
            dpg.set_value(self._txt_badge_oxi, "血氧  错误")
        else:
            dpg.configure_item(self._txt_badge_oxi, color=C_DIM)
            dpg.set_value(self._txt_badge_oxi, "血氧  未连接")

        # Mic badge
        st = self._snore.status
        snoring = self._snore.is_snoring()
        if st == 'listening' and snoring:
            dpg.configure_item(self._txt_badge_mic, color=C_AMBER)
            dpg.set_value(self._txt_badge_mic, "麦克风  打鼾中")
        elif st == 'listening':
            dpg.configure_item(self._txt_badge_mic, color=C_GREEN)
            dpg.set_value(self._txt_badge_mic, "麦克风  监听")
        elif st == 'error':
            dpg.configure_item(self._txt_badge_mic, color=C_RED)
            dpg.set_value(self._txt_badge_mic, "麦克风  错误")
        else:
            dpg.configure_item(self._txt_badge_mic, color=C_DIM)
            dpg.set_value(self._txt_badge_mic, "麦克风  待启动")

        # Closed-loop state
        cs = self._live_ctrl_state or 'idle'
        active = cs in ('armed', 'triggered', 'playing', 'observe')
        color = C_AMBER if active else (C_GREEN if cs == 'cooldown' else C_DIM)
        dpg.configure_item(self._txt_badge_ctrl, color=color)
        dpg.set_value(self._txt_badge_ctrl, f"闭环: {cs}")

        # Session
        if self._recorder is not None:
            sid = self._recorder.meta.session_id
            dur = max(0.0, time.time() - self._session_start_t)
            h = int(dur // 3600)
            m = int((dur % 3600) // 60)
            s = int(dur % 60)
            dur_s = f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            dpg.configure_item(self._txt_badge_session, color=C_GREEN)
            dpg.set_value(self._txt_badge_session,
                          f"会话: {sid}  ·  已进行 {dur_s}")
        else:
            dpg.configure_item(self._txt_badge_session, color=C_DIM)
            dpg.set_value(self._txt_badge_session, "会话: 未开始")

    def _trigger_viz_tick(self):
        """Update Block-A trigger condition viz on the experiment tab."""
        posture_zh = {
            'supine': '仰卧 (满足)', 'prone': '俯卧',
            'left': '左侧卧', 'right': '右侧卧',
            'upright': '直立/坐', 'unknown': '-',
        }
        p = self._live_posture or 'unknown'
        text = posture_zh.get(p, p)
        if p == 'supine':
            dpg.configure_item(self._txt_trig_posture, color=C_GREEN)
        elif p == 'unknown':
            dpg.configure_item(self._txt_trig_posture, color=C_DIM)
        else:
            dpg.configure_item(self._txt_trig_posture, color=C_AMBER)
        dpg.set_value(self._txt_trig_posture, f"姿态:  {text}")

        # Snoring channel state
        m = self._snore.metrics()
        st = m.get('status', 'idle')
        if st == 'error':
            dpg.configure_item(self._txt_trig_snoring, color=C_RED)
            dpg.set_value(
                self._txt_trig_snoring,
                f"鼾声通道:  错误 ({m.get('error', '')})")
        elif st != 'listening':
            dpg.configure_item(self._txt_trig_snoring, color=C_DIM)
            dpg.set_value(
                self._txt_trig_snoring,
                "鼾声通道:  待麦克风启动")
        else:
            snoring = bool(m.get('snoring'))
            color = C_GREEN if snoring else C_DIM
            tag = '检测到 (满足)' if snoring else '安静'
            dpg.configure_item(self._txt_trig_snoring, color=color)
            dpg.set_value(
                self._txt_trig_snoring,
                f"鼾声通道:  {tag}   "
                f"能量 {m.get('energy_db', 0):.0f} dB · "
                f"低频带能比 {m.get('band_ratio', 0):.2f}")

        # Session-running hint
        if self._recorder is None:
            dpg.configure_item(self._txt_session_hint, color=C_AMBER)
            dpg.set_value(
                self._txt_session_hint,
                "未开始会话 · 自动闭环不会触发 (请点顶部「开始会话」)")
        else:
            dpg.configure_item(self._txt_session_hint, color=C_GREEN)
            dpg.set_value(
                self._txt_session_hint,
                f"会话进行中 · {self._recorder.meta.session_id}")

        try:
            hold = float(dpg.get_value(self._sl_hold))
        except Exception:
            hold = 8.0
        armed = 0.0
        if self._controller is not None:
            try:
                armed = float(self._controller.status().get(
                    'armed_duration', 0.0))
            except Exception:
                armed = 0.0
        pct = min(1.0, armed / hold) if hold > 0 else 0.0
        dpg.set_value(self._prog_trig, pct)
        dpg.configure_item(
            self._prog_trig, overlay=f"{armed:.1f} / {hold:.1f} s")

    # ── build ──

    def build(self):
        dpg.create_context()

        fp = _find_chinese_font()
        if fp:
            with dpg.font_registry():
                with dpg.font(fp, 17) as f_body:
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                with dpg.font(fp, 26) as f_title:
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Default)
                    dpg.add_font_range_hint(dpg.mvFontRangeHint_Chinese_Full)
            dpg.bind_font(f_body)
        else:
            f_title = None

        self._themes()

        with dpg.window(tag='main'):
            self._build_header(f_title)
            self._build_status_bar()

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=6)

            with dpg.tab_bar():
                with dpg.tab(label='  1 · 声音设计  '):
                    self._build_design_tab()
                with dpg.tab(label='  2 · 设备连接  '):
                    self._build_devices_tab()
                with dpg.tab(label='  3 · 实时实验  '):
                    self._build_experiment_tab()
                with dpg.tab(label='  4 · 调试  '):
                    self._build_debug_tab()

            dpg.add_spacer(height=6)
            dpg.add_separator()
            dpg.add_spacer(height=4)
            self._txt_status = dpg.add_text("就绪", color=C_GREEN)

        dpg.bind_theme(self._th_global)
        self._refresh_strat()
        self._refresh_params()
        self._synth()
        self._reload_presets()

    def run(self):
        self.build()
        dpg.create_viewport(title='OSA · 打鼾干预实验工作台',
                            width=1180, height=900)
        dpg.setup_dearpygui()
        dpg.set_primary_window('main', True)
        dpg.show_viewport()
        while dpg.is_dearpygui_running():
            self._tick()
            self._check_export_flash()
            self._ble_tick_scan()
            self._ble_tick_data()
            self._oxi_tick_scan()
            self._oxi_tick_data()
            self._live_tick()
            self._status_bar_tick()
            self._trigger_viz_tick()
            if not self._snore_started:
                self._snore.start()
                self._snore_started = True
            dpg.render_dearpygui_frame()
        # Cleanup on exit
        try:
            if self._recorder is not None:
                self._session_stop()
        except Exception:
            pass
        try:
            self._snore.stop()
        except Exception:
            pass
        try:
            if self._ble_connected and self._ble is not None:
                fut = self._ble_submit(self._ble.disconnect())
                if fut is not None:
                    try:
                        fut.result(timeout=2.0)
                    except Exception:
                        pass
            if self._oxi_connected and self._oxi is not None:
                fut = self._ble_submit(self._oxi.disconnect())
                if fut is not None:
                    try:
                        fut.result(timeout=2.0)
                    except Exception:
                        pass
            if self._ble_loop is not None and self._ble_loop.is_running():
                self._ble_loop.call_soon_threadsafe(self._ble_loop.stop)
        except Exception:
            pass
        dpg.destroy_context()
