"""
OSA experiment pipeline: sensors → events → analysis → controller → actuator.
Runs parallel to `devices/` (raw BLE drivers) and `sounds/` (audio synthesis).
"""

from .events import Event, EventBus
from .sensors import (
    Sensor, SensorStatus,
    ChestBandSensor, OximeterSensor, HeadsetSensor, MockSensor,
)
from .audio import AudioSink, LocalAudioSink, HeadsetAudioSink
from .recorder import SessionRecorder, SessionMeta, new_session_id
from .posture import PostureAnalyzer, PostureSample, PostureChange, PostureHold
from .controller import ClosedLoopController, ControllerConfig
from .snore import MicSnoreDetector

try:  # yamnet backend is optional — requires tensorflow
    from .snore_yamnet import YamnetSnoreDetector
except Exception:  # pragma: no cover — tf missing or arm64 wheels broken
    YamnetSnoreDetector = None  # type: ignore

__all__ = [
    'Event', 'EventBus',
    'Sensor', 'SensorStatus',
    'ChestBandSensor', 'OximeterSensor', 'HeadsetSensor', 'MockSensor',
    'AudioSink', 'LocalAudioSink', 'HeadsetAudioSink',
    'SessionRecorder', 'SessionMeta', 'new_session_id',
    'PostureAnalyzer', 'PostureSample', 'PostureChange', 'PostureHold',
    'ClosedLoopController', 'ControllerConfig',
    'MicSnoreDetector', 'YamnetSnoreDetector',
]
