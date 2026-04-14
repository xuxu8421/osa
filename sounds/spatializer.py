"""
Binaural spatialization via ITD (interaural time difference) and
ILD (interaural level difference) for headphone playback.
"""

import numpy as np
from .generator import DEFAULT_SR, db_to_linear


def spatialize(mono: np.ndarray, direction: str = 'center',
               itd_ms: float = 0.0, ild_db: float = 0.0,
               sr: int = DEFAULT_SR) -> np.ndarray:
    """Convert mono signal to stereo with spatial cues.

    Parameters
    ----------
    mono : 1-D array
    direction : 'left', 'right', or 'center'
    itd_ms : interaural time difference in milliseconds (applied to far ear)
    ild_db : interaural level difference in dB (attenuation on far ear)
    sr : sample rate

    Returns
    -------
    stereo : (N+delay, 2) array — columns are [left, right]
    """
    if direction == 'center' or itd_ms == 0 and ild_db == 0:
        return np.column_stack([mono, mono])

    delay_samples = int(round(itd_ms * sr / 1000.0))
    ild_gain = db_to_linear(-abs(ild_db))

    n = len(mono)
    total = n + abs(delay_samples)
    left = np.zeros(total)
    right = np.zeros(total)

    if direction == 'left':
        # Target on left: left ear gets original, right ear delayed + attenuated
        left[:n] = mono
        right[delay_samples:delay_samples + n] = mono * ild_gain
    elif direction == 'right':
        # Target on right: right ear gets original, left ear delayed + attenuated
        right[:n] = mono
        left[delay_samples:delay_samples + n] = mono * ild_gain
    else:
        left[:n] = mono
        right[:n] = mono

    return np.column_stack([left, right])
