"""
OsaRuntime — headless runtime for the OSA experiment rig.

Owns:
  * EventBus, PostureAnalyzer, YamnetSnoreDetector, LocalAudioSink
  * Chest band BLE connection (background asyncio loop)
  * Session recorder (optional, per session)
  * Closed-loop controller (optional, per session)
  * Rolling buffers for plotting + per-trigger snapshots

SpO2 / pulse rate come in through the chest band (HSRG variant relays the
paired PC-68B pulse oximeter via BLE) — there is no separate BLE link to
the oximeter in this build.

Threading notes:
  * BLE work runs on a single dedicated asyncio loop in a bg thread.
  * EventBus callbacks can fire on any thread; handlers only mutate ivars.
  * Public methods are safe to call from the FastAPI request thread.
"""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np
import soundfile as sf

from devices.chestband import ChestBandBLE
from devices.chestband_protocol import DataPacket

from pipeline import (
    EventBus, LocalAudioSink,
    SessionRecorder, SessionMeta, new_session_id,
    PostureAnalyzer, ClosedLoopController, ControllerConfig,
    YamnetSnoreDetector,
)
from pipeline.audio import PlaybackRequest
from sounds.strategies import (
    STRATEGY_REGISTRY, synthesize, get_default_params,
)
from sounds.generator import DEFAULT_SR


ROOT = Path(__file__).resolve().parent.parent
PRESETS_DIR = ROOT / 'presets'
OUTPUT_DIR = ROOT / 'output'
SESSIONS_DIR = ROOT / 'sessions'
STRATEGY_ORDER = ['P1', 'P2', 'P3', 'L1', 'L2']


# Max posture/chest history kept for plotting
CHEST_BUF_SECS = 30
CHEST_FS = 25
CHEST_BUF_MAX = CHEST_FS * CHEST_BUF_SECS

# Long rolling buffers used for "±N seconds around trigger" snapshots.
CHEST_SNAPSHOT_BUF_S = 90       # 90 s chest resp waveform (@25 Hz ≈ 2250 pts)
SPO2_SNAPSHOT_BUF_S = 300       # 5 min SpO2 @ ~1 Hz
SNORE_SNAPSHOT_BUF_S = 300      # 5 min snoring probability @ 4 Hz


def _snore_hint(required: bool, ok: bool, now: bool,
                age, recent_s: float) -> str:
    """Build the status-banner hint for the snoring condition."""
    if not required:
        return '未要求 require_snoring'
    if now:
        return '刚检测到'
    if ok and age is not None:
        return f'最近 {age:.1f}s 内有打鼾 (窗口 {recent_s:.0f}s)'
    if age is None:
        return '启动后从未检测到打鼾'
    return f'距上次打鼾 {age:.1f}s (> {recent_s:.0f}s 窗口已过期)'


