"""
Audio output abstraction. Current implementation plays to whatever audio
device `sounddevice` selects (usually system default / connected headphones).
Once the headset platform SDK is available, swap in HeadsetAudioSink; the
closed-loop controller code doesn't change.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

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
    """Plays via sounddevice. Fine for bench testing with wired headphones."""

    name = 'local'

    def __init__(self, device: Optional[int] = None):
        self._playing = False
        self._t_end = 0.0
        self._lock = threading.Lock()
        # sounddevice output device index/name; None → OS default.
        # Set explicitly (e.g. AirPods) so the mic staying on the built-in
        # input doesn't drag the output into HFP mono.
        self.device: Optional[int] = device
        # Last wave handed to play(); exposed for per-event snapshots.
        self.last_wave: Optional[np.ndarray] = None
        self.last_sample_rate: int = 0
        self.last_meta: dict = {}
        # Optional orchestration hooks for "single earbud duplex" mode.
        # before_play runs synchronously right before we call sd.play, and
        # after_play runs synchronously AFTER the estimated playback is
        # over (scheduled via Timer). The runtime uses this to close the
        # snore detector's InputStream so AirPods can flip HFP→A2DP, then
        # reopen it when playback is done.
        self.before_play: Optional[Callable[[PlaybackRequest], None]] = None
        self.after_play: Optional[Callable[[PlaybackRequest], None]] = None

    def set_device(self, device: Optional[int]):
        with self._lock:
            self.device = device

    def play(self, req: PlaybackRequest):
        # Run before-play hook OUTSIDE the lock so it can take time
        # (e.g. stop the mic and wait for AirPods to switch to A2DP).
        if self.before_play is not None:
            try:
                self.before_play(req)
            except Exception:
                pass
        with self._lock:
            try:
                sd.stop()
            except Exception:
                pass
            wave = req.waveform
            mono = wave.mean(axis=1) if wave.ndim == 2 else wave
            dev = self.device
            # Keep a copy of what we actually play so the runtime can save
            # it alongside the per-trigger ±30 s snapshot.
            try:
                self.last_wave = np.asarray(wave).copy()
                self.last_sample_rate = int(req.sample_rate)
                self.last_meta = dict(req.meta or {})
            except Exception:
                pass

            def _try(payload):
                sd.play(payload, req.sample_rate, device=dev)

            # Attempts, in order:
            #   1. stereo as-is
            #   2. mono mix (AirPods in HFP accepts only 1ch)
            #   3. terminate+reinit PortAudio, then stereo
            #   4. terminate+reinit PortAudio, then mono
            last = None
            for attempt, payload in enumerate(
                    [wave, mono, wave, mono]):
                if attempt == 2:
                    try:
                        sd._terminate()
                        sd._initialize()
                    except Exception:
                        pass
                try:
                    _try(payload)
                    last = None
                    break
                except sd.PortAudioError as e:
                    last = e
                except Exception as e:
                    last = e
            if last is not None:
                # All paths failed — surface the error to the caller instead
                # of pretending playback is happening.
                self._playing = False
                self._t_end = 0.0
                raise last
            self._playing = True
            dur = len(wave) / req.sample_rate
            self._t_end = time.time() + dur + 0.05

        # Schedule after-play hook when playback is (roughly) done. Run
        # on a daemon thread so play() stays non-blocking.
        if self.after_play is not None:
            delay = dur + 0.3

            def _fire():
                try:
                    self.after_play(req)
                except Exception:
                    pass

            threading.Timer(delay, _fire).start()

    def stop(self):
        with self._lock:
            sd.stop()
            self._playing = False

    @property
    def is_playing(self) -> bool:
        # Cheap: trust time estimate, fall through when it crosses end.
        if self._playing and time.time() > self._t_end:
            self._playing = False
        return self._playing


class HeadsetAudioSink(AudioSink):
    """Stub — swap in when the earbud platform SDK becomes available."""

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
