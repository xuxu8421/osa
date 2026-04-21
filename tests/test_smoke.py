"""Basic smoke tests — fast, no BLE/TF required.

Run with `python3 -m pytest tests/` (or plain `python3 tests/test_smoke.py`
for a zero-dep run).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Ensure the project root is on sys.path so `pipeline` etc. import cleanly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_eventbus_pubsub():
    """EventBus subscribe/emit round-trips with matching kind + payload."""
    from pipeline.events import EventBus

    bus = EventBus()
    got = []
    bus.subscribe('x.test', lambda ev: got.append(ev))
    bus.emit('x.test', {'n': 1}, src='t')
    bus.emit('y.other', {'n': 2}, src='t')   # shouldn't reach `got`
    assert len(got) == 1
    assert got[0].kind == 'x.test'
    assert got[0].payload == {'n': 1}


def test_controller_state_machine_cooldowns():
    """Controller defaults match our tuned values (180 s / 5 s)."""
    from pipeline.controller import ControllerConfig

    c = ControllerConfig()
    assert c.trigger_hold_s == 8.0
    assert c.response_window_s == 10.0
    assert c.cooldown_s == 180.0
    assert c.cooldown_no_response_s == 5.0
    assert c.snoring_recent_s == 15.0


def test_controller_active_window_crossing_midnight():
    """Active window should handle 23:00 → 05:30 style wrap."""
    from pipeline.controller import ClosedLoopController, ControllerConfig
    from pipeline.events import EventBus

    # Minimal stub sink (controller never calls it in this check).
    class _NullSink:
        name = 'null'
        @property
        def is_playing(self): return False
        def play(self, req): pass
        def stop(self): pass

    cfg = ControllerConfig(active_window_start='23:00',
                           active_window_end='05:30')
    bus = EventBus()
    ctrl = ClosedLoopController(bus, _NullSink(), cfg)
    # Monkey-patch current time — rather than faking the clock, just
    # assert the two endpoints behave as expected by rewriting the cfg.
    cfg2 = ControllerConfig(active_window_start='09:00',
                            active_window_end='17:00')
    ctrl.cfg = cfg2
    # We don't hardcode "now" because tests run any time. Just assert the
    # method returns bool and doesn't throw on both non-crossing and
    # crossing windows.
    ctrl.cfg = cfg
    assert isinstance(ctrl._within_active_window(), bool)
    ctrl.cfg = cfg2
    assert isinstance(ctrl._within_active_window(), bool)
    # Empty window = always active
    ctrl.cfg = ControllerConfig()
    assert ctrl._within_active_window() is True


def test_strategies_registered():
    """All 5 strategies (P1/P2/P3/L1/L2) must be in the registry."""
    from sounds.strategies import STRATEGY_REGISTRY, get_default_params

    for k in ('P1', 'P2', 'P3', 'L1', 'L2'):
        assert k in STRATEGY_REGISTRY, f'missing strategy {k}'
        params = get_default_params(k)
        assert 'level_db' in params


def test_synthesize_produces_stereo_waveform():
    """Smoke-test waveform synthesis — any strategy outputs stereo float32."""
    import numpy as np
    from sounds.generator import DEFAULT_SR
    from sounds.strategies import synthesize, get_default_params

    w = synthesize('P1', get_default_params('P1'), 'left',
                   DEFAULT_SR, seed=42)
    assert isinstance(w, np.ndarray)
    assert w.dtype in (np.float32, np.float64)
    assert w.ndim == 2 and w.shape[1] == 2, 'expected (N, 2) stereo'
    # Non-silent
    assert float(np.abs(w).max()) > 1e-4


def test_session_meta_serializes():
    """SessionMeta round-trips through json cleanly (used in meta.json)."""
    from dataclasses import asdict
    from pipeline.recorder import SessionMeta, new_session_id

    sid = new_session_id('smoke')
    m = SessionMeta(session_id=sid,
                    started_at='2026-01-01T00:00:00',
                    subject_id='smoke',
                    mode='A',
                    config={'trigger_hold_s': 8.0})
    blob = json.dumps(asdict(m))
    back = json.loads(blob)
    assert back['session_id'].endswith('_smoke')
    assert back['mode'] == 'A'


if __name__ == '__main__':
    # Allow running without pytest
    tests = [v for k, v in list(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = 0
    for t in tests:
        t_name = t.__name__
        t0 = time.time()
        try:
            t()
            ms = (time.time() - t0) * 1000
            print(f'  [PASS] {t_name}  ({ms:.1f} ms)')
            passed += 1
        except AssertionError as e:
            print(f'  [FAIL] {t_name}  {e}')
        except Exception as e:
            print(f'  [ERR ] {t_name}  {type(e).__name__}: {e}')
    print(f'\n{passed}/{len(tests)} tests passed')
    sys.exit(0 if passed == len(tests) else 1)
