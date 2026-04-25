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
    """Plays via sounddevice on the configured output device."""

    name = 'local'

    def __init__(self, device: Optional[int] = None):
        self._playing = False
        self._t_end = 0.0
        self._lock = threading.Lock()
        self.device: Optional[int] = device
        # Last wave handed to play(); exposed for per-event snapshots.
        self.last_wave: Optional[np.ndarray] = None
        self.last_sample_rate: int = 0
        self.last_meta: dict = {}

    def set_device(self, device: Optional[int]):
        with self._lock:
            self.device = device

    def play(self, req: PlaybackRequest):
        with self._lock:
            try:
                sd.stop()
            except Exception:
                pass
            wave = req.waveform
            try:
                self.last_wave = np.asarray(wave).copy()
                self.last_sample_rate = int(req.sample_rate)
                self.last_meta = dict(req.meta or {})
            except Exception:
                pass
            sd.play(wave, req.sample_rate, device=self.device)
            self._playing = True
            dur = len(wave) / req.sample_rate
            self._t_end = time.time() + dur + 0.05

    def stop(self):
        with self._lock:
            sd.stop()
            self._playing = False

    @property
    def is_playing(self) -> bool:
        if self._playing and time.time() > self._t_end:
            self._playing = False
        return self._playing


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
