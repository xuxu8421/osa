"""
Closed-loop Controller — Block A (posture redirection).

Per the original OSA intervention design (see 《OSA干预实验设计》):
  Block A trigger condition = **supine AND sustained snoring**
  Block B trigger condition = **snore bout formed** (handled separately).

Snoring detection is expected to come from the headset-mic audio channel.
Until that SDK / hardware path is wired in, we expose a `snoring_provider`
hook on the controller: real detector when available; otherwise fall back
to posture-only gating during先导自验. This keeps the original design
intact rather than silently redefining the trigger as "supine only".

State machine
─────────────
  idle       : nothing to do.
  armed      : subject is in a trigger posture (e.g. supine) AND (if
               required) snoring; a timer counts the hold duration. If it
               reaches `trigger_hold_s`, we fire.
  triggered  : we've decided to play; synthesize and dispatch the chosen
               strategy to the audio sink.
  playing    : audio is out; we wait for end-of-playback.
  observe    : observation window: did the subject move? (posture.change
               to non-supine within `response_window_s` → mark success).
  cooldown   : mandatory quiet period so we don't spam the subject.

Transitions publish `intervention.*` events so the recorder + UI stay in
sync. Thresholds/cooldowns/strategy pool are kept as a small Config struct
editable from the UI later.
"""

from __future__ import annotations

import datetime as _dt
import random
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import numpy as np

from .audio import AudioSink, PlaybackRequest
from .events import Event, EventBus

from sounds.strategies import (
    STRATEGY_REGISTRY, synthesize, get_default_params,
)
from sounds.generator import DEFAULT_SR


@dataclass
class ControllerConfig:
    # Which postures count as Block-A trigger posture (design default: supine)
    trigger_postures: tuple = ('supine',)
    # Original design: Block A fires only when supine AND sustained snoring
    # are both true. Snoring comes from the headset-mic channel, which is
    # not yet wired. Set to True once a real SnoringDetector is attached.
    require_snoring: bool = False
    # How long the (posture ∧ snoring-recent) condition must hold before firing
    trigger_hold_s: float = 8.0
    # Real snoring has 3-6 s gaps between snores (inhale vs exhale cycles).
    # Instead of requiring `is_snoring()` to be True every instant of the
    # hold period (which would never accumulate), we treat the snoring side
    # as satisfied if any positive detection arrived in the last
    # `snoring_recent_s` seconds. The detector's own hangover stays short
    # so the UI timeline stays truthful.
    snoring_recent_s: float = 15.0
    # Strategy pool — randomly chosen on trigger
    strategy_pool: tuple = ('P1', 'P2', 'P3')
    # Preferred playback direction; 'opposite' = play to opposite-of-current
    # posture (side direction only), 'random' = random L/R, 'left'/'right' =
    # fixed side
    direction_policy: str = 'opposite'
    # Output level
    level_db: float = -15.0
    # Observation window after playback to check for posture change
    response_window_s: float = 10.0
    # Cooldown AFTER a successful response (subject moved off supine).
    # We stop bothering them for a while to let them settle.
    cooldown_s: float = 180.0
    # Cooldown AFTER an unsuccessful intervention (observe timed out with
    # no posture change). Short by design — if the trigger conditions
    # are still true we want to retry soon, not wait a minute.
    cooldown_no_response_s: float = 5.0
    # Active window (wall-clock HH:MM strings). Empty strings = always
    # active. If set, interventions only fire when current local time is
    # inside [active_window_start, active_window_end]. Supports crossing
    # midnight (e.g. 23:00 → 06:00). Rationale: pilot subjects sleep for
    # many hours but we typically only want to intervene during a 2-4h
    # block (e.g. 01:00–04:00 deep-sleep phase) to avoid disrupting the
    # whole night.
    active_window_start: str = ''
    active_window_end: str = ''
    # Master enable
    enabled: bool = True


