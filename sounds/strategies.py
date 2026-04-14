"""
Five candidate sound strategies for OSA intervention experiments.

Block A (posture redirection): P1, P2, P3 — spatialized left/right
Block B (micro-arousal):       L1, L2   — centered

Parameter ranges are taken directly from the experiment design document.
"""

from dataclasses import dataclass
from typing import Dict, Any

import numpy as np

from .generator import (
    DEFAULT_SR, pink_noise, bandpass, apply_envelope, hann_envelope,
    apply_am, normalize, db_to_linear,
)
from .spatializer import spatialize


@dataclass
class ParamSpec:
    label: str
    key: str
    min_val: float
    max_val: float
    default: float
    step: float = 0.01
    unit: str = ''
    group: str = 'general'


# -- Shared param fragments --

LEVEL_PARAM = ParamSpec('level_db', 'level_db', -40, 0, -15, 1, 'dB')

SPATIAL_PARAMS = [
    ParamSpec('ITD', 'itd_ms', 0, 0.7, 0.4, 0.01, 'ms', 'spatial'),
    ParamSpec('ILD', 'ild_db', 0, 15, 8, 0.5, 'dB', 'spatial'),
]

STRATEGY_REGISTRY: Dict[str, 'StrategyDef'] = {}


@dataclass
class StrategyDef:
    name: str
    key: str
    category: str
    description: str
    params: list
    has_direction: bool = False

    def __post_init__(self):
        STRATEGY_REGISTRY[self.key] = self


# ===================================================================
# P1 — Spatial Whisper Sweep
# Doc: 左/右轻气流声, 0.4-0.7s, 单脉冲, 低/中
# Keywords: 轻气流声、柔和扫过声、轻shh短声
# ===================================================================
P1_DEF = StrategyDef(
    name='P1 Spatial Whisper Sweep',
    key='P1',
    category='posture',
    description='轻气流声, 柔和扫过, 诱导离开仰卧',
    has_direction=True,
    params=[
        ParamSpec('duration', 'duration', 0.4, 0.7, 0.55, 0.01, 's'),
        ParamSpec('freq_low', 'band_low', 200, 2000, 400, 10, 'Hz'),
        ParamSpec('freq_high', 'band_high', 1000, 8000, 2500, 100, 'Hz'),
        LEVEL_PARAM,
        *SPATIAL_PARAMS,
    ],
)

# ===================================================================
# P2 — Spatial Double-Pulse Burst
# Doc: 0.12-0.18s x2, 间隔 0.25-0.4s, 双脉冲, 低/中
# Keywords: 双脉冲提示、双短促气流声、两连短提示
# ===================================================================
P2_DEF = StrategyDef(
    name='P2 Spatial Double-Pulse Burst',
    key='P2',
    category='posture',
    description='双脉冲气流声, 更强瞬时提示',
    has_direction=True,
    params=[
        ParamSpec('pulse_dur', 'pulse_dur', 0.12, 0.18, 0.15, 0.01, 's'),
        ParamSpec('gap', 'gap', 0.25, 0.40, 0.32, 0.01, 's'),
        ParamSpec('freq_low', 'band_low', 200, 2000, 400, 10, 'Hz'),
        ParamSpec('freq_high', 'band_high', 1000, 8000, 2500, 100, 'Hz'),
        LEVEL_PARAM,
        *SPATIAL_PARAMS,
    ],
)

# ===================================================================
# P3 — Unilateral Low-Frequency Rumble
# Doc: 单侧低频短声, 0.4-0.7s, 单脉冲, 偏单侧, 低/中
# Keywords: 低频闷声、低沉短声、低频脉冲
# ===================================================================
P3_DEF = StrategyDef(
    name='P3 Unilateral Low-Freq Rumble',
    key='P3',
    category='posture',
    description='单侧低频闷声, 低沉短声, 强方向感',
    has_direction=True,
    params=[
        ParamSpec('duration', 'duration', 0.4, 0.7, 0.5, 0.01, 's'),
        ParamSpec('freq_low', 'band_low', 40, 120, 80, 5, 'Hz'),
        ParamSpec('freq_high', 'band_high', 120, 300, 200, 10, 'Hz'),
        LEVEL_PARAM,
        ParamSpec('ITD', 'itd_ms', 0, 0.7, 0.6, 0.01, 'ms', 'spatial'),
        ParamSpec('ILD', 'ild_db', 0, 20, 12, 0.5, 'dB', 'spatial'),
    ],
)

# ===================================================================
# L1 — Brief Smooth Burst
# Doc: 居中平滑短声, 0.12-0.22s, 单脉冲, 居中, 低/中
# Keywords: 柔和短提示、平滑短声、轻噗声
# ===================================================================
L1_DEF = StrategyDef(
    name='L1 Brief Smooth Burst',
    key='L1',
    category='arousal',
    description='居中平滑短声, 最温和的打鼾截断',
    has_direction=False,
    params=[
        ParamSpec('duration', 'duration', 0.12, 0.22, 0.18, 0.01, 's'),
        ParamSpec('freq_low', 'band_low', 200, 2000, 300, 10, 'Hz'),
        ParamSpec('freq_high', 'band_high', 1000, 8000, 3000, 100, 'Hz'),
        LEVEL_PARAM,
    ],
)

