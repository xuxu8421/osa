"""
YamnetSnoreDetector — drop-in replacement for MicSnoreDetector using Google's
YAMNet (AudioSet-pretrained, 521 classes, including "Snoring" at idx 38).

Why: the heuristic (RMS + 80–500 Hz band ratio) is too coarse to tell apart
snoring, speech, loud breathing, TV and HVAC.  YAMNet handles all of those
zero-shot.

Interface compatibility with MicSnoreDetector (deliberate, so OsaRuntime can
swap backends without touching controller code):
  * start() / stop()
  * set_device(device)
  * set_thresholds(energy_db=None, band_ratio_min=None)  — kept as alias to
    snore_prob_thresh so the existing UI sliders still do something useful
  * snapshot(seconds) -> np.ndarray (mono float32)
  * is_snoring() -> bool
  * metrics() -> dict  (adds YAMNet-specific fields)

Threading model:
  * sounddevice callback fills a ring buffer (lock-guarded).
  * A worker thread pulls the latest 0.96 s every `infer_period_s` and runs
    YAMNet.  Inference runs off the audio callback so the capture stream
    never under-runs.
  * Public accessors read atomically from cached dicts.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import sounddevice as sd


# YAMNet native input: 16 kHz mono, 0.96 s window (15600 samples)
YAMNET_SR = 16000
YAMNET_WINDOW_N = int(YAMNET_SR * 0.96)
CACHE_DIR = Path(os.path.expanduser('~')) / '.cache' / 'osa_yamnet'


# Subset of AudioSet classes we care about for snore / respiratory context.
# Indices follow the YAMNet class map (AudioSet ontology ordering).
RELEVANT_CLASSES = {
    'snoring': 38,
    'breathing': 36,
    'snort': 37,
    'gasp': 40,
    'speech': 0,
    'cough': 42,
    'silence': 494,
}


def _download_class_map() -> Path:
    """Fetch the CSV that maps class index → human name (for debugging)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = CACHE_DIR / 'yamnet_class_map.csv'
    if fp.exists():
        return fp
    url = ('https://raw.githubusercontent.com/tensorflow/models/master/'
           'research/audioset/yamnet/yamnet_class_map.csv')
    urllib.request.urlretrieve(url, str(fp))
    return fp


