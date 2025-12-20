"""
Alarm Playback Module

Robustly wake Spotify Connect speakers and start playback at alarm time.
"""

__version__ = "2.0.0"
__author__ = "Wakeify"

from .orchestrator import AlarmPlaybackEngine, AlarmPlaybackFailure
from .config import AlarmPlaybackConfig
from .models import PhaseMetrics, State

__all__ = [
    "AlarmPlaybackEngine",
    "AlarmPlaybackFailure",
    "AlarmPlaybackConfig", 
    "PhaseMetrics",
    "State"
]