class OsaRuntime:
    """Singleton runtime for headless operation of the OSA experiment rig."""

    def __init__(self):
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

        # ── Pipeline core ──
        self.bus = EventBus()
        self.audio_sink = LocalAudioSink()
        self.posture = PostureAnalyzer(self.bus)
        if YamnetSnoreDetector is None:
            raise RuntimeError(
                "YAMNet detector unavailable — install tensorflow + tf-hub.")
        self.snore = YamnetSnoreDetector(self.bus)

        # ── BLE: chest band ──
        self.ble: Optional[ChestBandBLE] = None
        self._ble_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ble_loop_started = threading.Event()
        self._ble_connected = False
        self._ble_state = 'idle'       # idle | connecting | connected | error
        self._ble_err = ''
        self._ble_pkt_count = 0
        self._ble_scan_busy = False
        self._ble_devices: list = []   # [(BLEDevice, rssi)]
        self._ble_latest: Optional[DataPacket] = None
        self._ble_latest_vitals: dict = {
            'spo2': None, 'pulse': None, 'resp': None,
            'batt_mv': None, 'temp_c': None, 'gesture': None,
        }
        # HSRG chest band relays SpO2 / PR / PPG from a paired PC-68B oximeter.
        # Track when we last saw a valid SpO2 / PR so UI can grey out stale
        # values (e.g. PC-68B off, finger out, out of range).
        self._spo2_last_t: float = 0.0
        self._pulse_last_t: float = 0.0
        self._chest_buf: list[float] = []

        # Rolling multi-channel buffers for trigger snapshots.
        from collections import deque
        self._chest_hist = deque(maxlen=int(CHEST_FS * CHEST_SNAPSHOT_BUF_S))
        self._spo2_hist = deque(maxlen=int(SPO2_SNAPSHOT_BUF_S * 2))
        self._snore_hist = deque(maxlen=int(SNORE_SNAPSHOT_BUF_S * 6))

        # ── Session / controller ──
        self.recorder: Optional[SessionRecorder] = None
        self.controller: Optional[ClosedLoopController] = None
        self.session_start_t: float = 0.0

        # Live mirror of controller/posture state (set by bus handlers)
        self._live = {
            'posture': 'unknown',
            'posture_conf': 0.0,
            'ctrl_state': 'idle',
            'ctrl_reason': '',
            'last_strategy': '',
            'last_direction': '',
            'events': [],
        }

        self._controller_cfg_values = {
            'trigger_hold_s': 8.0,
            'retry_trigger_hold_s': 2.0,
            'retry_reset_idle_s': 30.0,
            'debounce_s': 3.0,
            'response_window_s': 3.0,
            'cooldown_s': 180.0,
            'cooldown_no_response_s': 1.0,
            'level_db': -15.0,
            'enabled': True,
            'require_snoring': True,
            'snoring_recent_s': 15.0,
            'confirm_snore_bouts': 1,
            'active_window_start': '',
            'active_window_end': '',
        }

        self.bus.subscribe('posture.sample', self._on_posture_sample)
        self.bus.subscribe('posture.change', self._on_posture_change)
        self.bus.subscribe('intervention.state', self._on_ctrl_state)
        self.bus.subscribe('intervention.triggered', self._on_triggered)
        self.bus.subscribe('intervention.response', self._on_response)
        self.bus.subscribe('intervention.error', self._on_intervention_error)
        self.bus.subscribe('chestband.data', self._on_chest_data)
        self.bus.subscribe('snore.state', self._on_snore_state)
        self.bus.subscribe('snore.state', self._on_snore_state_event)
        self._last_error = None  # {msg, t}

        self._start_ble_loop()
        self.snore.start()

    # ────────────────────────────────────────────────────────────── BLE loop

    def _start_ble_loop(self):
        def _runner():
            self._ble_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._ble_loop)
            self._ble_loop_started.set()
            self._ble_loop.run_forever()

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        self._ble_loop_started.wait(timeout=2.0)

    def _submit(self, coro):
        if self._ble_loop is None:
            return None
        return asyncio.run_coroutine_threadsafe(coro, self._ble_loop)

    # ────────────────────────────────────────────────── Pipeline callbacks

    def _push_event(self, text: str):
        ev = self._live['events']
        ev.append(f"[{time.strftime('%H:%M:%S')}]  {text}")
        if len(ev) > 60:
            del ev[:-60]

    def _on_posture_sample(self, ev):
        s = ev.payload
        self._live['posture'] = s.cls
        self._live['posture_conf'] = float(s.confidence)

    def _on_posture_change(self, ev):
        pc = ev.payload
        self._push_event(
            f"姿态 {pc.prev} → {pc.cur}  (上一段保持 {pc.prev_duration_s:.1f}s)")

    def _on_ctrl_state(self, ev):
        p = ev.payload or {}
        prev = self._live.get('ctrl_state', 'idle')
        new = p.get('state', prev)
        self._live['ctrl_state'] = new
        self._live['ctrl_reason'] = p.get('reason', '')
        if new != prev:
            readable = {
                'idle': '等待中',
                'armed': '条件满足中 (计时开始)',
                'triggered': '触发, 合成音频',
                'playing': '播放干预音',
                'observe': f"观察期 ({p.get('window_s', '-')} s)",
                'cooldown': (
                    f"冷却 ({p.get('cooldown_s', '-')} s · "
                    f"{p.get('reason','')})")
            }.get(new, new)
            self._push_event(f"控制器  {prev} → {new}  · {readable}")

    def _on_snore_state_event(self, ev):
        """Print a line when the snoring flag flips, so the timeline
        shows "开始打鼾 / 停止打鼾" even before any trigger fires."""
        p = ev.payload or {}
        snoring = bool(p.get('snoring', False))
        prev = self._live.get('_snore_flag', False)
        self._live['_snore_flag'] = snoring
        if snoring != prev:
            prob = p.get('snoring_prob')
            prob_s = f" (p={prob:.2f})" if isinstance(prob, (int, float)) else ''
            self._push_event(
                f"{'检测到打鼾' if snoring else '打鼾停止'}{prob_s}")

    def _on_triggered(self, ev):
        p = ev.payload or {}
        strategy = p.get('strategy', '')
        direction = p.get('direction', '')
        self._live['last_strategy'] = strategy
        self._live['last_direction'] = direction
        self._push_event(
            f">> 触发 {strategy}  方向 {direction}  原因 {p.get('reason','')}")
        self._save_trigger_audio(ev.t, strategy, direction)

    def _on_response(self, ev):
        p = ev.payload or {}
        tag = 'OK' if p.get('success') else '--'
        self._push_event(
            f"{tag} 响应 {p.get('reason','')}  潜伏 {p.get('latency_s', 0):.1f}s")

    def _on_intervention_error(self, ev):
        p = ev.payload or {}
        msg = f"干预播放失败: {p.get('error', '未知')}"
        self._last_error = {'msg': msg, 't': ev.t}
        self._push_event(">> " + msg)

    def _on_chest_data(self, ev):
        dp: DataPacket = ev.payload
        self._ble_latest = dp
        self._ble_pkt_count += 1
        v = dp.vitals
        lv = self._ble_latest_vitals
        now_t = float(ev.t)
        if 70 <= (v.spo2_pct or 0) <= 100:
            lv['spo2'] = v.spo2_pct
            self._spo2_last_t = now_t
            self._spo2_hist.append((now_t, float(v.spo2_pct)))
        if 30 <= (v.pulse_rate or 0) <= 220:
            lv['pulse'] = v.pulse_rate
            self._pulse_last_t = now_t
        if 4 <= (v.resp_rate or 0) <= 60:
            lv['resp'] = v.resp_rate
        if v.battery_voltage_mv > 0:
            lv['batt_mv'] = v.battery_voltage_mv
        if 28 <= (v.temperature or 0) <= 42:
            lv['temp_c'] = round(float(v.temperature), 1)
        lv['gesture'] = v.gesture

        if dp.chest_resp is not None and len(dp.chest_resp) > 0:
            self._chest_buf.extend([float(x) for x in dp.chest_resp])
            if len(self._chest_buf) > CHEST_BUF_MAX:
                self._chest_buf = self._chest_buf[-CHEST_BUF_MAX:]
            base_t = float(ev.t)
            n = len(dp.chest_resp)
            for i, y in enumerate(dp.chest_resp):
                sample_t = base_t - (n - 1 - i) / CHEST_FS
                self._chest_hist.append((sample_t, float(y)))

    def _on_snore_state(self, ev):
        p = ev.payload or {}
        prob = p.get('snoring_prob', 0.0)
        self._snore_hist.append((float(ev.t), float(prob),
                                 bool(p.get('snoring', False))))

    # ──────────────────────────────────────────────────────── Public state

    def snapshot(self) -> dict:
        """One dict the web UI reads each tick."""
        buf = np.asarray(self._chest_buf, dtype=np.float32)
        step = max(1, len(buf) // 200)
        chest_plot = buf[::step].tolist() if len(buf) > 0 else []
        chest_t = [i * step / CHEST_FS for i in range(len(chest_plot))]
        chest_rr = self._estimate_rr(buf)

        sn = self.snore.metrics()
        sess_dur = (time.time() - self.session_start_t) if self.recorder else 0.0

        # SpO2 freshness: values older than 5 s are considered stale.
        STALE_S = 5.0
        now_t = time.time()
        vitals_out = dict(self._ble_latest_vitals)
        spo2_age = (now_t - self._spo2_last_t) if self._spo2_last_t > 0 else None
        pulse_age = (now_t - self._pulse_last_t) if self._pulse_last_t > 0 else None
        spo2_stale = spo2_age is None or spo2_age > STALE_S
        pulse_stale = pulse_age is None or pulse_age > STALE_S
        if spo2_stale:
            vitals_out['spo2'] = None
        if pulse_stale:
            vitals_out['pulse'] = None
        # If firmware never computes resp_rate (HSRG variant), fall back to
        # our own peak-based estimate from the chest respiration wave.
        if vitals_out.get('resp') is None and chest_rr and chest_rr > 0:
            vitals_out['resp'] = round(float(chest_rr), 1)

        return {
            't': now_t,
            'chestband': {
                'state': self._ble_state,
                'err': self._ble_err,
                'pkt': self._ble_pkt_count,
                'vitals': vitals_out,
                'chest_t': chest_t,
                'chest_y': chest_plot,
                'chest_rr': chest_rr,
                'spo2_source': 'relay_pc68b',
                'spo2_stale': bool(spo2_stale),
                'spo2_age_s': None if spo2_age is None else round(spo2_age, 1),
                'pulse_stale': bool(pulse_stale),
            },
            'snore': sn,
            'audio': {
                'input_device': self.snore.device,
                'output_device': self.audio_sink.device,
                'input_name': self._device_name(self.snore.device),
                'output_name': self._device_name(self.audio_sink.device),
            },
            'posture': {
                'cls': self._live['posture'],
                'conf': self._live['posture_conf'],
            },
            'controller': self._controller_snapshot(),
            'session': {
                'active': self.recorder is not None,
                'id': self.recorder.meta.session_id if self.recorder else None,
                'subject': (self.recorder.meta.subject_id
                            if self.recorder else None),
                'note': (self.recorder.meta.note
                         if self.recorder else None),
                'mode': (self.recorder.meta.mode if self.recorder else 'A'),
                'duration_s': sess_dur,
                'packets': (self.recorder.packet_count
                            if self.recorder else 0),
                'interventions': (self.recorder.intervention_count
                                  if self.recorder else 0),
            },
            'events_tail': list(self._live['events'])[-40:],
            'last_error': self._last_error,
        }

    def _device_name(self, idx) -> Optional[str]:
        try:
            import sounddevice as sd
            info = sd.query_devices(idx if idx is not None else None)
            return info.get('name')
        except Exception:
            return None

    def _controller_snapshot(self) -> dict:
        """Build a fat controller snapshot the UI can drive a status banner
        off of. Tells you why (or why not) a trigger would fire right now."""
        cfg = dict(self._controller_cfg_values)
        posture = self._live['posture']
        posture_ok = posture in ('supine',)
        try:
            snore_now = bool(self.snore.is_snoring())
        except Exception:
            snore_now = False
        snore_required = bool(cfg.get('require_snoring', True))
        in_session = self.recorder is not None
        enabled = bool(cfg.get('enabled', True))

        stat = (self.controller.status() if self.controller is not None
                else {'state': 'idle', 'armed_duration': 0.0,
                      'cooldown_left': 0.0, 'snoring_age_s': None,
                      'snoring_recent_s': cfg.get('snoring_recent_s', 15.0),
                      'within_active_window': True,
                      'active_window_start': cfg.get('active_window_start',''),
                      'active_window_end': cfg.get('active_window_end',''),
                      })
        recent_s = float(stat.get('snoring_recent_s',
                                  cfg.get('snoring_recent_s', 15.0)))
        age = stat.get('snoring_age_s')
        if snore_now:
            snore_ok = True
        elif age is not None and age <= recent_s:
            snore_ok = True
        else:
            snore_ok = False
        state = stat.get('state', self._live['ctrl_state'])
        armed_dur = float(stat.get('armed_duration', 0.0))
        cooldown_left = float(stat.get('cooldown_left', 0.0))
        hold_s = float(stat.get('effective_trigger_hold_s',
                                cfg['trigger_hold_s']))

        conds = [
            {'key': 'session',  'label': '会话进行中',
             'ok': in_session,
             'hint': '点顶部「开始会话」' if not in_session else ''},
            {'key': 'enabled',  'label': '闭环已启用',
             'ok': enabled,
             'hint': '在「控制器阈值」里打开' if not enabled else ''},
            {'key': 'posture',  'label': '姿态: 仰卧',
             'ok': posture_ok,
             'hint': f'当前 {posture}' if not posture_ok else ''},
            {'key': 'snoring',  'label': '检测到打鼾',
             'ok': (snore_ok or not snore_required),
             'hint': _snore_hint(snore_required, snore_ok, snore_now,
                                 age, recent_s)},
            {'key': 'cooldown', 'label': '不在冷却中',
             'ok': cooldown_left <= 0.01,
             'hint': (f'冷却剩余 {cooldown_left:.1f}s'
                      if cooldown_left > 0.01 else '')},
            {'key': 'window',   'label': '在活跃时段内',
             'ok': bool(stat.get('within_active_window', True)),
             'hint': (f"窗口 {stat.get('active_window_start') or '—'}"
                      f" → {stat.get('active_window_end') or '—'}"
                      if not stat.get('within_active_window', True)
                      else ('未设置窗口，全天启用' if not (
                          stat.get('active_window_start') and
                          stat.get('active_window_end')) else ''))},
        ]
        all_ready = all(c['ok'] for c in conds)
        return {
            'state': state,
            'last_strategy': self._live['last_strategy'],
            'last_direction': self._live['last_direction'],
            'config': cfg,
            'armed_duration': armed_dur,
            'trigger_hold_s': hold_s,
            'armed_fraction': min(1.0, armed_dur / hold_s) if hold_s else 0,
            'cooldown_left': cooldown_left,
            'reason': self._live.get('ctrl_reason', ''),
            'snoring_age_s': age,
            'snoring_recent_s': recent_s,
            'snoring_now': snore_now,
            'conditions': conds,
            'all_ready': all_ready,
            'posture_ok': posture_ok,
            'snore_ok': snore_ok,
            'snore_required': snore_required,
            'session_active': in_session,
            'retry_mode': bool(stat.get('retry_mode', False)),
            'first_trigger_hold_s': float(stat.get(
                'first_trigger_hold_s', cfg['trigger_hold_s'])),
            'retry_trigger_hold_s': float(stat.get(
                'retry_trigger_hold_s',
                cfg.get('retry_trigger_hold_s', 2.0))),
        }

    @staticmethod
    def _estimate_rr(buf: np.ndarray, fs: float = 25.0) -> float:
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
        return rr if 4 <= rr <= 60 else 0.0

    # ──────────────────────────────────────────── Chestband BLE operations

    def chest_scan(self, named_only=True, chestband_only=False,
                   timeout=8.0) -> dict:
        if self._ble_scan_busy:
            return {'ok': False, 'err': 'already scanning'}
        self._ble_scan_busy = True

        async def _do():
            try:
                named = bool(named_only) or bool(chestband_only)
                pairs = await ChestBandBLE.scan(
                    timeout=timeout,
                    named_only=named,
                    chestband_only=bool(chestband_only))
                self._ble_devices = pairs
            except Exception as e:
                self._ble_err = f'scan: {e}'
                self._ble_devices = []
            finally:
                self._ble_scan_busy = False

        fut = self._submit(_do())
        if fut is not None:
            try:
                fut.result(timeout=timeout + 3.0)
            except Exception:
                pass
        devices = [{
            'name': dev.name or '(unknown)',
            'address': dev.address,
            'rssi': rssi,
        } for dev, rssi in self._ble_devices]
        return {'ok': True, 'devices': devices}

    def chest_connect(self, address: str) -> dict:
        if self._ble_connected or self._ble_state == 'connecting':
            return {'ok': False, 'err': 'already connected/connecting'}
        device = None
        for dev, _ in self._ble_devices:
            if dev.address == address:
                device = dev
                break
        if device is None:
            return {'ok': False, 'err': 'address not in latest scan'}

        self._ble_state = 'connecting'
        self._ble_err = ''

        async def _do():
            try:
                self.ble = ChestBandBLE()
                await self.ble.connect(device)
                self._ble_pkt_count = 0

                def _on_data(dp: DataPacket):
                    try:
                        self.bus.emit('chestband.data', dp, src='chestband')
                    except Exception:
                        pass

                await self.ble.start_receiving(_on_data)
                self._ble_connected = True
                self._ble_state = 'connected'
            except Exception as e:
                self._ble_connected = False
                self._ble_state = 'error'
                self._ble_err = str(e)

        self._submit(_do())
        return {'ok': True}

    def chest_disconnect(self) -> dict:
        if not self._ble_connected:
            return {'ok': False, 'err': 'not connected'}
        self._ble_connected = False
        self._ble_state = 'idle'

        async def _do():
            try:
                if self.ble:
                    await self.ble.disconnect()
            except Exception:
                pass

        self._submit(_do())
        return {'ok': True}

    # ────────────────────────────────────────────── Controller / sessions

    def set_controller_config(self, patch: dict) -> dict:
        v = self._controller_cfg_values
        for k in ('trigger_hold_s', 'retry_trigger_hold_s',
                  'retry_reset_idle_s', 'debounce_s', 'response_window_s',
                  'cooldown_s', 'cooldown_no_response_s',
                  'level_db', 'snoring_recent_s'):
            if k in patch:
                v[k] = float(patch[k])
        for k in ('enabled', 'require_snoring'):
            if k in patch:
                v[k] = bool(patch[k])
        if 'confirm_snore_bouts' in patch:
            v['confirm_snore_bouts'] = max(0, int(patch['confirm_snore_bouts']))
        for k in ('active_window_start', 'active_window_end'):
            if k in patch:
                v[k] = str(patch[k] or '').strip()
        if self.controller is not None:
            self.controller.cfg = self._build_cfg()
        if self.posture is not None:
            self.posture.debounce_s = v['debounce_s']
        return {'ok': True, 'config': dict(v)}

    def _build_cfg(self) -> ControllerConfig:
        v = self._controller_cfg_values
        return ControllerConfig(
            trigger_postures=('supine',),
            require_snoring=v['require_snoring'],
            trigger_hold_s=v['trigger_hold_s'],
            retry_trigger_hold_s=v.get('retry_trigger_hold_s', 2.0),
            retry_reset_idle_s=v.get('retry_reset_idle_s', 30.0),
            snoring_recent_s=v['snoring_recent_s'],
            confirm_snore_bouts=int(v.get('confirm_snore_bouts', 1)),
            strategy_pool=('P1', 'P2', 'P3'),
            direction_policy='opposite',
            level_db=v['level_db'],
            response_window_s=v['response_window_s'],
            cooldown_s=v['cooldown_s'],
            cooldown_no_response_s=v['cooldown_no_response_s'],
            active_window_start=v.get('active_window_start', ''),
            active_window_end=v.get('active_window_end', ''),
            enabled=v['enabled'],
        )

    # ── Audio device selection ──

    def list_audio_devices(self) -> dict:
        """Enumerate sounddevice devices via a short-lived subprocess so
        BT devices that pair after process start (AirPods etc.) become
        visible without disturbing our live InputStream.
        """
        raw, default_in, default_out, err = self._query_devices_subprocess()
        if err:
            return {'ok': False, 'err': err,
                    'inputs': [], 'outputs': [],
                    'selected_input': self.snore.device,
                    'selected_output': self.audio_sink.device}
        inputs, outputs = [], []
        for idx, d in enumerate(raw):
            entry = {
                'index': idx,
                'name': d.get('name', f'device {idx}'),
                'hostapi': d.get('hostapi'),
                'max_input_channels': d.get('max_input_channels', 0),
                'max_output_channels': d.get('max_output_channels', 0),
                'default_samplerate': d.get('default_samplerate'),
            }
            if entry['max_input_channels'] > 0:
                inputs.append({**entry, 'is_default': idx == default_in})
            if entry['max_output_channels'] > 0:
                outputs.append({**entry, 'is_default': idx == default_out})
        return {
            'ok': True,
            'inputs': inputs,
            'outputs': outputs,
            'selected_input': self.snore.device,
            'selected_output': self.audio_sink.device,
        }

    @staticmethod
    def _query_devices_subprocess(timeout: float = 4.0):
        import sys as _sys
        code = (
            "import json, sys\n"
            "try:\n"
            "    import sounddevice as sd\n"
            "    devs = [dict(d) for d in sd.query_devices()]\n"
            "    try:\n"
            "        d_in, d_out = sd.default.device\n"
            "    except Exception:\n"
            "        d_in, d_out = None, None\n"
            "    json.dump({'devs': devs,\n"
            "               'default_in': d_in,\n"
            "               'default_out': d_out}, sys.stdout)\n"
            "except Exception as e:\n"
            "    json.dump({'err': str(e)}, sys.stdout)\n"
        )
        try:
            out = subprocess.run(
                [_sys.executable, '-c', code],
                capture_output=True, text=True, timeout=timeout)
            if out.returncode != 0:
                return [], None, None, (out.stderr or '').strip() or 'subprocess failed'
            data = json.loads(out.stdout)
            if 'err' in data:
                return [], None, None, data['err']
            return (data.get('devs', []),
                    data.get('default_in'),
                    data.get('default_out'),
                    None)
        except subprocess.TimeoutExpired:
            return [], None, None, 'subprocess timeout'
        except Exception as e:
            return [], None, None, str(e)

    def set_audio_devices(self, input_device=None, output_device=None,
                          set_input: bool = False,
                          set_output: bool = False) -> dict:
        """Set input (mic) and/or output (playback) devices.
        Use the explicit `set_input/set_output` flags so None can mean
        "switch back to OS default" instead of "leave unchanged".
        """
        if set_input:
            try:
                self.snore.set_device(input_device)
            except Exception as e:
                return {'ok': False, 'err': f'input: {e}'}
        if set_output:
            try:
                self.audio_sink.set_device(output_device)
            except Exception as e:
                return {'ok': False, 'err': f'output: {e}'}
        return {
            'ok': True,
            'selected_input': self.snore.device,
            'selected_output': self.audio_sink.device,
            'snore_status': self.snore.status,
            'snore_err': self.snore.error,
        }

    def set_snore_config(self, patch: dict) -> dict:
        kwargs = {}
        if 'snore_prob_thresh' in patch:
            kwargs['snore_prob_thresh'] = patch['snore_prob_thresh']
        self.snore.set_thresholds(**kwargs)
        cfg = {
            'snore_prob_thresh': getattr(self.snore,
                                         'snore_prob_thresh', None),
        }
        return {'ok': True, 'config': cfg}

    def session_start(self, tag='', subject='', note='') -> dict:
        if self.recorder is not None:
            return {'ok': False, 'err': 'session already running'}
        sid = new_session_id(tag.strip())
        meta = SessionMeta(
            session_id=sid,
            started_at=datetime.now().isoformat(timespec='seconds'),
            subject_id=subject.strip(),
            note=note.strip(),
            protocol='block_a_pilot',
            mode='A',
            config=asdict(self._build_cfg()),
        )
        self.recorder = SessionRecorder(self.bus, meta)
        self.session_start_t = time.time()
        self.posture.debounce_s = self._controller_cfg_values['debounce_s']
        self.controller = ClosedLoopController(
            self.bus, self.audio_sink, self._build_cfg(),
            snoring_provider=self.snore.is_snoring)
        self.bus.emit('session.marker', {'kind': 'start', 'session': sid},
                      src='web')
        self._live['events'] = []
        self._push_event(f">> 会话开始 · 受试 "
                         f"{subject.strip() or '-'}  · id {sid}")
        return {'ok': True, 'session_id': sid}

    def session_stop(self) -> dict:
        if self.recorder is None:
            return {'ok': False, 'err': 'no session'}
        self.bus.emit('session.marker', {'kind': 'stop'}, src='web')
        try:
            if self.controller:
                self.controller.close()
            self.recorder.close()
        finally:
            sid = self.recorder.meta.session_id
            self.controller = None
            self.recorder = None
        return {'ok': True, 'session_id': sid}

    def manual_trigger(self) -> dict:
        if self.controller is not None:
            self.controller.manual_trigger()
            return {'ok': True, 'mode': 'session'}
        # No session: fire a one-shot unlogged test playback.
        strategy = random.choice(['P1', 'P2', 'P3'])
        direction = random.choice(['left', 'right'])
        params = get_default_params(strategy)
        params['level_db'] = self._controller_cfg_values['level_db']
        wave = synthesize(strategy, params, direction, DEFAULT_SR,
                          seed=random.randint(0, 2 ** 31 - 1))
        self.audio_sink.play(PlaybackRequest(wave, DEFAULT_SR, {
            'strategy': strategy, 'direction': direction, 'manual': True,
        }))
        return {'ok': True, 'mode': 'preview',
                'strategy': strategy, 'direction': direction}

    def stop_audio(self):
        self.audio_sink.stop()
        return {'ok': True}

    def test_sound(self, strategy='P1', direction='left',
                   level_db: Optional[float] = None) -> dict:
        if strategy not in STRATEGY_REGISTRY:
            return {'ok': False, 'err': 'unknown strategy'}
        params = get_default_params(strategy)
        if level_db is not None:
            params['level_db'] = float(level_db)
        wave = synthesize(strategy, params, direction, DEFAULT_SR,
                          seed=random.randint(0, 2 ** 31 - 1))
        self.audio_sink.play(PlaybackRequest(wave, DEFAULT_SR, {
            'strategy': strategy, 'direction': direction, 'test': True,
        }))
        return {'ok': True, 'strategy': strategy, 'direction': direction}

    def _save_trigger_audio(self, trigger_at: float, strategy: str,
                            direction: str,
                            chest_before_s: float = 30.0,
                            chest_after_s: float = 30.0,
                            mic_before_s: float = 10.0,
                            mic_after_s: float = 10.0,
                            spo2_before_s: float = 30.0,
                            spo2_after_s: float = 60.0):
        """After each trigger, dump a ±N s multi-channel snapshot:
            sessions/<id>/events/<YYYYMMDD_HHMMSS>_A_<strategy>_<dir>.npz
            sessions/<id>/events/<same>_mic.wav        (mic around trigger)
            sessions/<id>/events/<same>_played.wav     (the intervention we played)
        Everything is timestamped relative to `trigger_at` (unix seconds).
        Runs on a short background thread so we don't block the event loop.
        """
        if self.recorder is None:
            return
        sess_dir = SESSIONS_DIR / self.recorder.meta.session_id
        ev_dir = sess_dir / 'events'
        ev_dir.mkdir(parents=True, exist_ok=True)
        block = self.recorder.meta.mode

        played_wave = getattr(self.audio_sink, 'last_wave', None)
        played_sr = int(getattr(self.audio_sink, 'last_sample_rate', 0) or 0)

        def _slice_hist(hist, t0, t1, with_flag=False):
            ts_out, v_out, f_out = [], [], []
            for row in list(hist):
                if with_flag:
                    t, v, f = row
                    if t0 <= t <= t1:
                        ts_out.append(t); v_out.append(v); f_out.append(f)
                else:
                    t, v = row
                    if t0 <= t <= t1:
                        ts_out.append(t); v_out.append(v)
            if with_flag:
                return (np.asarray(ts_out), np.asarray(v_out),
                        np.asarray(f_out, dtype=bool))
            return np.asarray(ts_out), np.asarray(v_out)

        def _do():
            tail = max(chest_after_s, mic_after_s, spo2_after_s) + 0.2
            time.sleep(tail)
            try:
                ts_str = datetime.fromtimestamp(trigger_at).strftime(
                    '%Y%m%d_%H%M%S')
                base = f"{ts_str}_{block}_{strategy}_{direction or 'center'}"

                span = mic_before_s + mic_after_s
                try:
                    wav = self.snore.snapshot(span + 0.2)
                except Exception:
                    wav = np.zeros(0, dtype=np.float32)
                if wav.size > 0:
                    sf.write(str(ev_dir / f"{base}_mic.wav"),
                             wav, self.snore.sr)

                if played_wave is not None and played_sr > 0:
                    sf.write(str(ev_dir / f"{base}_played.wav"),
                             played_wave, int(played_sr))

                chest_t, chest_y = _slice_hist(
                    self._chest_hist,
                    trigger_at - chest_before_s, trigger_at + chest_after_s)
                spo2_t, spo2_y = _slice_hist(
                    self._spo2_hist,
                    trigger_at - spo2_before_s, trigger_at + spo2_after_s)
                snore_t, snore_p, snore_flag = _slice_hist(
                    self._snore_hist,
                    trigger_at - chest_before_s, trigger_at + chest_after_s,
                    with_flag=True)

                np.savez_compressed(
                    str(ev_dir / f"{base}.npz"),
                    trigger_at=np.float64(trigger_at),
                    block=block,
                    strategy=strategy,
                    direction=direction or 'center',
                    chest_t=chest_t.astype(np.float64),
                    chest_y=chest_y.astype(np.float32),
                    chest_fs=np.float32(CHEST_FS),
                    spo2_t=spo2_t.astype(np.float64),
                    spo2_y=spo2_y.astype(np.float32),
                    snore_t=snore_t.astype(np.float64),
                    snore_p=snore_p.astype(np.float32),
                    snore_flag=snore_flag,
                )
            except Exception as e:
                try:
                    self.bus.emit('session.error',
                                  {'where': 'save_trigger_snapshot',
                                   'error': str(e)}, src='runtime')
                except Exception:
                    pass

        threading.Thread(target=_do, daemon=True).start()

    # ──────────────────────────────────────────────── Sounds / strategies

    def list_strategies(self) -> list:
        out = []
        for k in STRATEGY_ORDER:
            s = STRATEGY_REGISTRY[k]
            out.append({
                'key': k,
                'name': getattr(s, 'name', k),
                'description': getattr(s, 'description', ''),
                'has_direction': bool(getattr(s, 'has_direction', True)),
                'params': [{
                    'key': p.key,
                    'label': p.label,
                    'unit': getattr(p, 'unit', ''),
                    'group': getattr(p, 'group', 'general'),
                    'min': float(p.min_val),
                    'max': float(p.max_val),
                    'step': float(p.step),
                    'default': float(p.default),
                } for p in s.params],
            })
        return out

    def synth_wave(self, strategy: str, params: dict,
                   direction: str, seed: Optional[int] = None) -> np.ndarray:
        sdef = STRATEGY_REGISTRY[strategy]
        d = direction if sdef.has_direction else 'center'
        p = dict(get_default_params(strategy))
        p.update(params or {})
        s = seed if seed is not None else random.randint(0, 2 ** 31 - 1)
        return synthesize(strategy, p, d, DEFAULT_SR, seed=s)

    def preview_wave(self, strategy: str, params: dict,
                     direction: str) -> dict:
        try:
            w = self.synth_wave(strategy, params, direction)
        except Exception as e:
            return {'ok': False, 'err': str(e)}
        n = len(w)
        step = max(1, n // 2000)
        t_ms = (np.arange(0, n, step) / DEFAULT_SR * 1000.0).tolist()
        L = w[::step, 0].tolist()
        R = w[::step, 1].tolist()
        return {
            'ok': True, 'sr': DEFAULT_SR, 'duration_ms': n / DEFAULT_SR * 1000.0,
            't_ms': t_ms, 'L': L, 'R': R,
        }

    def play_strategy(self, strategy: str, params: dict,
                      direction: str, repeats=1, gap_s=0.5) -> dict:
        try:
            w = self.synth_wave(strategy, params, direction)
        except Exception as e:
            return {'ok': False, 'err': str(e)}
        if repeats > 1:
            gap = np.zeros((int(gap_s * DEFAULT_SR), 2), dtype=w.dtype)
            parts = []
            for i in range(repeats):
                parts.append(w)
                if i < repeats - 1:
                    parts.append(gap)
            w = np.concatenate(parts)
        try:
            self.audio_sink.play(PlaybackRequest(w, DEFAULT_SR, {
                'strategy': strategy, 'direction': direction, 'preview': True,
            }))
        except Exception as e:
            return {'ok': False, 'err': f'audio: {e}'}
        return {'ok': True, 'duration_ms': len(w) / DEFAULT_SR * 1000.0}

    def export_wav(self, strategy: str, params: dict,
                   direction: str) -> dict:
        try:
            w = self.synth_wave(strategy, params, direction)
        except Exception as e:
            return {'ok': False, 'err': str(e)}
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = f"{strategy}_{direction}_{ts}.wav"
        fp = OUTPUT_DIR / name
        sf.write(str(fp), w, DEFAULT_SR)
        return {'ok': True, 'path': str(fp), 'name': name}

    def batch_export(self) -> dict:
        levels = [-30, -20, -10, -6]
        n = 0
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        for sk in STRATEGY_ORDER:
            sdef = STRATEGY_REGISTRY[sk]
            pr = dict(get_default_params(sk))
            dirs = ['left', 'right'] if sdef.has_direction else ['center']
            for d in dirs:
                for lv in levels:
                    pr['level_db'] = lv
                    w = synthesize(sk, pr, d, DEFAULT_SR,
                                   seed=random.randint(0, 2 ** 31 - 1))
                    sf.write(str(OUTPUT_DIR / f"{sk}_{d}_{lv}dB_{ts}.wav"),
                             w, DEFAULT_SR)
                    n += 1
        return {'ok': True, 'count': n}

    # ── Presets ──

    def list_presets(self) -> list:
        out = []
        for f in sorted(PRESETS_DIR.glob('*.json')):
            try:
                with open(f, 'r', encoding='utf-8') as fh:
                    out.append(json.load(fh))
            except Exception:
                continue
        return out

    def save_preset(self, name: str, strategy: str, direction: str,
                    params: dict, note: str = '', seed: int = 42) -> dict:
        name = (name or '').strip()
        if not name:
            return {'ok': False, 'err': 'empty name'}
        obj = {
            'name': name,
            'strategy': strategy,
            'direction': direction,
            'created_at': datetime.now().isoformat(timespec='seconds'),
            'note': note or '',
            'params': dict(params or {}),
            'seed': int(seed),
        }
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in name)
        with open(PRESETS_DIR / f'{safe}.json', 'w', encoding='utf-8') as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        return {'ok': True}

    def delete_preset(self, name: str) -> dict:
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in name)
        fp = PRESETS_DIR / f'{safe}.json'
        if fp.exists():
            fp.unlink()
            return {'ok': True}
        return {'ok': False, 'err': 'not found'}

    # ── Session history ──

    def list_sessions(self, limit: int = 10) -> list:
        if not SESSIONS_DIR.exists():
            return []
        dirs = sorted([p for p in SESSIONS_DIR.iterdir() if p.is_dir()],
                      key=lambda p: p.name, reverse=True)
        out = []
        for d in dirs[:limit]:
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
            dur_s = None
            try:
                t0 = datetime.fromisoformat(summ.get('started_at') or
                                            meta.get('started_at'))
                t1 = datetime.fromisoformat(summ.get('ended_at'))
                dur_s = int((t1 - t0).total_seconds())
            except Exception:
                pass
            out.append({
                'id': d.name,
                'started_at': meta.get('started_at'),
                'subject': meta.get('subject_id'),
                'note': meta.get('note'),
                'duration_s': dur_s,
                'interventions': summ.get('interventions'),
                'packets': summ.get('chestband_packets'),
                'ongoing': not summ,
            })
        return out

    def open_sessions_dir(self):
        try:
            subprocess.Popen(['open', str(SESSIONS_DIR)])
            return {'ok': True}
        except Exception as e:
            return {'ok': False, 'err': str(e)}

    # ── Replay: session detail + per-event bundle ──

    def session_detail(self, session_id: str) -> dict:
        sid = ''.join(c for c in session_id if c.isalnum() or c in '-_')
        d = SESSIONS_DIR / sid
        if not d.is_dir():
            return {'ok': False, 'err': 'session not found'}
        meta = {}
        try:
            if (d / 'meta.json').exists():
                meta = json.loads((d / 'meta.json').read_text(encoding='utf-8'))
        except Exception:
            pass
        summary = {}
        try:
            if (d / 'summary.json').exists():
                summary = json.loads(
                    (d / 'summary.json').read_text(encoding='utf-8'))
        except Exception:
            pass
        report_summary = {}
        if (d / 'report' / 'summary.json').exists():
            try:
                report_summary = json.loads(
                    (d / 'report' / 'summary.json').read_text(encoding='utf-8'))
            except Exception:
                pass

        interventions = []
        try:
            if (d / 'interventions.jsonl').exists():
                for line in (d / 'interventions.jsonl').read_text(
                        encoding='utf-8').splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        interventions.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass

        responses = []
        try:
            if (d / 'events.jsonl').exists():
                for line in (d / 'events.jsonl').read_text(
                        encoding='utf-8').splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                        if e.get('kind') == 'intervention.response':
                            responses.append(e)
                    except Exception:
                        pass
        except Exception:
            pass
        r_it = iter(responses)
        events = []
        for iv in interventions:
            resp_evt = next(r_it, None)
            resp = (resp_evt or {}).get('payload') or {}
            t = float(iv.get('t', 0.0))
            ts_str = datetime.fromtimestamp(t).strftime('%Y%m%d_%H%M%S')
            block = iv.get('block', meta.get('mode', 'A'))
            strategy = iv.get('strategy', '')
            direction = iv.get('direction') or 'center'
            base = f"{ts_str}_{block}_{strategy}_{direction}"
            event_dir = d / 'events'
            files = {}
            for suffix, key in (('.npz', 'npz'),
                                ('_mic.wav', 'mic'),
                                ('_played.wav', 'played')):
                fp = event_dir / (base + suffix)
                if fp.exists():
                    files[key] = f"/api/history/{sid}/event/{base}{suffix}"
            events.append({
                't': t,
                'time_str': datetime.fromtimestamp(t).strftime('%H:%M:%S'),
                'block': block,
                'strategy': strategy,
                'direction': direction,
                'level_db': iv.get('level_db'),
                'reason': iv.get('reason'),
                'success': bool(resp.get('success', False)),
                'latency_s': resp.get('latency_s'),
                'response_reason': resp.get('reason'),
                'base': base,
                'files': files,
            })

        return {
            'ok': True,
            'id': sid,
            'meta': meta,
            'summary': summary,
            'report_summary': report_summary,
            'events': events,
            'has_report': (d / 'report' / 'strategy_report.md').exists(),
        }

    @staticmethod
    def session_event_file(session_id: str, fname: str) -> Optional[Path]:
        """Validate and return the absolute path of an event asset."""
        sid = ''.join(c for c in session_id if c.isalnum() or c in '-_')
        safe_name = Path(fname).name
        p = SESSIONS_DIR / sid / 'events' / safe_name
        return p if p.is_file() else None

    def run_session_analysis(self, session_id: str) -> dict:
        sid = ''.join(c for c in session_id if c.isalnum() or c in '-_')
        d = SESSIONS_DIR / sid
        if not d.is_dir():
            return {'ok': False, 'err': 'session not found'}
        script = ROOT / 'scripts' / 'analyze_night.py'
        try:
            subprocess.run(
                [sys.executable, str(script), str(d)],
                check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as e:
            return {'ok': False, 'err': e.stderr or e.stdout or str(e)}
        except Exception as e:
            return {'ok': False, 'err': str(e)}
        return self.session_detail(session_id)

    # ── cleanup ──

    def shutdown(self):
        try:
            self.session_stop()
        except Exception:
            pass
        try:
            self.snore.stop()
        except Exception:
            pass
        try:
            if self._ble_connected and self.ble:
                fut = self._submit(self.ble.disconnect())
                if fut:
                    fut.result(timeout=2.0)
        except Exception:
            pass
        try:
            if self._ble_loop and self._ble_loop.is_running():
                self._ble_loop.call_soon_threadsafe(self._ble_loop.stop)
        except Exception:
            pass


_runtime_lock = threading.Lock()
_runtime: Optional[OsaRuntime] = None


def get_runtime() -> OsaRuntime:
    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = OsaRuntime()
        return _runtime
