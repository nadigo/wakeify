"""
Unified configuration system for Wakeify
Merges existing environment variables with playback module configuration
"""

import os
import json
import time
import logging
from typing import List, Optional, Dict, Any
from pathlib import Path
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

# Import playback module configuration
from alarm_playback.config import (
    SpotifyAuth, DeviceProfile, AirPlayConfig, FallbackConfig, 
    Timings, AlarmPlaybackConfig
)

# Load environment variables
load_dotenv()

logger = logging.getLogger(__name__)

# Use BASE_DIR for all file paths
BASE_DIR = os.environ.get("BASE_DIR", "/data/wakeify")
DATA_DIR = os.path.join(BASE_DIR, "data")
DEVICES_FILE = os.path.join(DATA_DIR, "devices.json")
FALLBACK_CONFIG_FILE = os.path.join(DATA_DIR, "fallback_config.json")
CIRCUIT_BREAKERS_FILE = os.path.join(DATA_DIR, "circuit_breakers.json")
METRICS_FILE = os.path.join(DATA_DIR, "metrics.json")

# Ensure data directory exists
os.makedirs(DATA_DIR, exist_ok=True)


@dataclass
class AlarmSystemConfig:
    """Unified configuration for the alarm system"""
    
    # Core alarm settings
    spotify: SpotifyAuth
    targets: List[DeviceProfile]
    fallback: FallbackConfig
    timings: Timings
    context_uri: str
    log_level: str
    log_format: str
    
    # Legacy settings from main.py
    default_speaker: str
    default_volume: int
    default_shuffle: bool
    app_secret: str
    oauth_scopes: str
    use_web_api: bool
    
    # New settings
    prewarm_enabled: bool
    device_auto_discovery: bool
    
    @classmethod
    def from_env(cls) -> "AlarmSystemConfig":
        """Create configuration from environment variables"""
        
        # Spotify authentication
        spotify = SpotifyAuth.from_env()
        
        # Default device profiles (will be populated by device registry)
        targets = []
        
        # Fallback configuration
        fallback = FallbackConfig(
            spotifyd_device_name=os.environ.get("SPOTIFYD_DEVICE_NAME", "Alarm Fallback"),
            airplay=AirPlayConfig.from_env()
        )
        
        # Timing configuration
        timings = Timings(
            prewarm_s=int(os.environ.get("ALARM_PREWARM_S", "60")),
            poll_fast_period_s=float(os.environ.get("ALARM_POLL_FAST_S", "5.0")),
            total_poll_deadline_s=int(os.environ.get("ALARM_POLL_DEADLINE_S", "20")),
            poll_deadline_extension_s=int(os.environ.get("ALARM_POLL_DEADLINE_EXTENSION_S", "15")),
            debounce_after_seen_s=float(os.environ.get("ALARM_DEBOUNCE_S", "0.6")),
            retry_404_delay_s=float(os.environ.get("ALARM_RETRY_404_S", "0.7")),
            failover_fire_after_s=float(os.environ.get("ALARM_FAILOVER_S", "2.0")),
            adduser_wait_after_s=float(os.environ.get("ALARM_ADDUSER_WAIT_S", "5.0")),
            mdns_discovery_timeout_s=float(os.environ.get("ALARM_MDNS_TIMEOUT_S", "1.5")),
            getinfo_timeout_s=float(os.environ.get("ALARM_GETINFO_TIMEOUT_S", "1.5")),
            adduser_timeout_s=float(os.environ.get("ALARM_ADDUSER_TIMEOUT_S", "2.5")),
            device_info_timeout_s=float(os.environ.get("ALARM_DEVICE_INFO_TIMEOUT_S", "2.0")),
            verify_device_ready_timeout_s=float(os.environ.get("ALARM_VERIFY_DEVICE_TIMEOUT_S", "0.5")),
            confirmation_sleep_s=float(os.environ.get("ALARM_CONFIRMATION_SLEEP_S", "0.2")),
            poll_sleep_fast_s=float(os.environ.get("ALARM_POLL_SLEEP_FAST_S", "0.5")),
            poll_sleep_slow_s=float(os.environ.get("ALARM_POLL_SLEEP_SLOW_S", "1.0"))
        )
        
        # Core settings
        context_uri = os.environ.get("ALARM_CONTEXT_URI", "")
        log_level = os.environ.get("LOG_LEVEL", "INFO")
        log_format = os.environ.get("LOG_FORMAT", "json")
        
        # Legacy settings
        default_speaker = os.environ.get("DEFAULT_SPEAKER", "")
        default_volume = int(os.environ.get("DEFAULT_VOLUME", "50"))
        default_shuffle = os.environ.get("DEFAULT_SHUFFLE", "true").lower() == "true"
        app_secret = os.environ.get("APP_SECRET", "dev-secret")
        oauth_scopes = os.environ.get("OAUTH_SCOPES", 
            "user-read-playback-state user-modify-playback-state playlist-read-private playlist-read-collaborative")
        use_web_api = os.environ.get("USE_WEB_API", "true").lower() == "true"
        
        # New settings
        prewarm_enabled = os.environ.get("ALARM_PREWARM_ENABLED", "true").lower() == "true"
        device_auto_discovery = os.environ.get("DEVICE_AUTO_DISCOVERY", "true").lower() == "true"
        
        return cls(
            spotify=spotify,
            targets=targets,
            fallback=fallback,
            timings=timings,
            context_uri=context_uri,
            log_level=log_level,
            log_format=log_format,
            default_speaker=default_speaker,
            default_volume=default_volume,
            default_shuffle=default_shuffle,
            app_secret=app_secret,
            oauth_scopes=oauth_scopes,
            use_web_api=use_web_api,
            prewarm_enabled=prewarm_enabled,
            device_auto_discovery=device_auto_discovery
        )
    
    def to_playback_config(self) -> AlarmPlaybackConfig:
        """Convert to AlarmPlaybackConfig for playback module"""
        return AlarmPlaybackConfig(
            spotify=self.spotify,
            targets=self.targets,
            fallback=self.fallback,
            timings=self.timings,
            context_uri=self.context_uri,
            log_level=self.log_level,
            log_format=self.log_format
        )
    
    def save_device_profiles(self) -> None:
        """Save device profiles to file"""
        try:
            device_data = {
                "devices": [device.model_dump() for device in self.targets],
                "last_updated": str(time.time())
            }
            with open(DEVICES_FILE, 'w') as f:
                json.dump(device_data, f, indent=2)
            logger.info(f"Saved {len(self.targets)} device profiles to {DEVICES_FILE}")
        except Exception as e:
            logger.error(f"Failed to save device profiles: {e}")
    
    def load_device_profiles(self) -> None:
        """Load device profiles from file"""
        try:
            if not os.path.exists(DEVICES_FILE):
                logger.info("No device profiles file found, will auto-discover")
                return
            
            with open(DEVICES_FILE, 'r') as f:
                data = json.load(f)
            
            devices = data.get("devices", [])
            self.targets = [DeviceProfile(**device) for device in devices]
            logger.info(f"Loaded {len(self.targets)} device profiles from {DEVICES_FILE}")
            
        except Exception as e:
            logger.error(f"Failed to load device profiles: {e}")
            self.targets = []
    
    def get_device_profile(self, device_name: str) -> Optional[DeviceProfile]:
        """Get device profile by name"""
        for device in self.targets:
            if device.name == device_name:
                return device
        return None
    
    def add_or_update_device_profile(self, device: DeviceProfile) -> None:
        """Add or update a device profile"""
        # Remove existing profile with same name
        self.targets = [d for d in self.targets if d.name != device.name]
        # Add new profile
        self.targets.append(device)
        # Save to file
        self.save_device_profiles()
        logger.info(f"Added/updated device profile: {device.name}")


