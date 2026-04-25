"""
SessionRecorder — persists an experiment session to disk.

Layout (all files under `sessions/<session_id>/`):

  meta.json        session-wide metadata (start_ts, subject_id, config, …)
  events.jsonl     every pipeline event, time-ordered, one JSON per line
  chestband.npz    concatenated waveforms (ECG×4, resp, accel) + vitals CSV
  chestband.csv    one row per second: ts, SpO2, PR, RR, temp, gesture, batt
  interventions.jsonl  what was played, when, with which params

The design goal is "append-cheap, seek-free". We subscribe to EventBus topics
and buffer in memory; a background flusher writes to disk every few seconds
so a crash loses at most that window.
"""

from __future__ import annotations

import csv
import json
import threading
import time
from dataclasses import dataclass, asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .events import Event, EventBus

SESSIONS_DIR = Path(__file__).resolve().parent.parent / 'sessions'


def _json_default(o):
    if is_dataclass(o):
        return asdict(o)
    if isinstance(o, np.ndarray):
        return o.tolist() if o.size < 64 else f'<ndarray shape={o.shape}>'
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    return str(o)


@dataclass
class SessionMeta:
    session_id: str
    started_at: str              # ISO-8601
    subject_id: str = ''
    note: str = ''
    protocol: str = 'block_a_posture'   # 'block_a_posture' | 'block_b_arousal' | 'phase1_screen'
    # Experiment mode tag — we only ever run one block per night. Kept
    # separate from `protocol` so the latter can stay for free-form text
    # while analysis scripts can trust this enum.
    mode: str = 'A'              # 'A' | 'B'
    config: dict = None          # strategy pool, thresholds, cooldowns, ...

    def __post_init__(self):
        if self.config is None:
            self.config = {}