class ClosedLoopController:
    """Event-driven controller. No own thread; progress happens on publish."""

    def __init__(self, bus: EventBus, sink: AudioSink,
                 config: Optional[ControllerConfig] = None,
                 snoring_provider: Optional[Callable[[], bool]] = None):
        self.bus = bus
        self.sink = sink
        self.cfg = config or ControllerConfig()
        # Callable returning current snoring state. None => no detector
        # attached (headset mic not available). In that case we fall back to
        # posture-only gating if `require_snoring` is False (先导自验 path).
        self.snoring_provider = snoring_provider

        self.state = 'idle'
        self._armed_since: float = 0.0
        self._triggered_at: float = 0.0
        self._cooldown_until: float = 0.0
        self._observe_until: float = 0.0
        self._last_strategy: Optional[str] = None
        self._last_direction: Optional[str] = None
        self._last_posture_at_trigger: Optional[str] = None
        # Timestamp of the last positive snoring reading (from the detector).
        # Used by `_snoring_ok` to tolerate the gaps between snore events.
        self._last_snore_at: float = 0.0
        self._lock = threading.Lock()

        self._unsubs = [
            bus.subscribe('posture.change', self._on_posture_change),
            bus.subscribe('posture.sample', self._on_posture_sample),
        ]

    def close(self):
        for u in self._unsubs:
            try: u()
            except Exception: pass

    # ── external API (e.g., for UI to poke) ──

    def set_enabled(self, on: bool):
        with self._lock:
            self.cfg.enabled = on
            if not on:
                self._to_idle('disabled')

    def manual_trigger(self, strategy: Optional[str] = None,
                       direction: Optional[str] = None):
        """Ignore cooldown/hold and fire immediately (dev/test aid)."""
        self._fire(strategy=strategy, direction=direction, reason='manual')

    def status(self) -> dict:
        snore_age = (time.time() - self._last_snore_at
                     if self._last_snore_at > 0 else float('inf'))
        return {
            'state': self.state,
            'enabled': self.cfg.enabled,
            'cooldown_left': max(0.0, self._cooldown_until - time.time()),
            'armed_duration': max(0.0, time.time() - self._armed_since) if self.state == 'armed' else 0.0,
            'last_strategy': self._last_strategy,
            'last_direction': self._last_direction,
            'snoring_available': self.snoring_provider is not None,
            'require_snoring': self.cfg.require_snoring,
            'snoring_age_s': (snore_age if snore_age != float('inf')
                              else None),
            'snoring_recent_s': float(self.cfg.snoring_recent_s),
            'active_window_start': self.cfg.active_window_start,
            'active_window_end': self.cfg.active_window_end,
            'within_active_window': self._within_active_window(),
        }

    # ── trigger-condition gating ──

    def _within_active_window(self) -> bool:
        """True if current local time is inside [start, end]. Either
        boundary being empty disables the gate. Handles windows that
        cross midnight (e.g. 23:00 → 05:30).
        """
        start = (self.cfg.active_window_start or '').strip()
        end = (self.cfg.active_window_end or '').strip()
        if not start or not end:
            return True
        try:
            sh, sm = (int(x) for x in start.split(':')[:2])
            eh, em = (int(x) for x in end.split(':')[:2])
        except Exception:
            return True  # malformed → don't block
        now = _dt.datetime.now().time()
        s = _dt.time(hour=sh % 24, minute=sm % 60)
        e = _dt.time(hour=eh % 24, minute=em % 60)
        if s == e:
            return True
        if s < e:
            return s <= now < e
        # Crosses midnight
        return now >= s or now < e

    def _snoring_ok(self) -> bool:
        """True if the snoring side of (supine ∧ snoring) is satisfied.

        Block-A needs *sustained* snoring, but natural snoring has 3-6 s
        gaps between breaths — an instantaneous `is_snoring()` check
        would constantly drop back to False and reset the hold timer.
        So we treat the snoring side as satisfied if any positive reading
        came in within the last `snoring_recent_s` seconds.
        """
        if not self.cfg.require_snoring:
            return True
        if self.snoring_provider is None:
            return False
        try:
            if self.snoring_provider():
                self._last_snore_at = time.time()
        except Exception:
            pass
        gap = time.time() - self._last_snore_at
        return gap <= float(self.cfg.snoring_recent_s)

    # ── event handlers ──

    def _on_posture_change(self, ev: Event):
        pc = ev.payload
        now = ev.t

        if not self.cfg.enabled:
            return
        if not self._within_active_window():
            # Outside active hours: don't accumulate, don't fire.
            if self.state == 'armed':
                self._to_idle('outside_active_window')
            return

        # Observation window: if we're watching for a move-off event and see
        # the subject leaving the trigger posture, log success.
        if self.state == 'observe':
            was = self._last_posture_at_trigger
            moved_off = (was in self.cfg.trigger_postures and
                         pc.cur not in self.cfg.trigger_postures)
            self.bus.emit('intervention.response',
                          {
                              'reason': 'move_off' if moved_off else 'change',
                              'from': pc.prev, 'to': pc.cur,
                              'strategy': self._last_strategy,
                              'direction': self._last_direction,
                              'latency_s': now - self._triggered_at,
                              'success': bool(moved_off),
                          },
                          src='controller')
            if moved_off:
                self._enter_cooldown(now, reason='response_success',
                                     length_s=self.cfg.cooldown_s)
            return

        # Cooldown: ignore posture changes (but let them be recorded)
        if self.state == 'cooldown':
            return

        # Normal transitions: (posture ∧ snoring) gated
        in_trigger = (pc.cur in self.cfg.trigger_postures) and self._snoring_ok()
        if in_trigger:
            self._arm(now, posture=pc.cur)
        else:
            self._to_idle('left_trigger')

    def _on_posture_sample(self, ev: Event):
        if not self.cfg.enabled:
            return
        if not self._within_active_window():
            if self.state == 'armed':
                self._to_idle('outside_active_window')
            return
        if self.state == 'cooldown':
            return
        cls = getattr(ev.payload, 'cls', None)
        in_trigger = (cls in self.cfg.trigger_postures) and self._snoring_ok()
        # If we were idle and conditions just became true (e.g. snoring
        # arrived after posture was already supine), arm now.
        if self.state == 'idle':
            if in_trigger:
                self._arm(ev.t, posture=cls)
            return
        if self.state != 'armed':
            return
        # Keep armed honest: if conditions drop, go idle rather than fire.
        if not in_trigger:
            reason = ('left_trigger' if cls not in self.cfg.trigger_postures
                      else 'snoring_off')
            self._to_idle(reason)
            return
        held = ev.t - self._armed_since
        if held >= self.cfg.trigger_hold_s:
            self._fire(reason='auto_hold')

    # ── state transitions ──

    def _arm(self, now: float, posture: str):
        if self.state == 'armed':
            return
        with self._lock:
            self.state = 'armed'
            self._armed_since = now
            self._last_posture_at_trigger = posture
        self.bus.emit('intervention.state',
                      {'state': 'armed', 'posture': posture,
                       'trigger_hold_s': self.cfg.trigger_hold_s,
                       'require_snoring': self.cfg.require_snoring,
                       'snoring_available': self.snoring_provider is not None},
                      src='controller')

    def _to_idle(self, reason: str):
        if self.state == 'idle':
            return
        with self._lock:
            self.state = 'idle'
        self.bus.emit('intervention.state',
                      {'state': 'idle', 'reason': reason}, src='controller')

    def _fire(self, reason: str = 'auto',
              strategy: Optional[str] = None,
              direction: Optional[str] = None):
        if time.time() < self._cooldown_until:
            return
        # Pick strategy + direction
        if strategy is None:
            strategy = random.choice(self.cfg.strategy_pool)
        sdef = STRATEGY_REGISTRY.get(strategy)
        if sdef is None:
            return
        if direction is None:
            direction = self._pick_direction(sdef)

        with self._lock:
            self.state = 'triggered'
            self._triggered_at = time.time()
            self._last_strategy = strategy
            self._last_direction = direction

        self.bus.emit('intervention.triggered',
                      {
                          'strategy': strategy,
                          'direction': direction,
                          'reason': reason,
                          'posture': self._last_posture_at_trigger,
                          'level_db': self.cfg.level_db,
                      }, src='controller')

        # Synth + play (off the event thread, since synthesis can take a few
        # tens of ms)
        threading.Thread(
            target=self._play_then_observe,
            args=(strategy, direction),
            daemon=True,
        ).start()

    def _pick_direction(self, sdef) -> str:
        if not getattr(sdef, 'has_direction', True):
            return 'center'
        policy = self.cfg.direction_policy
        if policy == 'left' or policy == 'right':
            return policy
        if policy == 'random':
            return random.choice(['left', 'right'])
        # 'opposite': if subject is in one side, play to the other. For pure
        # supine we don't have side info → random.
        p = self._last_posture_at_trigger
        if p == 'left':  return 'right'
        if p == 'right': return 'left'
        return random.choice(['left', 'right'])

    def _play_then_observe(self, strategy: str, direction: str):
        try:
            params = get_default_params(strategy)
            params['level_db'] = self.cfg.level_db
            wave = synthesize(strategy, params, direction,
                              DEFAULT_SR, seed=random.randint(0, 2**31 - 1))
            self.sink.play(PlaybackRequest(wave, DEFAULT_SR, {
                'strategy': strategy, 'direction': direction,
                'level_db': self.cfg.level_db,
            }))
            self.state = 'playing'
            # Wait for playback to finish (approx)
            dur = len(wave) / DEFAULT_SR
            time.sleep(dur + 0.1)
            # Enter observation window
            self.state = 'observe'
            self._observe_until = time.time() + self.cfg.response_window_s
            self.bus.emit('intervention.state',
                          {'state': 'observe',
                           'window_s': self.cfg.response_window_s,
                           'strategy': strategy, 'direction': direction},
                          src='controller')
            # Wait out the window; if no posture.change came, record as
            # 'no_response' and enter cooldown.
            time.sleep(self.cfg.response_window_s)
            if self.state == 'observe':
                self.bus.emit('intervention.response', {
                    'reason': 'timeout', 'success': False,
                    'strategy': strategy, 'direction': direction,
                    'latency_s': self.cfg.response_window_s,
                }, src='controller')
                # Short cooldown on no-response so we can retry quickly.
                self._enter_cooldown(
                    time.time(), reason='no_response',
                    length_s=self.cfg.cooldown_no_response_s)
        except Exception as e:
            self.bus.emit('intervention.error',
                          {'error': str(e), 'strategy': strategy}, src='controller')
            self._enter_cooldown(
                time.time(), reason='error',
                length_s=self.cfg.cooldown_no_response_s)

    def _enter_cooldown(self, now: float, reason: str,
                        length_s: Optional[float] = None):
        dur = float(length_s if length_s is not None else self.cfg.cooldown_s)
        with self._lock:
            self.state = 'cooldown'
            self._cooldown_until = now + dur
        self.bus.emit('intervention.state',
                      {'state': 'cooldown', 'reason': reason,
                       'cooldown_s': dur},
                      src='controller')
        # Auto-exit cooldown
        def _exit():
            time.sleep(dur)
            if self.state == 'cooldown':
                self._to_idle('cooldown_done')
        threading.Thread(target=_exit, daemon=True).start()
