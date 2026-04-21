"""
PostureAnalyzer — turns raw chest-band accel + gesture byte into stable
posture events.

We use two signals:
  * `vitals.gesture` byte from the device itself (already smoothed internally)
  * `accel_x / y / z` arrays (25 samples/s, 10-bit) to re-classify when the
    gesture byte is ambiguous

Output topics:
  * posture.sample    every chest band packet, current instantaneous class
  * posture.change    edge transitions: {from, to, duration_of_prev, t}
  * posture.hold      while in a state, every `hold_tick_s` seconds, emits
                      {state, duration_so_far}

Posture classes: 'supine' | 'prone' | 'left' | 'right' | 'upright' | 'unknown'
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

import numpy as np

from .events import Event, EventBus


@dataclass
class PostureSample:
    t: float
    cls: str
    confidence: float = 1.0
    mean_xyz: tuple = (0.0, 0.0, 0.0)


@dataclass
class PostureChange:
    t: float
    prev: str
    cur: str
    prev_duration_s: float


@dataclass
class PostureHold:
    t: float
    cls: str
    duration_s: float


# Device `gesture` byte semantics (from 1A2.0 protocol doc):
#   0 = flat / supine
#   1 = upright / side
# Not enough resolution to distinguish supine vs prone, so we refine
# from accelerometer.
GESTURE_BYTE_MAP = {0: 'supine', 1: 'upright'}


class PostureAnalyzer:
    """State machine with debounce over N seconds."""

    def __init__(self,
                 bus: EventBus,
                 debounce_s: float = 3.0,
                 hold_tick_s: float = 5.0,
                 supine_axis_thresh: float = 0.70):
        """
        Args:
          debounce_s:        require this much continuous time in a new class
                             before declaring a transition.
          hold_tick_s:       periodically re-emit a 'posture.hold' event every
                             this many seconds while in the same class.
          supine_axis_thresh: |z|/||xyz|| above this ⇒ supine/prone axis
                             (value in [0, 1]; 0.7 ≈ tilt within ±45° of flat).
        """
        self.bus = bus
        self.debounce_s = debounce_s
        self.hold_tick_s = hold_tick_s
        self.supine_axis_thresh = supine_axis_thresh

        self._history: Deque[tuple[float, str]] = deque(maxlen=256)
        self._confirmed: Optional[str] = None
        self._confirmed_since: float = 0.0
        self._last_hold_emit: float = 0.0

        self._unsub = bus.subscribe('chestband.data', self._on_packet)

    # ── public accessors ──

    @property
    def current(self) -> Optional[str]:
        return self._confirmed

    @property
    def current_duration_s(self) -> float:
        if self._confirmed is None:
            return 0.0
        return time.time() - self._confirmed_since

    def close(self):
        try:
            self._unsub()
        except Exception:
            pass

    # ── core ──

    def _classify(self, dp) -> tuple[str, float, tuple]:
        """Return (class, confidence, mean_xyz) for the current packet."""
        ax = getattr(dp, 'accel_x', None)
        ay = getattr(dp, 'accel_y', None)
        az = getattr(dp, 'accel_z', None)

        if ax is None or ay is None or az is None or len(ax) == 0:
            g = getattr(getattr(dp, 'vitals', None), 'gesture', None)
            cls = GESTURE_BYTE_MAP.get(g, 'unknown')
            return cls, 0.4, (0.0, 0.0, 0.0)

        # 10-bit accel raw values have neutral bias ≈ 512. Subtract it to get
        # the actual gravity vector component on each axis. Otherwise every
        # normalized axis ends up around 0.6 and none dominates.
        NEUTRAL = 512.0
        mx = float(np.mean(ax)) - NEUTRAL
        my = float(np.mean(ay)) - NEUTRAL
        mz = float(np.mean(az)) - NEUTRAL
        mag = (mx * mx + my * my + mz * mz) ** 0.5 or 1.0
        nx, ny, nz = mx / mag, my / mag, mz / mag

        # Gesture byte as tiebreaker
        g = getattr(getattr(dp, 'vitals', None), 'gesture', None)

        # Axis-dominant logic. The chest band is worn with the label facing
        # up, electrodes on the skin. When the subject lies flat on their
        # back, gravity acts through the +Z axis (device normal).
        absx, absy, absz = abs(nx), abs(ny), abs(nz)

        if absz >= self.supine_axis_thresh:
            cls = 'supine' if nz > 0 else 'prone'
            conf = absz
        elif absy >= self.supine_axis_thresh:
            # Y axis runs across the chest → dominant when subject lies
            # on their side. Sign distinguishes left vs right side.
            cls = 'right' if ny > 0 else 'left'
            conf = absy
        elif absx >= self.supine_axis_thresh:
            # X axis runs along the body's long axis → dominant when
            # upright/sitting.
            cls = 'upright'
            conf = absx
        else:
            cls = 'unknown'
            conf = 0.3

        # If the on-device byte clearly says upright and our Z isn't strong,
        # let it override — it has hysteresis built in.
        if g == 1 and cls in ('supine', 'prone'):
            cls = 'upright'
            conf = max(conf, 0.5)

        return cls, float(conf), (mx, my, mz)

    def _on_packet(self, ev: Event):
        dp = ev.payload
        cls, conf, mean_xyz = self._classify(dp)
        now = ev.t

        self._history.append((now, cls))
        self.bus.emit('posture.sample',
                      PostureSample(now, cls, conf, mean_xyz),
                      src='analyzer.posture')

        # Debounce: only switch confirmed state once the new class has been
        # seen continuously for `debounce_s` seconds.
        cutoff = now - self.debounce_s
        recent = [c for t, c in self._history if t >= cutoff]
        if not recent:
            return
        recent_set = set(recent)
        if len(recent_set) == 1 and recent[0] != self._confirmed:
            prev = self._confirmed or 'unknown'
            prev_dur = (now - self._confirmed_since) if self._confirmed else 0.0
            self._confirmed = recent[0]
            self._confirmed_since = now - self.debounce_s
            self._last_hold_emit = now
            self.bus.emit('posture.change',
                          PostureChange(now, prev, self._confirmed, prev_dur),
                          src='analyzer.posture')
        elif self._confirmed and (now - self._last_hold_emit) >= self.hold_tick_s:
            self._last_hold_emit = now
            self.bus.emit('posture.hold',
                          PostureHold(now, self._confirmed,
                                      now - self._confirmed_since),
                          src='analyzer.posture')