class YamnetSnoreDetector:
    """Snore detector backed by YAMNet."""

    name = 'yamnet'

    def __init__(self,
                 bus=None,
                 sample_rate: int = YAMNET_SR,
                 infer_period_s: float = 0.25,
                 snore_prob_thresh: float = 0.3,
                 hangover_s: float = 2.0,
                 ring_s: float = 30.0,
                 device: Optional[int] = None):
        if sample_rate != YAMNET_SR:
            # YAMNet is hard-wired to 16 kHz; stick with the model's native
            # rate so we don't have to resample every frame.
            sample_rate = YAMNET_SR
        self.bus = bus
        self.sr = sample_rate
        self.device = device
        self.infer_period_s = infer_period_s
        self.snore_prob_thresh = snore_prob_thresh
        self.hangover_s = hangover_s

        self._ring_n = int(sample_rate * ring_s)
        self._ring = np.zeros(self._ring_n, dtype=np.float32)
        self._ring_lock = threading.Lock()

        self._stream: Optional[sd.InputStream] = None
        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._model = None
        self._class_names: list[str] = []
        self._is_snoring = False
        self._last_loud_at = 0.0
        self._latest = {
            'backend': 'yamnet',
            'snoring_prob': 0.0,
            'breathing_prob': 0.0,
            'speech_prob': 0.0,
            'top_class': '',
            'top_prob': 0.0,
            'energy_db': -120.0,  # still handy for the UI
            'snoring': False,
        }
        self.status = 'idle'      # idle | loading | listening | error
        self.error = ''

    # ── model loading (lazy; called from worker thread) ──

    def _ensure_model(self) -> bool:
        if self._model is not None:
            return True
        try:
            # Import here so the module can be loaded even when TF is missing
            # (we fall back to the heuristic detector in that case).
            import tensorflow_hub as hub          # noqa: F401
            import tensorflow as tf               # noqa: F401
            os.environ.setdefault('TF_CPP_MIN_LOG_LEVEL', '2')
            self.status = 'loading'
            self._model = hub.load('https://tfhub.dev/google/yamnet/1')
            # Read class map the model ships with.
            class_map_path = self._model.class_map_path().numpy().decode()
            with open(class_map_path, 'r') as f:
                next(f)  # header
                self._class_names = [row.split(',')[2].strip()
                                     for row in f]
            return True
        except Exception as e:
            self.status = 'error'
            self.error = f'load yamnet: {e}'
            self._model = None
            return False

    # ── public API ──

    def is_snoring(self) -> bool:
        return self._is_snoring

    def metrics(self) -> dict:
        m = dict(self._latest)
        m['status'] = self.status
        if self.error:
            m['error'] = self.error
        return m

    def set_thresholds(self, energy_db: float = None,
                       band_ratio_min: float = None,
                       snore_prob_thresh: float = None):
        """Kept signature-compatible with the heuristic detector.
        * `band_ratio_min` is reinterpreted as the YAMNet probability
          threshold (it's on 0..1, same range as our old slider min).
        * `snore_prob_thresh` is the modern name for the same knob.
        * `energy_db` is accepted and ignored (there is no energy gate
          here; YAMNet handles it).
        """
        if snore_prob_thresh is not None:
            self.snore_prob_thresh = float(snore_prob_thresh)
        elif band_ratio_min is not None:
            self.snore_prob_thresh = float(band_ratio_min)

    def snapshot(self, seconds: float) -> np.ndarray:
        n = min(self._ring_n, max(1, int(seconds * self.sr)))
        with self._ring_lock:
            return self._ring[-n:].copy()

    def start(self):
        if self._stream is not None:
            return
        self._stop_evt.clear()
        try:
            self._stream = sd.InputStream(
                samplerate=self.sr,
                channels=1,
                dtype='float32',
                blocksize=int(self.sr * 0.1),
                device=self.device,
                callback=self._on_audio,
            )
            self._stream.start()
        except Exception as e:
            self._stream = None
            self.status = 'error'
            self.error = f'open input: {e}'
            return
        self.status = 'loading'    # until worker actually loads the model
        self.error = ''
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

    def stop(self):
        self._stop_evt.set()
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop(); s.close()
            except Exception:
                pass
        if self._worker is not None:
            try:
                self._worker.join(timeout=1.5)
            except Exception:
                pass
            self._worker = None
        self.status = 'idle'
        self._is_snoring = False

    def set_device(self, device: Optional[int]):
        self.device = device
        was_running = self._stream is not None
        try:
            self.stop()
        except Exception:
            pass
        if was_running or True:
            # Mirror heuristic behaviour: a device switch always re-arms.
            self.error = ''
            self.status = 'idle'
            self.start()

    # ── audio callback: just push to ring ──

    def _on_audio(self, indata, frames, time_info, status):
        x = indata[:, 0] if indata.ndim == 2 else indata
        n = len(x)
        if n == 0:
            return
        with self._ring_lock:
            if n >= self._ring_n:
                self._ring = x[-self._ring_n:].astype(np.float32, copy=True)
            else:
                self._ring = np.roll(self._ring, -n)
                self._ring[-n:] = x

    # ── worker: run YAMNet every infer_period_s on last 0.96 s ──

    def _worker_loop(self):
        if not self._ensure_model():
            return
        self.status = 'listening'
        self.error = ''
        import tensorflow as tf  # imported lazily only here
        next_t = time.time()
        while not self._stop_evt.is_set():
            now = time.time()
            if now < next_t:
                time.sleep(min(0.05, next_t - now))
                continue
            next_t = now + self.infer_period_s

            with self._ring_lock:
                if len(self._ring) < YAMNET_WINDOW_N:
                    continue
                buf = self._ring[-YAMNET_WINDOW_N:].astype(np.float32,
                                                           copy=True)
            # YAMNet expects a 1-D float32 tensor in [-1, 1].
            try:
                scores, _, _ = self._model(tf.constant(buf))
                s = scores.numpy()  # (n_frames, 521), usually n_frames=2
            except Exception as e:
                self.status = 'error'
                self.error = f'infer: {e}'
                continue

            # Two improvements over plain mean-over-frames:
            # * Use MAX across YAMNet's 0.48 s sub-frames so a short snore
            #   that only occupies one half of the 0.96 s window still
            #   crosses the threshold (mean would dilute it).
            # * For the `top_class` debug display, keep the mean so it
            #   isn't dominated by single-frame spikes.
            peak = s.max(axis=0)
            mean = s.mean(axis=0)
            snore_p = float(peak[RELEVANT_CLASSES['snoring']])
            breath_p = float(peak[RELEVANT_CLASSES['breathing']])
            speech_p = float(peak[RELEVANT_CLASSES['speech']])
            top_i = int(np.argmax(mean))
            top_name = (self._class_names[top_i]
                        if 0 <= top_i < len(self._class_names) else str(top_i))

            rms = float(np.sqrt(np.mean(buf * buf)) + 1e-12)
            energy_db = 20.0 * np.log10(rms)

            if snore_p >= self.snore_prob_thresh:
                self._is_snoring = True
                self._last_loud_at = now
            elif self._is_snoring and (now - self._last_loud_at) > self.hangover_s:
                self._is_snoring = False

            self._latest = {
                'backend': 'yamnet',
                'snoring_prob': round(snore_p, 3),
                'breathing_prob': round(breath_p, 3),
                'speech_prob': round(speech_p, 3),
                'top_class': top_name,
                'top_prob': round(float(mean[top_i]), 3),
                'energy_db': round(energy_db, 1),
                'snoring': bool(self._is_snoring),
            }
            if self.bus is not None:
                try:
                    self.bus.emit('snore.state', dict(self._latest),
                                  src='yamnet')
                except Exception:
                    pass
