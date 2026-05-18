"""
Audio output abstraction.

`LocalAudioSink` plays via sounddevice / PortAudio to whatever output device
the user picked. The previous AirPods "single-earbud duplex" workaround
(stereo→mono fallback chain + before/after_play hooks for HFP↔A2DP flipping)
has been removed: the current architecture keeps mic input and intervention
output on physically separate devices, so the HFP profile collision that
required all that machinery no longer happens.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

import numpy as np
import sounddevice as sd


@dataclass
class PlaybackRequest:
    waveform: np.ndarray       # (N, 2) stereo float32 in [-1, 1]
    sample_rate: int
    meta: dict                 # free-form: strategy, direction, level_db, ...


class AudioSink(ABC):
    """Output channel for intervention sounds."""

    name: str = 'sink'

    @abstractmethod
    def play(self, req: PlaybackRequest) -> None:
        """Start playback; non-blocking. Returns immediately."""

    @abstractmethod
    def stop(self) -> None:
        """Stop any ongoing playback."""

    @property
    @abstractmethod
    def is_playing(self) -> bool: ...


class LocalAudioSink(AudioSink):
    """Plays via sounddevice on the configured output device.

    Implementation note: we maintain a *single, always-on* `OutputStream`
    driven by a callback. When nothing is queued the callback fills with
    zeros, which is what keeps Bluetooth (and especially BT LE Audio
    sleep buds like Ozlo) from putting the audio link to sleep. With
    the previous "open a fresh stream per playback" design, the first
    call after a few seconds of silence would lose ~300-800 ms of audio
    to the BT cold-start ramp, and our 0.5 s interventions landed
    entirely in that ramp — so the headset never heard them. Persistent
    stream + ringbuffer of pending audio fixes this for arbitrarily
    short stimuli.

    Bluetooth-output robustness: AirPods / BT earbuds routinely have
    their PortAudio device index reshuffled when macOS toggles HFP↔A2DP,
    when another app grabs the route, when the earbuds go to sleep
    and re-pair, etc. A stored numeric index becomes stale and opening
    an `OutputStream(device=stale_idx)` raises `Error querying device N`.
    To survive this without nagging the user, `_ensure_stream()` records
    the *name* of the device when selected, and on failure walks a short
    fallback ladder:
      1. retry with the cached numeric index
      2. re-resolve the same name in a fresh `query_devices()`
      3. terminate + reinit PortAudio (forces full enumeration) and
         re-resolve by name
      4. fall back to the OS default output (device=None)
    Whichever step succeeds becomes the new cached index.
    """

    name = 'local'

    # Default samplerate / channel layout when opening a warm stream
    # before any actual playback request has come in. Matches our
    # synth pipeline (44.1 kHz stereo). If a play() arrives with a
    # different format we close+reopen the stream.
    DEFAULT_SR = 44100
    DEFAULT_CH = 2

    # Tiny silence preroll before the actual stimulus, just in case
    # the BT link briefly desynchronizes. Stream is already warm so
    # this can be small — purely a margin-of-safety pad.
    PREROLL_S = 0.05

    def __init__(self, device: Optional[int] = None):
        self._lock = threading.RLock()
        self.device: Optional[int] = device
        self._device_name: Optional[str] = self._lookup_device_name(device)

        # Saved trigger audio mirror — the *actual* stimulus (no preroll).
        self.last_wave: Optional[np.ndarray] = None
        self.last_sample_rate: int = 0
        self.last_meta: dict = {}
        self.last_used_device: Optional[int] = None

        # Persistent stream + queued audio for callback to consume.
        self._stream: Optional[sd.OutputStream] = None
        self._stream_sr: Optional[int] = None
        self._stream_ch: Optional[int] = None

        # Pending audio (list of (N, channels) float32 arrays). The
        # callback drains the head; play() appends to the tail. Mutex
        # protected by _q_lock (separate from the configuration lock
        # to keep the callback latency low).
        self._q_lock = threading.Lock()
        self._queue: list[np.ndarray] = []
        self._q_head_pos = 0
        self._t_end = 0.0  # wall clock when current queued audio finishes

        # Eager warm-up: open a stream against the configured device
        # (or the OS default when no specific device is set) so the
        # BT link is already keep-alive by the time the user clicks
        # anything. Failure here is non-fatal — _ensure_stream_locked
        # will be retried on the first play().
        try:
            with self._lock:
                self._ensure_stream_locked(self.DEFAULT_SR,
                                           self.DEFAULT_CH)
        except Exception:
            pass

    # ── public API ──

    def set_device(self, device: Optional[int]):
        with self._lock:
            self.device = device
            self._device_name = self._lookup_device_name(device)
            # Tear down old stream so the next play (or warm-up below)
            # opens against the new device.
            self._close_stream_locked()
            # Eagerly open against the new device so it's already warm
            # by the time the user clicks "测试音". A failure here is
            # non-fatal — _ensure_stream will be tried again from play().
            try:
                self._ensure_stream_locked(self.DEFAULT_SR, self.DEFAULT_CH)
            except Exception:
                pass

    def play(self, req: PlaybackRequest):
        wave = np.asarray(req.waveform, dtype=np.float32)
        if wave.ndim == 1:
            wave = wave.reshape(-1, 1)
        wave = np.ascontiguousarray(wave)
        channels = wave.shape[1]
        sr = int(req.sample_rate)

        with self._lock:
            try:
                self.last_wave = wave.copy()
                self.last_sample_rate = sr
                self.last_meta = dict(req.meta or {})
            except Exception:
                pass

            # Make sure a stream with the right format is running.
            self._ensure_stream_locked(sr, channels)

            # Reset queue, prepend tiny silence pad, append the stimulus.
            n_pre = max(0, int(self.PREROLL_S * sr))
            with self._q_lock:
                self._queue = []
                self._q_head_pos = 0
                if n_pre > 0:
                    self._queue.append(
                        np.zeros((n_pre, channels), dtype=np.float32))
                self._queue.append(wave)
                total = sum(len(a) for a in self._queue)
                self._t_end = time.time() + total / sr + 0.05

    def stop(self):
        # Drop pending audio; leave stream alive so it stays warm.
        with self._q_lock:
            self._queue = []
            self._q_head_pos = 0
            self._t_end = 0.0

    @property
    def is_playing(self) -> bool:
        if time.time() >= self._t_end:
            return False
        with self._q_lock:
            return bool(self._queue)

    # ── stream management ──

    @staticmethod
    def _lookup_device_name(device: Optional[int]) -> Optional[str]:
        if device is None:
            return None
        try:
            info = sd.query_devices(device)
            return info.get('name')
        except Exception:
            return None

    @staticmethod
    def _resolve_output_by_name(name: Optional[str]) -> Optional[int]:
        """Find an output device whose name matches `name`. Returns
        the new index or None when not found / on error."""
        if not name:
            return None
        try:
            devs = sd.query_devices()
        except Exception:
            return None
        for idx, d in enumerate(devs):
            if (d.get('name') == name
                    and d.get('max_output_channels', 0) > 0):
                return idx
        return None

    def _audio_callback(self, outdata, frames, time_info, status):
        """Pulled by PortAudio; pulls audio from `_queue`, fills with
        zeros when nothing pending. Must be fast (no I/O, no logging
        on the happy path)."""
        if status:
            # Underruns / overflows: ignore, we'll just emit silence.
            pass
        out = outdata
        written = 0
        with self._q_lock:
            while written < frames and self._queue:
                head = self._queue[0]
                avail = len(head) - self._q_head_pos
                need = frames - written
                take = min(avail, need)
                out[written:written + take] = \
                    head[self._q_head_pos:self._q_head_pos + take]
                written += take
                self._q_head_pos += take
                if self._q_head_pos >= len(head):
                    self._queue.pop(0)
                    self._q_head_pos = 0
        if written < frames:
            out[written:].fill(0)

    def _close_stream_locked(self):
        s = self._stream
        if s is None:
            return
        try: s.stop()
        except Exception: pass
        try: s.close()
        except Exception: pass
        self._stream = None
        self._stream_sr = None
        self._stream_ch = None

    def _open_stream(self, dev: Optional[int],
                     sample_rate: int, channels: int) -> sd.OutputStream:
        s = sd.OutputStream(
            samplerate=sample_rate,
            channels=channels,
            device=dev,
            dtype='float32',
            callback=self._audio_callback,
            # Reasonable buffer size. Bigger = fewer underruns, more
            # latency between play() and audible sound. ~20 ms is
            # plenty given we don't have realtime constraints.
            blocksize=int(sample_rate * 0.02),
            latency='high',
        )
        s.start()
        return s

    def _ensure_stream_locked(self, sample_rate: int, channels: int):
        """Open a persistent OutputStream (or reuse the existing one if
        the format matches). Walks the fallback ladder if the cached
        device index is stale. Caller must hold self._lock."""
        if (self._stream is not None
                and self._stream.active
                and self._stream_sr == sample_rate
                and self._stream_ch == channels):
            return

        if self._stream is not None:
            self._close_stream_locked()

        attempts: list[tuple[str, Optional[int]]] = [
            ('selected', self.device),
        ]
        if self._device_name:
            resolved = self._resolve_output_by_name(self._device_name)
            if resolved is not None and resolved != self.device:
                attempts.append(('reresolve_by_name', resolved))
        attempts.append(('reinit', None))
        attempts.append(('os_default', None))

        last_err: Optional[BaseException] = None
        for label, dev in attempts:
            if label == 'reinit':
                try:
                    sd._terminate(); sd._initialize()
                except Exception:
                    pass
                if self._device_name:
                    dev = self._resolve_output_by_name(self._device_name)
                else:
                    dev = None
                if dev is None:
                    continue
            try:
                stream = self._open_stream(dev, sample_rate, channels)
                self._stream = stream
                self._stream_sr = sample_rate
                self._stream_ch = channels
                self.last_used_device = dev
                if dev is not None and dev != self.device:
                    self.device = dev
                return
            except Exception as e:
                last_err = e
                continue

        raise last_err if last_err else RuntimeError(
            'audio: no playback path worked')


class HeadsetAudioSink(AudioSink):
    """Stub — swap in when an earbud platform SDK becomes available."""

    name = 'headset'

    def __init__(self):
        self._playing = False

    def play(self, req: PlaybackRequest):
        raise NotImplementedError("耳机平台 SDK 尚未接入")

    def stop(self):
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing
