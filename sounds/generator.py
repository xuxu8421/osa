"""
Wave primitives for OSA intervention sound synthesis.

Provides building blocks: noise generators, tone, bandpass filter,
envelope shaping, and amplitude modulation.
"""

import numpy as np
from scipy.signal import butter, sosfilt


DEFAULT_SR = 44100


def white_noise(duration: float, sr: int = DEFAULT_SR) -> np.ndarray:
    n_samples = int(duration * sr)
    return np.random.randn(n_samples)


def pink_noise(duration: float, sr: int = DEFAULT_SR) -> np.ndarray:
    """Generate 1/f pink noise using the Voss-McCartney algorithm."""
    n_samples = int(duration * sr)
    n_rows = 16
    array = np.random.randn(n_rows, n_samples)
    # Progressively hold-and-repeat rows at power-of-2 intervals
    for i in range(1, n_rows):
        step = 2 ** i
        array[i] = np.repeat(array[i, ::step], step)[:n_samples]
    signal = array.sum(axis=0)
    signal /= np.max(np.abs(signal)) + 1e-12
    return signal


def tone(freq: float, duration: float, sr: int = DEFAULT_SR) -> np.ndarray:
    t = np.arange(int(duration * sr)) / sr
    return np.sin(2 * np.pi * freq * t)


def bandpass(signal: np.ndarray, low: float, high: float,
             sr: int = DEFAULT_SR, order: int = 4) -> np.ndarray:
    nyq = sr / 2.0
    # Enforce minimum 50 Hz gap to avoid degenerate filter
    if high - low < 50:
        high = low + 50
    low_n = max(low / nyq, 1e-6)
    high_n = min(high / nyq, 1.0 - 1e-6)
    if low_n >= high_n:
        return signal
    sos = butter(order, [low_n, high_n], btype='band', output='sos')
    return sosfilt(sos, signal)


def apply_envelope(signal: np.ndarray, attack: float = 0.0,
                   decay: float = 0.0, sr: int = DEFAULT_SR) -> np.ndarray:
    """Apply cosine-ramp fade-in (attack) and fade-out (decay) in seconds."""
    n = len(signal)
    env = np.ones(n)

    att_samples = min(int(attack * sr), n // 2)
    if att_samples > 0:
        env[:att_samples] = 0.5 * (1 - np.cos(np.pi * np.arange(att_samples) / att_samples))

    dec_samples = min(int(decay * sr), n // 2)
    if dec_samples > 0:
        env[-dec_samples:] = 0.5 * (1 - np.cos(np.pi * np.arange(dec_samples, 0, -1) / dec_samples))

    return signal * env


def hann_envelope(signal: np.ndarray) -> np.ndarray:
    """Apply a full Hann window as envelope — smooth rise and fall."""
    n = len(signal)
    window = np.hanning(n)
    return signal * window


def apply_am(signal: np.ndarray, mod_freq: float, mod_depth: float,
             sr: int = DEFAULT_SR) -> np.ndarray:
    """Amplitude modulation for roughness perception.

    mod_freq: modulation frequency in Hz (30-70 Hz typical for roughness)
    mod_depth: 0.0 (no modulation) to 1.0 (full modulation)
    """
    t = np.arange(len(signal)) / sr
    modulator = 1.0 - mod_depth * 0.5 * (1 - np.cos(2 * np.pi * mod_freq * t))
    return signal * modulator


def normalize(signal: np.ndarray) -> np.ndarray:
    peak = np.max(np.abs(signal))
    if peak < 1e-12:
        return signal
    return signal / peak


def db_to_linear(db: float) -> float:
    return 10 ** (db / 20.0)
