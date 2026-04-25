"""
OSA experiment pipeline: sensors → events → analysis → controller → actuator.
Runs parallel to `devices/` (raw BLE drivers) and `sounds/` (audio synthesis).
"""

from .events import Event, EventBus
from .sensors import (
    Sensor, SensorStatus,
    ChestBandSensor, HeadsetSensor, MockSensor,
)
from .audio import AudioSink, LocalAudioSink, HeadsetAudioSink
from .recorder import SessionRecorder, SessionMeta, new_session_id
from .posture import PostureAnalyzer, PostureSample, PostureChange, PostureHold
from .controller import ClosedLoopController, ControllerConfig
from .snore_yamnet import YamnetSnoreDetector

__all__ = [
    'Event', 'EventBus',
    'Sensor', 'SensorStatus',
    'ChestBandSensor', 'HeadsetSensor', 'MockSensor',
    'AudioSink', 'LocalAudioSink', 'HeadsetAudioSink',
    'SessionRecorder', 'SessionMeta', 'new_session_id',
    'PostureAnalyzer', 'PostureSample', 'PostureChange', 'PostureHold',
    'ClosedLoopController', 'ControllerConfig',
    'YamnetSnoreDetector',
]