# ===================================================================
# L2 — Brief Rough Burst
# Doc: 居中粗糙短声, 0.12-0.18s, 单脉冲, 居中, 低/中
# Keywords: 粗糙短声、颗粒感短提示、沙沙短声
# ===================================================================
L2_DEF = StrategyDef(
    name='L2 Brief Rough Burst',
    key='L2',
    category='arousal',
    description='居中粗糙短声, 颗粒感, 更易抓住注意',
    has_direction=False,
    params=[
        ParamSpec('duration', 'duration', 0.12, 0.18, 0.15, 0.01, 's'),
        ParamSpec('freq_low', 'band_low', 200, 2000, 300, 10, 'Hz'),
        ParamSpec('freq_high', 'band_high', 1000, 8000, 3000, 100, 'Hz'),
        ParamSpec('am_freq', 'am_freq', 30, 70, 45, 1, 'Hz', 'roughness'),
        ParamSpec('am_depth', 'am_depth', 0.0, 1.0, 0.6, 0.01, '', 'roughness'),
        LEVEL_PARAM,
    ],
)


# ===================================================================
# Synthesis
# ===================================================================

def synthesize(strategy_key: str, params: Dict[str, Any],
               direction: str = 'left',
               sr: int = DEFAULT_SR,
               seed: int = 42) -> np.ndarray:
    """Synthesize a stereo waveform.

    seed controls the random noise pattern — same seed + same duration
    = same underlying noise, so only filter/envelope/level changes are
    audible when tweaking parameters.
    """
    fn = _SYNTH_MAP.get(strategy_key)
    if fn is None:
        raise ValueError(f"Unknown strategy: {strategy_key}")
    return fn(params, direction, sr, seed)


def _seeded_pink(duration: float, sr: int, seed: int) -> np.ndarray:
    """Generate pink noise with a fixed seed for reproducibility."""
    rng_state = np.random.get_state()
    np.random.seed(seed)
    out = pink_noise(duration, sr)
    np.random.set_state(rng_state)
    return out


def _synth_p1(p: dict, direction: str, sr: int, seed: int) -> np.ndarray:
    dur = p.get('duration', 0.55)
    ramp = min(dur * 0.35, 0.10)
    mono = _seeded_pink(dur, sr, seed)
    mono = bandpass(mono, p.get('band_low', 500), p.get('band_high', 4000), sr)
    mono = apply_envelope(mono, attack=ramp, decay=ramp, sr=sr)
    mono = normalize(mono)
    stereo = spatialize(mono, direction,
                        p.get('itd_ms', 0.4), p.get('ild_db', 8), sr)
    return _apply_level(stereo, p.get('level_db', -6))


def _synth_p2(p: dict, direction: str, sr: int, seed: int) -> np.ndarray:
    pulse_dur = p.get('pulse_dur', 0.15)
    gap = p.get('gap', 0.32)
    ramp = min(pulse_dur * 0.35, 0.03)

    pulse1 = _seeded_pink(pulse_dur, sr, seed)
    pulse1 = bandpass(pulse1, p.get('band_low', 500), p.get('band_high', 4000), sr)
    pulse1 = apply_envelope(pulse1, attack=ramp, decay=ramp, sr=sr)

    silence = np.zeros(int(gap * sr))

    pulse2 = _seeded_pink(pulse_dur, sr, seed + 1)
    pulse2 = bandpass(pulse2, p.get('band_low', 500), p.get('band_high', 4000), sr)
    pulse2 = apply_envelope(pulse2, attack=ramp, decay=ramp, sr=sr)

    mono = np.concatenate([pulse1, silence, pulse2])
    mono = normalize(mono)
    stereo = spatialize(mono, direction,
                        p.get('itd_ms', 0.4), p.get('ild_db', 8), sr)
    return _apply_level(stereo, p.get('level_db', -6))


def _synth_p3(p: dict, direction: str, sr: int, seed: int) -> np.ndarray:
    dur = p.get('duration', 0.5)
    ramp = min(dur * 0.35, 0.10)
    mono = _seeded_pink(dur, sr, seed)
    mono = bandpass(mono, p.get('band_low', 80), p.get('band_high', 200), sr)
    mono = apply_envelope(mono, attack=ramp, decay=ramp, sr=sr)
    mono = normalize(mono)
    stereo = spatialize(mono, direction,
                        p.get('itd_ms', 0.6), p.get('ild_db', 12), sr)
    return _apply_level(stereo, p.get('level_db', -6))


def _synth_l1(p: dict, _direction: str, sr: int, seed: int) -> np.ndarray:
    dur = p.get('duration', 0.17)
    mono = _seeded_pink(dur, sr, seed)
    mono = bandpass(mono, p.get('band_low', 300), p.get('band_high', 6000), sr)
    mono = hann_envelope(mono)
    mono = normalize(mono)
    stereo = spatialize(mono, 'center')
    return _apply_level(stereo, p.get('level_db', -6))


def _synth_l2(p: dict, _direction: str, sr: int, seed: int) -> np.ndarray:
    dur = p.get('duration', 0.15)
    mono = _seeded_pink(dur, sr, seed)
    mono = bandpass(mono, p.get('band_low', 300), p.get('band_high', 6000), sr)
    mono = apply_am(mono, p.get('am_freq', 50), p.get('am_depth', 0.7), sr)
    mono = hann_envelope(mono)
    mono = normalize(mono)
    stereo = spatialize(mono, 'center')
    return _apply_level(stereo, p.get('level_db', -6))


def _apply_level(stereo: np.ndarray, level_db: float) -> np.ndarray:
    return stereo * db_to_linear(level_db)


_SYNTH_MAP = {
    'P1': _synth_p1,
    'P2': _synth_p2,
    'P3': _synth_p3,
    'L1': _synth_l1,
    'L2': _synth_l2,
}


def get_default_params(strategy_key: str) -> Dict[str, float]:
    sdef = STRATEGY_REGISTRY[strategy_key]
    return {ps.key: ps.default for ps in sdef.params}
