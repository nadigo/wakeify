"""
Configuration models for alarm playback system
"""

from pydantic import BaseModel, Field
from typing import List, Optional, Literal, Dict, Any
import os
import logging
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Use BASE_DIR for all file paths
BASE_DIR = os.getenv("BASE_DIR", "/data/wakeify")
DATA_DIR = os.path.join(BASE_DIR, "data")


class SpotifyAuth(BaseModel):
    """Spotify API authentication configuration"""
    client_id: str = Field(..., description="Spotify app client ID")
    client_secret: str = Field(..., description="Spotify app client secret")
    refresh_token: str = Field(..., description="Refresh token for Premium account")
    redirect_uri: str = Field(default="https://localhost/callback", description="OAuth redirect URI")
    access_token_cache: str = Field(default_factory=lambda: os.path.join(DATA_DIR, "token.json"), description="Local cache file for access token")

    @classmethod
    def from_env(cls) -> "SpotifyAuth":
        """Create SpotifyAuth from environment variables and token file"""
        # Try to get refresh token from environment first
        refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN", "")
        
        # If not in environment, try to load from token.json file
        if not refresh_token:
            try:
                import json
                token_file = os.path.join(DATA_DIR, "token.json")
                if os.path.exists(token_file):
                    with open(token_file, 'r') as f:
                        token_data = json.load(f)
                        refresh_token = token_data.get("refresh_token", "")
            except Exception as e:
                logger.warning(f"Could not load refresh token from file: {e}")
        
        return cls(
            client_id=os.getenv("SPOTIFY_CLIENT_ID", ""),
            client_secret=os.getenv("SPOTIFY_CLIENT_SECRET", ""),
            refresh_token=refresh_token,
            redirect_uri=os.getenv("SPOTIFY_REDIRECT_URI", "https://localhost/callback")
        )


class DeviceProfile(BaseModel):
    """Configuration for a target Spotify Connect device (generic for all devices)"""
    name: str = Field(..., description="Friendly name (used for display and matching)")
    instance_name: Optional[str] = Field(None, description="Instance name from mDNS discovery (for exact matching)")
    spotify_device_names: List[str] = Field(default_factory=list, description="All known Spotify device names for this device (exact matching only)")
    ip: Optional[str] = Field(None, description="Static IP if available")
    cpath: Optional[str] = Field(None, description="Zeroconf TXT.CPath if known")
    port: Optional[int] = Field(None, description="Zeroconf SRV port if known")
    volume_preset: int = Field(default=35, ge=0, le=100, description="Volume level 0-100")
    auth_mode_for_adduser: str = Field(default="access_token", description="Authentication mode for addUser")
    max_wake_wait_s: int = Field(default=22, ge=1, le=60, description="Maximum time to wait for device wake")
    
    def get_all_matching_names(self) -> List[str]:
        """Get all names that should match this device (exact matching only)"""
        names = [self.name]
        if self.instance_name:
            names.append(self.instance_name)
        names.extend(self.spotify_device_names)
        # Remove duplicates while preserving order
        seen = set()
        unique_names = []
        for name in names:
            name_lower = name.lower().strip() if name else ""
            if name_lower and name_lower not in seen:
                seen.add(name_lower)
                unique_names.append(name)
        return unique_names


class Timings(BaseModel):
    """Timing configuration for alarm orchestration"""
    prewarm_s: int = Field(default=60, ge=10, le=300, description="Pre-warm time in seconds")
    poll_fast_period_s: float = Field(default=5.0, ge=1.0, le=20.0, description="Fast polling period")
    total_poll_deadline_s: int = Field(default=20, ge=5, le=60, description="Total polling deadline")
    poll_deadline_extension_s: int = Field(default=15, ge=0, le=60, description="Poll deadline extension after addUser (seconds)")
    debounce_after_seen_s: float = Field(default=0.6, ge=0.1, le=5.0, description="Debounce after device seen")
    retry_404_delay_s: float = Field(default=0.7, ge=0.1, le=5.0, description="Delay before 404 retry")
    failover_fire_after_s: float = Field(default=2.0, ge=0.5, le=10.0, description="Failover timeout")
    adduser_wait_after_s: float = Field(default=5.0, ge=0, le=30.0, description="Wait time after addUser before checking devices")
    mdns_discovery_timeout_s: float = Field(default=1.5, ge=0.5, le=10.0, description="mDNS discovery timeout")
    getinfo_timeout_s: float = Field(default=1.5, ge=0.5, le=10.0, description="getInfo request timeout")
    adduser_timeout_s: float = Field(default=2.5, ge=0.5, le=10.0, description="addUser request timeout")
    device_info_timeout_s: float = Field(default=2.0, ge=0.5, le=10.0, description="getDeviceInfo request timeout")
    verify_device_ready_timeout_s: float = Field(default=0.5, ge=0.1, le=5.0, description="verifyDeviceReady timeout")
    confirmation_sleep_s: float = Field(default=0.2, ge=0.1, le=1.0, description="Sleep time in confirmation loop")
    poll_sleep_fast_s: float = Field(default=0.5, ge=0.1, le=2.0, description="Sleep time during fast polling")
    poll_sleep_slow_s: float = Field(default=1.0, ge=0.1, le=5.0, description="Sleep time during slow polling")


class PlaybackMetrics(BaseModel):
    """Metrics for alarm playback execution"""
    branch: str = Field(default="unknown", description="Execution branch taken")
    total_duration_ms: int = Field(default=0, description="Total execution time in milliseconds")
    discovered_ms: int = Field(default=0, description="Time to discover device")
    getinfo_ms: int = Field(default=0, description="Time to get device info")
    adduser_ms: int = Field(default=0, description="Time to add user")
    cloud_visible_ms: int = Field(default=0, description="Time until device visible in cloud")
    play_ms: int = Field(default=0, description="Time to start playback")
    errors: List[Dict[str, Any]] = Field(default_factory=list, description="List of errors encountered")


class AlarmPlaybackConfig(BaseModel):
    """Main configuration for alarm playback system"""
    spotify: SpotifyAuth = Field(default_factory=SpotifyAuth.from_env, description="Spotify authentication")
    targets: List[DeviceProfile] = Field(default_factory=list, description="Target device profiles")
    timings: Timings = Field(default_factory=Timings, description="Timing configuration")
    context_uri: str = Field(
        default_factory=lambda: os.getenv("ALARM_CONTEXT_URI", ""),
        description="Default playlist/album/artist URI for alarms"
    )
    shuffle: bool = Field(default=False, description="Whether to enable shuffle mode")
    log_level: str = Field(default="INFO", description="Logging level")
    log_format: str = Field(default="json", description="Log format (json|text)")

    @classmethod
    def from_env(cls) -> "AlarmPlaybackConfig":
        """Create configuration from environment variables"""
        return cls(
            spotify=SpotifyAuth.from_env(),
            context_uri=os.getenv("ALARM_CONTEXT_URI", ""),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            log_format=os.getenv("LOG_FORMAT", "json")
        )