class SessionRecorder:
    """Subscribes to EventBus topics and persists everything to disk."""

    FLUSH_SECS = 2.0

    def __init__(self, bus: EventBus, meta: SessionMeta,
                 root: Optional[Path] = None):
        self.bus = bus
        self.meta = meta

        self.dir = (root or SESSIONS_DIR) / meta.session_id
        self.dir.mkdir(parents=True, exist_ok=True)

        # Open append-mode line files
        self._events = open(self.dir / 'events.jsonl', 'a', buffering=1,
                            encoding='utf-8')
        self._cb_csv = open(self.dir / 'chestband.csv', 'a', newline='',
                            encoding='utf-8')
        self._cb_w = csv.writer(self._cb_csv)
        if self._cb_csv.tell() == 0:
            self._cb_w.writerow([
                'ts', 'packet_sn', 'spo2_pct', 'pulse_rate', 'resp_rate',
                'heart_rate', 'gesture', 'temperature', 'battery_mv',
            ])
        self._int_f = open(self.dir / 'interventions.jsonl', 'a', buffering=1,
                           encoding='utf-8')

        # In-memory buffers for waveform blocks (flushed periodically)
        self._wave_lock = threading.Lock()
        self._wave_buf: dict[str, list] = {
            'ts': [], 'chest_resp': [], 'abd_resp': [],
            'ecg_ch1': [], 'ecg_ch2': [], 'ecg_ch3': [], 'ecg_ch4': [],
            'accel_x': [], 'accel_y': [], 'accel_z': [],
            'spo2_wave': [],
        }
        self._block_idx = 0

        # Persist meta immediately
        with open(self.dir / 'meta.json', 'w', encoding='utf-8') as f:
            json.dump(asdict(meta), f, ensure_ascii=False, indent=2)

        # Subscribe
        self._unsubs = [
            bus.subscribe('chestband.data', self._on_chestband),
            bus.subscribe('intervention.triggered', self._on_intervention),
            bus.subscribe('posture.change', self._on_generic),
            bus.subscribe('sensor.status', self._on_generic),
            bus.subscribe('session.marker', self._on_generic),
            bus.subscribe('intervention.state', self._on_generic),
            bus.subscribe('intervention.response', self._on_generic),
            bus.subscribe('snore.state', self._on_generic),
        ]

        # Background flusher
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

        # Stats
        self.packet_count = 0
        self.intervention_count = 0

    # ── subscribers ──

    def _on_chestband(self, ev: Event):
        dp = ev.payload
        v = getattr(dp, 'vitals', None)
        self.packet_count += 1

        # per-second vitals row
        self._cb_w.writerow([
            f'{ev.t:.3f}',
            getattr(dp, 'packet_sn', ''),
            getattr(v, 'spo2_pct', '') if v else '',
            getattr(v, 'pulse_rate', '') if v else '',
            getattr(v, 'resp_rate', '') if v else '',
            getattr(v, 'heart_rate', '') if v else '',
            getattr(v, 'gesture', '') if v else '',
            getattr(v, 'temperature', '') if v else '',
            getattr(v, 'battery_voltage_mv', '') if v else '',
        ])

        # waveforms → buffer
        with self._wave_lock:
            b = self._wave_buf
            b['ts'].append(ev.t)
            for key in ('chest_resp', 'abd_resp',
                        'ecg_ch1', 'ecg_ch2', 'ecg_ch3', 'ecg_ch4',
                        'accel_x', 'accel_y', 'accel_z', 'spo2_wave'):
                arr = getattr(dp, key, None)
                if arr is not None:
                    b[key].append(np.asarray(arr))
                else:
                    # keep ragged arrays aligned: None-placeholder filtered at
                    # flush time
                    b[key].append(None)

        # summary event line
        self._events.write(json.dumps({
            't': ev.t, 'kind': 'chestband.summary', 'sn': getattr(dp, 'packet_sn', None),
            'vitals': {
                'spo2': getattr(v, 'spo2_pct', None) if v else None,
                'pr':   getattr(v, 'pulse_rate', None) if v else None,
                'rr':   getattr(v, 'resp_rate', None) if v else None,
                'temp': getattr(v, 'temperature', None) if v else None,
                'gesture': getattr(v, 'gesture', None) if v else None,
            },
        }, default=_json_default) + '\n')

    def _on_intervention(self, ev: Event):
        self.intervention_count += 1
        payload = ev.payload or {}
        # Tag every logged intervention with the session's block mode so
        # analyzers can group A-night vs B-night trials without having
        # to cross-reference meta.json.
        rec = {'t': ev.t, 'block': self.meta.mode, **payload}
        self._int_f.write(json.dumps(rec, ensure_ascii=False,
                                     default=_json_default) + '\n')

    def _on_generic(self, ev: Event):
        self._events.write(json.dumps({
            't': ev.t, 'kind': ev.kind,
            'src': ev.src,
            'payload': ev.payload,
        }, ensure_ascii=False, default=_json_default) + '\n')

    # ── flusher ──

    def _flush_loop(self):
        while not self._stop.wait(self.FLUSH_SECS):
            self._flush_waves()

    def _flush_waves(self):
        with self._wave_lock:
            if not self._wave_buf['ts']:
                return
            buf = self._wave_buf
            self._wave_buf = {k: [] for k in buf}

        out = {'ts': np.array(buf['ts'], dtype=np.float64)}
        for key in ('chest_resp', 'abd_resp',
                    'ecg_ch1', 'ecg_ch2', 'ecg_ch3', 'ecg_ch4',
                    'accel_x', 'accel_y', 'accel_z', 'spo2_wave'):
            valid = [a for a in buf[key] if a is not None]
            if not valid:
                continue
            try:
                out[key] = np.stack(valid, axis=0).astype(
                    np.int16 if key.startswith(('ecg', 'accel', 'chest',
                                                'abd')) else np.uint8)
            except Exception:
                # ragged (rare) — fall back to object array of lists
                out[key] = np.array([np.asarray(a) for a in valid],
                                    dtype=object)

        block = self.dir / f'chestband_{self._block_idx:04d}.npz'
        np.savez_compressed(block, **out)
        self._block_idx += 1

    # ── lifecycle ──

    def close(self):
        for u in self._unsubs:
            try: u()
            except Exception: pass
        self._stop.set()
        self._flusher.join(timeout=self.FLUSH_SECS * 2)
        self._flush_waves()
        for f in (self._events, self._cb_csv, self._int_f):
            try: f.close()
            except Exception: pass

        # Final session summary
        ended = datetime.now().isoformat(timespec='seconds')
        with open(self.dir / 'summary.json', 'w', encoding='utf-8') as f:
            json.dump({
                'session_id': self.meta.session_id,
                'started_at': self.meta.started_at,
                'ended_at': ended,
                'chestband_packets': self.packet_count,
                'interventions': self.intervention_count,
                'blocks': self._block_idx,
            }, f, ensure_ascii=False, indent=2)


def new_session_id(tag: str = '') -> str:
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    return f"{ts}_{tag}" if tag else ts
