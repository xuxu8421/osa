"""
MicSnoreDetector — pilot-stage snoring detector from system mic input.

Wires into `ClosedLoopController.snoring_provider` so the design-spec
trigger "supine ∧ sustained snoring" can be exercised end-to-end while
the headset platform / SDK is still pending. When AirPods (or any other
mic) is selected as the system input, sounddevice will pick it up.

This is intentionally a small heuristic — energy + low-band spectral
ratio + hangover smoothing. Plenty good enough to validate the
sense → react → play → record loop on a pilot subject (the user themself,
or a snoring volunteer in a quiet room). It is *not* a clinical-grade
snore detector. Swap in a learned model later via the same interface.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import numpy as np
import sounddevice as sd


class MicSnoreDetector:
    """Real-time snoring proxy from default mic."""

    def __init__(self,
                 bus=None,
                 sample_rate: int = 16000,
                 frame_s: float = 0.064,
                 window_s: float = 1.5,
                 hangover_s: float = 1.2,
                 energy_db: float = -45.0,
                 band_ratio_min: float = 0.55,
                 band_hz: tuple = (80.0, 500.0),
                 ring_s: float = 30.0,
                 device: Optional[int] = None):
        self.bus = bus
        self.sr = sample_rate
        # sounddevice input device index/name; None → OS default.
        # Explicit device lets us pin the mic to the Mac built-in input so
        # that AirPods can stay in A2DP stereo for output.
        self.device: Optional[int] = device
        self.frame_n = max(64, int(sample_rate * frame_s))
        self.window_n = int(sample_rate * window_s)
        self.hangover_s = hangover_s
        # Thresholds are live-tunable from the GUI.
        self.energy_db = energy_db
        self.band_ratio_min = band_ratio_min
        self.band_hz = band_hz

        self._stream: Optional[sd.InputStream] = None
        self._buf = np.zeros(self.window_n, dtype=np.float32)
        # Long rolling ring buffer for ±Ns snapshot around trigger events.
        self._ring_n = int(sample_rate * ring_s)
        self._ring = np.zeros(self._ring_n, dtype=np.float32)
        self._lock = threading.Lock()
        self._is_snoring = False
        self._last_loud_at = 0.0
        self._last_audio_t: float = 0.0
        self._latest = {
            'energy_db': -120.0,
            'band_ratio': 0.0,
            'snoring': False,
        }
        self.status = 'idle'        # idle | listening | error
        self.error = ''

    # ── public ──

    def is_snoring(self) -> bool:
        return self._is_snoring

    def metrics(self) -> dict:
        m = dict(self._latest)
        m['status'] = self.status
        if self.error:
            m['error'] = self.error
        m['stream_open'] = self._stream is not None
        m['last_audio_age_s'] = (
            (time.time() - self._last_audio_t)
            if getattr(self, '_last_audio_t', 0.0) > 0 else None)
        return m

    def set_thresholds(self, energy_db: float = None,
                       band_ratio_min: float = None):
        if energy_db is not None:
            self.energy_db = float(energy_db)
        if band_ratio_min is not None:
            self.band_ratio_min = float(band_ratio_min)

    def snapshot(self, seconds: float) -> np.ndarray:
        """Return the most recent `seconds` of audio (float32, mono)."""
        n = min(self._ring_n, max(1, int(seconds * self.sr)))
        with self._lock:
            return self._ring[-n:].copy()

    def _open_stream(self, device):
        self._stream = sd.InputStream(
            samplerate=self.sr,
            channels=1,
            dtype='float32',
            blocksize=self.frame_n,
            device=device,
            callback=self._on_audio,
        )
        self._stream.start()

    def start(self):
        if self._stream is not None:
            return
        # Three-stage fallback: user's selected device → reinit PortAudio then
        # retry (device indices diverge between main process and our
        # enumeration subprocess after BT mics join) → OS default device.
        last_err = None
        for attempt, (dev, reinit) in enumerate([
            (self.device, False),
            (self.device, True),
            (None,        False),
        ]):
            if reinit:
                try:
                    if self._stream is not None:
                        self._stream.close()
                except Exception:
                    pass
                self._stream = None
                try:
                    sd._terminate(); sd._initialize()
                except Exception:
                    pass
            try:
                self._open_stream(dev)
                last_err = None
                if dev is None and self.device is not None:
                    self.device = None
                break
            except Exception as e:
                self._stream = None
                last_err = e
        if last_err is not None:
            self.status = 'error'
            self.error = str(last_err)
            return
        self.status = 'listening'
        self.error = ''

    def set_device(self, device: Optional[int]):
        """Change input device and (re)start capture.
        Always restart — even if the previous start failed (_stream is None)
        the user is trying to pick a *different* device, so give it a shot.
        """
        self.device = device
        try:
            self.stop()
        except Exception:
            pass
        self.error = ''
        self.status = 'idle'
        self.start()

    def stop(self):
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        self.status = 'idle'
        self._is_snoring = False

    # ── audio thread ──

    def _on_audio(self, indata, frames, time_info, status):
        x = indata[:, 0] if indata.ndim == 2 else indata
        n = len(x)
        if n == 0:
            return
        self._last_audio_t = time.time()
        with self._lock:
            # Feature window (short, for FFT)
            if n >= self.window_n:
                self._buf = x[-self.window_n:].astype(np.float32, copy=True)
            else:
                self._buf = np.roll(self._buf, -n)
                self._buf[-n:] = x
            buf = self._buf.copy()
            # Long ring buffer (for post-trigger audio snapshot)
            if n >= self._ring_n:
                self._ring = x[-self._ring_n:].astype(np.float32, copy=True)
            else:
                self._ring = np.roll(self._ring, -n)
                self._ring[-n:] = x

        rms = float(np.sqrt(np.mean(buf * buf)) + 1e-12)
        energy_db = float(20.0 * np.log10(rms))

        win = buf * np.hanning(len(buf)).astype(np.float32)
        spec = np.abs(np.fft.rfft(win))
        freqs = np.fft.rfftfreq(len(buf), 1.0 / self.sr)
        total = float(np.sum(spec) + 1e-12)
        lo, hi = self.band_hz
        mask = (freqs >= lo) & (freqs <= hi)
        ratio = float(np.sum(spec[mask]) / total)

        loud = energy_db > self.energy_db
        snore_like = ratio > self.band_ratio_min
        now = time.time()
        if loud and snore_like:
            self._is_snoring = True
            self._last_loud_at = now
        elif self._is_snoring and (now - self._last_loud_at) > self.hangover_s:
            self._is_snoring = False

        self._latest = {
            'energy_db': round(energy_db, 1),
            'band_ratio': round(ratio, 2),
            'snoring': bool(self._is_snoring),
        }
        if self.bus is not None:
            try:
                self.bus.emit('snore.state', dict(self._latest), src='mic')
            except Exception:
                pass