def load_alarm_config() -> AlarmSystemConfig:
    """Load alarm system configuration"""
    config = AlarmSystemConfig.from_env()
    
    # Load device profiles if auto-discovery is disabled
    if not config.device_auto_discovery:
        config.load_device_profiles()
    
    return config


def save_fallback_config(config: FallbackConfig) -> None:
    """Save fallback configuration to file"""
    try:
        with open(FALLBACK_CONFIG_FILE, 'w') as f:
            json.dump(asdict(config), f, indent=2)
        logger.info(f"Saved fallback configuration to {FALLBACK_CONFIG_FILE}")
    except Exception as e:
        logger.error(f"Failed to save fallback configuration: {e}")


def load_fallback_config() -> FallbackConfig:
    """Load fallback configuration from file"""
    try:
        if not os.path.exists(FALLBACK_CONFIG_FILE):
            return FallbackConfig()
        
        with open(FALLBACK_CONFIG_FILE, 'r') as f:
            data = json.load(f)
        
        return FallbackConfig(**data)
    except Exception as e:
        logger.error(f"Failed to load fallback configuration: {e}")
        return FallbackConfig()


def save_circuit_breakers(circuit_breakers: Dict[str, Dict[str, Any]]) -> None:
    """Save circuit breaker states to file"""
    try:
        with open(CIRCUIT_BREAKERS_FILE, 'w') as f:
            json.dump(circuit_breakers, f, indent=2)
        logger.info(f"Saved circuit breaker states to {CIRCUIT_BREAKERS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save circuit breaker states: {e}")


def load_circuit_breakers() -> Dict[str, Dict[str, Any]]:
    """Load circuit breaker states from file"""
    try:
        if not os.path.exists(CIRCUIT_BREAKERS_FILE):
            return {}
        
        with open(CIRCUIT_BREAKERS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load circuit breaker states: {e}")
        return {}


def save_metrics(metrics: List[Dict[str, Any]]) -> None:
    """Save execution metrics to file"""
    try:
        with open(METRICS_FILE, 'w') as f:
            json.dump(metrics, f, indent=2)
        logger.info(f"Saved {len(metrics)} execution metrics to {METRICS_FILE}")
    except Exception as e:
        logger.error(f"Failed to save metrics: {e}")


def load_metrics() -> List[Dict[str, Any]]:
    """Load execution metrics from file"""
    try:
        if not os.path.exists(METRICS_FILE):
            return []
        
        with open(METRICS_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load metrics: {e}")
        return []
