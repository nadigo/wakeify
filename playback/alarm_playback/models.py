"""
Data models and enums for alarm playback system
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, Any
import time


class State(Enum):
    """Device state enumeration"""
    UNKNOWN = "UNKNOWN"
    DISCOVERED = "DISCOVERED"
    LOCAL_AWAKE = "LOCAL_AWAKE"
    LOGGED_IN = "LOGGED_IN"
    CLOUD_VISIBLE = "CLOUD_VISIBLE"
    STAGED = "STAGED"
    PLAYING = "PLAYING"
    FALLBACK_ACTIVE = "FALLBACK_ACTIVE"
    DEEP_SLEEP_SUSPECTED = "DEEP_SLEEP_SUSPECTED"


@dataclass
class DiscoveryResult:
    """Result from mDNS/DNS-SD discovery"""
    ip: Optional[str] = None
    port: Optional[int] = None
    cpath: Optional[str] = None
    instance_name: Optional[str] = None
    txt_records: Optional[Dict[str, str]] = None
    
    @property
    def is_complete(self) -> bool:
        """Check if discovery has all required fields"""
        return self.ip is not None and self.port is not None and self.cpath is not None


@dataclass
class CloudDevice:
    """Spotify Web API device representation"""
    id: str
    name: str
    is_active: bool
    volume_percent: Optional[int] = None
    device_type: Optional[str] = None
    is_private_session: bool = False
    is_restricted: bool = False
    
    @classmethod
    def from_spotify_dict(cls, device_dict: Dict[str, Any]) -> "CloudDevice":
        """Create CloudDevice from Spotify API response"""
        return cls(
            id=device_dict["id"],
            name=device_dict["name"],
            is_active=device_dict.get("is_active", False),
            volume_percent=device_dict.get("volume_percent"),
            device_type=device_dict.get("type"),
            is_private_session=device_dict.get("is_private_session", False),
            is_restricted=device_dict.get("is_restricted", False)
        )


@dataclass
class PhaseMetrics:
    """Timing metrics for alarm playback phases"""
    discovered_ms: Optional[int] = None
    getinfo_ms: Optional[int] = None
    adduser_ms: Optional[int] = None
    cloud_visible_ms: Optional[int] = None
    play_ms: Optional[int] = None
    branch: Optional[str] = None  # e.g. "primary", "failed:not_in_devices"
    errors: Optional[list] = None
    total_duration_ms: Optional[int] = None
    
    def __post_init__(self):
        """Initialize errors list if None"""
        if self.errors is None:
            self.errors = []
    
    def add_error(self, error: str, phase: str = None):
        """Add an error with optional phase context"""
        error_entry = {"error": error, "phase": phase, "timestamp": time.time()}
        self.errors.append(error_entry)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging"""
        return {
            "discovered_ms": self.discovered_ms,
            "getinfo_ms": self.getinfo_ms,
            "adduser_ms": self.adduser_ms,
            "cloud_visible_ms": self.cloud_visible_ms,
            "play_ms": self.play_ms,
            "branch": self.branch,
            "total_duration_ms": self.total_duration_ms,
            "error_count": len(self.errors),
            "errors": self.errors
        }


@dataclass
class CircuitBreakerState:
    """Circuit breaker state for device failure tracking"""
    device_name: str
    failure_count: int = 0
    last_failure_time: Optional[float] = None
    is_open: bool = False
    
    def record_failure(self):
        """Record a failure and update circuit breaker state"""
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        # Open circuit after 3 consecutive failures within 10 minutes
        if self.failure_count >= 3:
            self.is_open = True
    
    def record_success(self):
        """Record a success and reset circuit breaker"""
        self.failure_count = 0
        self.last_failure_time = None
        self.is_open = False
    
    def should_bypass_primary(self) -> bool:
        """Check if primary path should be bypassed due to circuit breaker"""
        if not self.is_open:
            return False
        
        # Reset circuit breaker after 10 minutes
        if (self.last_failure_time and 
            time.time() - self.last_failure_time > 600):
            self.is_open = False
            return False
        
        return True

