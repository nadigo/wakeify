import os
import json
import logging
import threading
import time
import asyncio
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Monkey-patch input() to prevent interactive OAuth prompts in non-interactive environments
# This prevents spotipy from trying to read from stdin when it can't get a token
try:
    import builtins
    _original_input = getattr(builtins, 'input', None)
    
    def _non_interactive_input(prompt=''):
        """Raise EOFError immediately to prevent interactive OAuth prompts."""
        raise EOFError("Interactive input not available in non-interactive environment")
    
    if _original_input:
        # Replace input() with our non-interactive version
        builtins.input = _non_interactive_input
except Exception:
    # If monkey-patching fails, silently continue (shouldn't happen in normal operation)
    pass

# Import the comprehensive playback engine
from alarm_playback import AlarmPlaybackEngine
from alarm_playback.config import AlarmPlaybackConfig, DeviceProfile, PlaybackMetrics

# Import APScheduler for better scheduling
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Import structured logging utilities
from alarm_playback.logging_utils import setup_logging

# Configure structured logging based on environment variables
log_level = os.getenv("LOG_LEVEL", "WARNING")
log_format = os.getenv("LOG_FORMAT", "text")
setup_logging(log_level=log_level, log_format=log_format)

logger = logging.getLogger(__name__)

# Define lifespan function that will be used by FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan events."""
    # Startup
    global scheduler, alarm_config
    
    logger.info("Starting Wakeify")
    load_data()  # This function is defined later, but Python supports this
    
    # Initialize alarm configuration
    alarm_config = AlarmPlaybackConfig()
    
    # Initialize scheduler
    scheduler = BackgroundScheduler(timezone='America/New_York')
    scheduler.start()
    logger.info("Scheduler initialized")
    
    # Schedule all alarms
    schedule_alarms()  # This function is defined later, but Python supports this
    
    # Start background device registration (generic for all devices)
    registration_thread = threading.Thread(target=background_device_registration, daemon=True)
    registration_thread.start()
    logger.info("Background device registration monitoring started (all devices)")
    
    # Start background cache refresh to keep device list warm
    cache_refresh_thread = threading.Thread(target=background_cache_refresh, daemon=True)
    cache_refresh_thread.start()
    logger.info("Background cache refresh started")
    
    logger.info("Alarm system started")
    
    yield  # Application runs here
    
    # Shutdown
    global running
    running = False
    if scheduler:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
    save_data()
    logger.info("Alarm system stopped")

app = FastAPI(title="Wakeify - Wake up and smell the coffee", lifespan=lifespan)

# Configuration
BASE_DIR = Path(os.getenv("BASE_DIR", "/data/wakeify"))
# Determine app directory (where this script is located)
APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Static files (for favicon, etc.)
STATIC_DIR.mkdir(exist_ok=True)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

ALARMS_FILE = DATA_DIR / "alarms.json"
DEVICES_FILE = DATA_DIR / "devices.json"
TOKEN_FILE = DATA_DIR / "token.json"

# Spotify configuration
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "https://localhost/callback")

# Alarm configuration
DEFAULT_SPEAKER = os.getenv("DEFAULT_SPEAKER", "")
DEFAULT_VOLUME = int(os.getenv("DEFAULT_VOLUME", "30"))
DEFAULT_SHUFFLE = os.getenv("DEFAULT_SHUFFLE", "false").lower() == "true"

# Initialize Spotify OAuth with PKCE
# Let Spotipy manage the token file directly via cache_path
# This ensures automatic token saving, loading, and refreshing
sp_oauth = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope="user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private",
    cache_path=str(TOKEN_FILE),  # Spotipy will manage this file automatically
    open_browser=False,
    show_dialog=True
)

# Global variables
alarms: List[Dict[str, Any]] = []
devices: List[Dict[str, Any]] = []
spotify = None
spotify_lock = threading.Lock()  # Thread-safe access to spotify client
scheduler = None
running = True
alarm_config = None

# Performance: Device cache
device_cache = None
device_cache_timestamp = None
device_cache_ttl = 600  # Cache for 10 minutes (increased from 5 minutes)
# Friendly name cache: maps (ip, port) -> (name, timestamp)
friendly_name_cache = {}
friendly_name_cache_ttl = 900  # Cache friendly names for 15 minutes (device names rarely change)

# Performance: Playlist cache
playlist_cache = None
playlist_cache_timestamp = None
playlist_cache_ttl = 300  # Cache playlists for 5 minutes

def _validate_token_file() -> bool:
    """
    Validate that token file exists, is not empty, and contains valid JSON.
    
    Non-destructive validation - does not delete files, only checks validity.
    Spotipy manages the token file, so we shouldn't delete it.
    
    Returns:
        True if token file is valid, False otherwise
    """
    if not TOKEN_FILE.exists():
        return False
    
    # Check if file is empty
    if TOKEN_FILE.stat().st_size == 0:
        logger.warning(f"Token file {TOKEN_FILE} is empty (0 bytes). Spotipy will handle this.")
        return False
    
    # Check if file contains valid JSON
    try:
        with open(TOKEN_FILE, 'r') as f:
            content = f.read().strip()
            if not content:
                logger.warning(f"Token file {TOKEN_FILE} appears to be empty after reading.")
                return False
            json.loads(content)  # Validate JSON
    except json.JSONDecodeError as e:
        logger.warning(f"Token file {TOKEN_FILE} contains invalid JSON: {e}. Spotipy will handle this.")
        return False
    except Exception as e:
        logger.error(f"Error validating token file: {e}")
        return False
    
    return True

def _is_token_expired(token_info: Optional[Dict[str, Any]], margin_seconds: int = 300) -> bool:
    """
    Check if token is expired or will expire within margin_seconds.
    
    Args:
        token_info: Token info dict from spotipy
        margin_seconds: Refresh if token expires within this many seconds (default 5 minutes)
    
    Returns:
        True if token is expired or will expire soon, False otherwise
    """
    if not token_info:
        return True
    
    expires_at = token_info.get('expires_at')
    if not expires_at:
        # If no expiration info, assume it's valid (spotipy will handle refresh)
        return False
    
    current_time = time.time()
    time_until_expiry = expires_at - current_time
    
    if time_until_expiry <= 0:
        return True
    
    if time_until_expiry <= margin_seconds:
        return True
    
    return False

def _invalidate_spotify_caches():
    """Invalidate all Spotify-related caches when authentication fails."""
    global playlist_cache, playlist_cache_timestamp, device_cache, device_cache_timestamp
    
    playlist_cache = None
    playlist_cache_timestamp = None
    device_cache = None
    device_cache_timestamp = None

def _check_token_exists() -> bool:
    """
    Check if a token file exists and is valid JSON.
    
    Trust spotipy's auth_manager to handle token refresh automatically during API calls.
    This function only validates that we have a token file to work with.
    
    Returns:
        True if token file exists and is valid JSON, False otherwise
    """
    if not TOKEN_FILE.exists():
        return False
    
    return _validate_token_file()

def _retry_spotify_api(func, max_retries: int = 3, base_delay: float = 1.0):
    """
    Retry Spotify API call with exponential backoff.
    
    Args:
        func: Callable that makes the API call
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff
    
    Returns:
        Result of func() if successful
    
    Raises:
        Exception: If all retries fail
    """
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            return func()
        except EOFError as e:
            # Don't retry EOFError - it's an auth issue
            logger.error(f"EOFError in Spotify API call (attempt {attempt + 1}): {e}")
            raise
        except spotipy.SpotifyException as e:
            # Don't retry 401 (Unauthorized) - it's an auth issue
            if e.http_status == 401:
                logger.error(f"401 Unauthorized in Spotify API call (attempt {attempt + 1}): {e}")
                raise
            
            # Don't retry 4xx errors (client errors) except rate limiting
            if 400 <= e.http_status < 500 and e.http_status != 429:
                logger.error(f"Client error {e.http_status} in Spotify API call (attempt {attempt + 1}): {e}")
                raise
            
            last_exception = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Spotify API error (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
        except Exception as e:
            last_exception = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Error in Spotify API call (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {delay:.1f}s...")
                time.sleep(delay)
    
    # All retries failed
    logger.error(f"Spotify API call failed after {max_retries} attempts")
    raise last_exception

def get_spotify_client():
    """
    Get authenticated Spotify client (thread-safe).
    
    Trusts spotipy's auth_manager to handle token refresh automatically during API calls.
    When using Spotify(auth_manager=SpotifyOAuth(cache_path=...)), spotipy automatically:
    - Loads tokens from cache_path
    - Refreshes tokens when expired (during API calls)
    - Saves refreshed tokens back to cache_path
    
    We only need to check if a token file exists before creating the client.
    """
    global spotify
    
    # Use double-checked locking pattern for thread safety
    if spotify is None:
        with spotify_lock:
            # Check again after acquiring lock (another thread might have created it)
    if spotify is None:
        try:
                    # Check if token file exists and is valid JSON
                    # Trust spotipy's auth_manager to handle refresh automatically
                    if not _check_token_exists():
                        logger.warning("No token file found or token file is invalid. Spotify authentication required.")
                        return None
                    
                    # Try to get cached token to verify it exists (but don't refresh manually)
                    # This prevents spotipy from trying interactive OAuth if no token exists
                    try:
            token_info = sp_oauth.get_cached_token()
            if not token_info:
                            logger.warning("No valid token found in cache. Spotify authentication required.")
                            return None
                        
                        # Token expiration is handled automatically by auth_manager during API calls
                    except EOFError as e:
                        logger.error(f"EOFError getting cached token (interactive auth attempted in non-interactive environment): {e}")
                return None
            
            # Create Spotify client with auth_manager - spotipy handles token refresh automatically
                    # The monkey-patched input() function will prevent interactive OAuth prompts
                    try:
            spotify = spotipy.Spotify(auth_manager=sp_oauth)
                    except EOFError as e:
                        logger.error(f"EOFError creating Spotify client (interactive auth attempted in non-interactive environment): {e}")
                        return None
            
            # Set proper file permissions on token file if it exists
            if TOKEN_FILE.exists():
                try:
                    os.chmod(TOKEN_FILE, 0o600)  # rw------- for security
                        except Exception:
                            pass
            
                except EOFError as e:
                    logger.error(f"EOFError getting Spotify client (interactive auth attempted in non-interactive environment): {e}")
                    return None
        except Exception as e:
            logger.error(f"Error getting Spotify client: {e}")
            return None
    
    return spotify

def _reset_spotify_client():
    """Reset Spotify client and invalidate caches (thread-safe)."""
    global spotify
    
    with spotify_lock:
        spotify = None
        _invalidate_spotify_caches()

def get_spotify_auth_url():
    """Generate Spotify OAuth authorization URL."""
    try:
        return sp_oauth.get_authorize_url()
    except Exception as e:
        logger.error(f"Error generating Spotify auth URL: {e}")
        return None

def load_data():
    """Load alarms and devices from files."""
    global alarms, devices
    
    # Load alarms
    if ALARMS_FILE.exists():
        try:
            with open(ALARMS_FILE, 'r') as f:
                alarms = json.load(f)
        except Exception as e:
            logger.error(f"Error loading alarms: {e}")
            alarms = []
    
    # Load devices
    if DEVICES_FILE.exists():
        try:
            with open(DEVICES_FILE, 'r') as f:
                devices = json.load(f)
        except Exception as e:
            logger.error(f"Error loading devices: {e}")
            devices = []

def save_data():
    """Save alarms and devices to files."""
    try:
        with open(ALARMS_FILE, 'w') as f:
            json.dump(alarms, f, indent=2)
        with open(DEVICES_FILE, 'w') as f:
            json.dump(devices, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving data: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")

def get_playlist_name(playlist_uri: str) -> str:
    """Get playlist name from Spotify API."""
    try:
        sp = get_spotify_client()
        if not sp:
            return "Unknown Playlist"
        
        # Extract playlist ID from URI
        if ":" in playlist_uri:
            playlist_id = playlist_uri.split(":")[-1]
        else:
            playlist_id = playlist_uri
        
        playlist = sp.playlist(playlist_id)
        return playlist.get('name', 'Unknown Playlist')
    except Exception as e:
        logger.error(f"Error getting playlist name: {e}")
        return "Unknown Playlist"

def run_alarm(alarm: Dict[str, Any]) -> None:
    """Execute alarm using comprehensive playback engine with T-60s timeline."""
    try:
        playlist_name = alarm.get('playlist_name', 'Unknown Playlist')
        alarm_id = alarm.get('id')
        logger.info(f"Running alarm {alarm_id}: {playlist_name}")
        
        target_device_name = alarm.get("device_name", DEFAULT_SPEAKER)
        playlist_uri = alarm.get("playlist_uri")
        
        if not playlist_uri:
            logger.error(f"No playlist URI found in alarm {alarm_id}")
            return
        
        # Use comprehensive playback engine
        # Try to get existing device profile first, but don't create new ones via mDNS
        # Let the orchestrator handle Web API discovery first
        device_profile = None
        for profile in alarm_config.targets:
            if profile.name == target_device_name:
                device_profile = profile
                break
        
        # If no profile found, try to discover the device via mDNS and create profile with IP
        if not device_profile:
            try:
                from alarm_playback.discovery import discover_all_connect_devices
                # Discover all devices
                all_devices = discover_all_connect_devices(timeout_s=3.0)
                # Use DeviceRegistry to extract friendly names (from getInfo, then TXT, then instance_name)
                import sys
                sys.path.insert(0, str(APP_DIR))
                from device_registry import DeviceRegistry
                device_registry = DeviceRegistry(alarm_config) if alarm_config else None
                
                # Normalize target name for comparison (case-insensitive, strip whitespace)
                target_normalized = target_device_name.lower().strip()
                
                for dev in all_devices:
                    # Get friendly name from device (priority: getInfo > TXT records > instance_name)
                    # This is fully generic - no device-specific patterns
                    dev_name = None
                    dev_instance = dev.instance_name or ""
                    
                    try:
                        if device_registry:
                            # This will call getInfo to get remoteName/displayName, or fallback to instance_name
                            dev_name = device_registry._extract_friendly_name(dev)
                    except Exception:
                        pass
                    
                    if not dev_name:
                        dev_name = dev_instance
                    
                    # Exact matching: compare normalized names (friendly name and instance_name)
                    # This is generic - works for any device type
                    dev_name_normalized = dev_name.lower().strip() if dev_name else ""
                    dev_instance_normalized = dev_instance.lower().strip() if dev_instance else ""
                    
                    # Match if target matches either the friendly name or instance name (exact match only)
                    if (dev_name_normalized == target_normalized or 
                        dev_instance_normalized == target_normalized):
                        # Create device profile using names from mDNS/getInfo discovery (fully generic)
                        device_profile = DeviceProfile(
                            name=dev_name,  # Friendly name from getInfo (or instance_name fallback)
                            instance_name=dev.instance_name,  # Instance name from mDNS for exact matching
                            spotify_device_names=[],  # Will be populated when device appears in Spotify
                            ip=dev.ip,
                            port=dev.port,
                            cpath=dev.cpath or "/spotifyconnect/zeroconf",
                            volume_preset=DEFAULT_VOLUME
                        )
                        break
                
                # If still no profile, create minimal one
                if not device_profile:
                    device_profile = DeviceProfile(
                        name=target_device_name,
                        volume_preset=DEFAULT_VOLUME
                    )
            except Exception as e:
                logger.error(f"Error during mDNS discovery: {e}")
                # Create a minimal profile that the orchestrator can use
                device_profile = DeviceProfile(
                    name=target_device_name,
                    volume_preset=DEFAULT_VOLUME
                )
        
        # Apply alarm-specific volume setting
        alarm_volume = alarm.get("volume", DEFAULT_VOLUME)
        device_profile.volume_preset = alarm_volume
        
        # Update device profile in config targets
        found_in_targets = False
        for i, target in enumerate(alarm_config.targets):
            if target.name == target_device_name:
                alarm_config.targets[i] = device_profile
                found_in_targets = True
                break
        
        # If not found in targets, add it
        if not found_in_targets:
            alarm_config.targets.append(device_profile)
            # Save the device profile permanently so it persists with instance_name
            try:
                import json
                device_data = {
                    "devices": [device.model_dump() for device in alarm_config.targets],
                    "last_updated": str(time.time())
                }
                devices_file = str(DEVICES_FILE)
                with open(devices_file, 'w') as f:
                    json.dump(device_data, f, indent=2)
            except Exception:
                pass
        
        # Update context URI in config
        alarm_config.context_uri = playlist_uri
        
        # Update shuffle setting
        alarm_shuffle = alarm.get("shuffle", False)
        alarm_config.shuffle = alarm_shuffle
        
        # Create playback engine
        engine = AlarmPlaybackEngine(alarm_config)
        
        # Execute full timeline
        metrics = engine.play_alarm(target_device_name)
        
        # Save device profiles after alarm execution to persist any learned Spotify device names
        try:
            import json
            device_data = {
                "devices": [device.model_dump() for device in alarm_config.targets],
                "last_updated": str(time.time())
            }
            devices_file = str(DEVICES_FILE)
            with open(devices_file, 'w') as f:
                json.dump(device_data, f, indent=2)
        except Exception:
            pass
        
        # Log results
        logger.info(
            "Alarm %s completed in %sms (branch=%s)",
            alarm_id,
            metrics.total_duration_ms,
            metrics.branch,
        )
        
        # Track metrics for monitoring
        try:
            from alarm_config import save_metrics, load_metrics
            existing_metrics = load_metrics()
            
            # Create metric entry
            metric_entry = {
                "alarm_id": alarm_id,
                "timestamp": time.time(),
                "device_name": target_device_name,
                "playlist_name": playlist_name,
                "metrics": metrics.to_dict(),
                "success": not metrics.branch or not metrics.branch.startswith("failed:"),
                "failure_reason": metrics.branch.split(":")[1] if metrics.branch and ":" in metrics.branch else None
            }
            
            existing_metrics.append(metric_entry)
            
            # Keep only last 100 metrics to prevent file from growing too large
            if len(existing_metrics) > 100:
                existing_metrics = existing_metrics[-100:]
            
            save_metrics(existing_metrics)
        except Exception:
            pass
        
        if metrics.errors:
            logger.warning(f"Errors encountered: {len(metrics.errors)}")
            for error in metrics.errors:
                error_msg = error.get('error', 'Unknown error')
                error_phase = error.get('phase', 'unknown')
                logger.warning(f"  - {error_msg} (phase: {error_phase})")
                
                # Log helpful guidance for common errors
                if 'not_in_devices_by_deadline' in error_msg or 'manual authentication' in error_msg.lower():
                    logger.warning(f"  → TROUBLESHOOTING: Device may need manual authentication via Spotify app")
                    logger.warning(f"  → ACTION: Open Spotify app, select device '{target_device_name}', play a song")
        
            
    except RuntimeError as e:
        error_msg = str(e)
        alarm_id = alarm.get('id', 'unknown')
        logger.error(f"Alarm playback failed for alarm {alarm_id}: {error_msg}")
        
        # Track failure metrics
        try:
            from alarm_config import save_metrics, load_metrics
            existing_metrics = load_metrics()
            
            # Extract failure reason from error message
            failure_reason = None
            if 'not_in_devices_by_deadline' in error_msg:
                failure_reason = "not_in_devices_by_deadline"
            elif 'no_mdns' in error_msg:
                failure_reason = "no_mdns"
            elif 'circuit_breaker' in error_msg:
                failure_reason = "circuit_breaker_open"
            
            metric_entry = {
                "alarm_id": alarm_id,
                "timestamp": time.time(),
                "device_name": target_device_name,
                "playlist_name": alarm.get('playlist_name', 'Unknown'),
                "metrics": {
                    "branch": f"failed:{failure_reason}" if failure_reason else "failed:unknown",
                    "total_duration_ms": None,
                    "error_count": 1,
                    "errors": [{"error": error_msg, "phase": "runtime_error", "timestamp": time.time()}]
                },
                "success": False,
                "failure_reason": failure_reason
            }
            
            existing_metrics.append(metric_entry)
            
            # Keep only last 100 metrics
            if len(existing_metrics) > 100:
                existing_metrics = existing_metrics[-100:]
            
            save_metrics(existing_metrics)
            
            # Log monitoring info for not_in_devices_by_deadline
            if failure_reason == "not_in_devices_by_deadline":
                logger.warning("=" * 60)
                logger.warning("MONITORING: not_in_devices_by_deadline failure detected")
                logger.warning(f"  Alarm ID: {alarm_id}")
                logger.warning(f"  Device: {target_device_name}")
                logger.warning(f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
                logger.warning(f"  This failure has been logged to metrics.json for tracking")
                logger.warning("=" * 60)
        except Exception:
            pass
        
        # Extract helpful guidance from error message
        if 'manual authentication' in error_msg.lower() or 'not_in_devices_by_deadline' in error_msg:
            logger.error("=" * 60)
            logger.error("TROUBLESHOOTING GUIDE:")
            logger.error(f"  Device '{target_device_name}' needs manual authentication.")
            logger.error("  Steps to fix:")
            logger.error("  1. Open Spotify app on your phone or computer")
            logger.error(f"  2. Look for '{target_device_name}' in available devices")
            logger.error("  3. Select it and play any song to authenticate")
            logger.error("  4. Wait a few seconds, then retry the alarm")
            logger.error("=" * 60)
    except Exception as e:
        logger.error(f"Error running alarm {alarm.get('id', 'unknown')}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")

def prewarm_device(alarm: Dict[str, Any]) -> None:
    """Prewarm device 60 seconds before alarm time to wake it up."""
    try:
        alarm_id = alarm.get('id')
        target_device_name = alarm.get("device_name", DEFAULT_SPEAKER)
        
        # Find device profile
        device_profile = None
        for profile in alarm_config.targets:
            if profile.name == target_device_name:
                device_profile = profile
                break
        
        # If no profile, try to discover the device
        if not device_profile:
            try:
                from alarm_playback.discovery import discover_all_connect_devices
                all_devices = discover_all_connect_devices(timeout_s=2.0)
                
                for dev in all_devices:
                    # Try to match device name
                    if (target_device_name.lower() in dev.instance_name.lower() or
                        dev.instance_name.lower() in target_device_name.lower()):
                        device_profile = DeviceProfile(
                            name=dev.instance_name,
                            ip=dev.ip,
                            port=dev.port,
                            cpath=dev.cpath or "/spotifyconnect/zeroconf",
                            volume_preset=DEFAULT_VOLUME
                        )
                        break
            except Exception:
                pass
        
        # Wake up device if we have IP
        if device_profile and device_profile.ip:
            try:
                from alarm_playback.fallback import _wake_device_via_ip
                _wake_device_via_ip(
                    device_profile.ip,
                    device_profile.port or 80,
                    device_profile.cpath or "/spotifyconnect/zeroconf",
                    target_device_name
                )
            except Exception as e:
                logger.warning(f"Prewarm failed for {target_device_name}: {e}")
            
    except Exception as e:
        logger.error(f"Error during prewarm for alarm {alarm.get('id', 'unknown')}: {e}")

def schedule_alarms():
    """Schedule all alarms using APScheduler with T-60 prewarm."""
    global scheduler
    
    # Remove existing jobs
    if scheduler:
        scheduler.remove_all_jobs()
    
    # Schedule each alarm
    for alarm in alarms:
        if not alarm.get('active', True):
            continue
        
        alarm_id = alarm['id']
        hour = alarm.get('hour', 0)
        minute = alarm.get('minute', 0)
        dow = alarm.get('dow', '')
        
        # Convert day of week to cron format (APScheduler uses ISO 8601: Monday=0, Sunday=6)
        dow_map = {'mon': '0', 'tue': '1', 'wed': '2', 'thu': '3', 'fri': '4', 'sat': '5', 'sun': '6'}
        day_of_week = ','.join([dow_map[d.strip().lower()] for d in dow.split(',') if d.strip().lower() in dow_map])
        
        # Validate that at least one day is specified
        if not day_of_week:
            logger.warning(f"Skipping alarm {alarm_id}: no valid days of week specified (dow='{dow}')")
            continue
        
        try:
            # Schedule T-60 prewarm (60 seconds before alarm)
            prewarm_minute = minute - 1
            prewarm_hour = hour
            if prewarm_minute < 0:
                prewarm_minute += 60
                prewarm_hour -= 1
                if prewarm_hour < 0:
                    prewarm_hour = 23
            
            prewarm_trigger = CronTrigger(hour=prewarm_hour, minute=prewarm_minute, day_of_week=day_of_week)
            scheduler.add_job(
                prewarm_device,
                trigger=prewarm_trigger,
                args=(alarm,),
                id=f"prewarm_{alarm_id}",
                replace_existing=True
            )
            
            # Schedule alarm start
            trigger = CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week)
            scheduler.add_job(
                run_alarm,
                trigger=trigger,
                args=(alarm,),
                id=f"alarm_{alarm_id}",
                replace_existing=True,
                misfire_grace_time=None  # Allow misfired alarms to run regardless of delay
            )
            
            # Schedule alarm stop if stop time is set
            stop_hour = alarm.get('stop_hour')
            stop_minute = alarm.get('stop_minute')
            if stop_hour is not None and stop_minute is not None:
                stop_trigger = CronTrigger(hour=stop_hour, minute=stop_minute, day_of_week=day_of_week)
                scheduler.add_job(
                    stop_alarm_playback,
                    trigger=stop_trigger,
                    args=(alarm_id,),
                    id=f"stop_{alarm_id}",
                    replace_existing=True,
                    misfire_grace_time=None  # Allow misfired alarms to run regardless of delay
                )
        except Exception as e:
            logger.error(f"Failed to schedule alarm {alarm_id}: {e}")

def stop_alarm_playback(alarm_id: str):
    """Stop playback for a specific alarm."""
    logger.info(f"Stop time reached for alarm {alarm_id}, stopping playback")
    
    # Stop regular Spotify playback only
    sp = get_spotify_client()
    if sp:
        try:
            _retry_spotify_api(lambda: sp.pause_playback(), max_retries=2)
            logger.info(f"Successfully paused Spotify playback for alarm {alarm_id}")
        except spotipy.SpotifyException as e:
            if e.http_status == 404 or "NO_ACTIVE_DEVICE" in str(e):
                pass
            elif e.http_status == 401:
                logger.warning(f"401 Unauthorized pausing playback for alarm {alarm_id} - token may be expired")
                _reset_spotify_client()
            else:
                logger.error(f"Failed to pause Spotify playback for alarm {alarm_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to pause Spotify playback for alarm {alarm_id}: {e}")
    
    logger.info(f"Removed alarm {alarm_id} from active playback tracking")

def background_device_registration():
    """Background task to register devices when not detected in Spotify (generic for all devices)."""
    global alarm_config
    
    while running:
        try:
            if not alarm_config:
                time.sleep(60)
                continue
            
            # Check all devices in targets (generic for all speakers)
            sp = get_spotify_client()
            if sp and alarm_config.targets:
                try:
                    devices = _retry_spotify_api(lambda: sp.devices(), max_retries=2)
                    spotify_device_names = {d['name'].upper() for d in devices.get('devices', [])}
                except (EOFError, spotipy.SpotifyException) as e:
                    if isinstance(e, spotipy.SpotifyException) and e.http_status == 401:
                        logger.warning("401 Unauthorized checking Spotify devices - token may be expired")
                        _reset_spotify_client()
                    continue
                except Exception:
                    continue
                try:
                    
                    # Check each device in targets
                    for device in alarm_config.targets:
                        if not device.ip:
                            continue
                            
                        device_found = any(device.name.upper() in d['name'].upper() or d['name'].upper() in device.name.upper() 
                                          for d in devices.get('devices', []))
                        
                        if not device_found:
                            try:
                                from alarm_playback.fallback import _mdns_auth_user_registration
                                _mdns_auth_user_registration(device.ip, device.name)
                            except Exception:
                                pass
                            
                except Exception:
                    pass
            
            time.sleep(60)  # Check every minute
            
        except Exception as e:
            logger.error(f"Error in background device registration: {e}")
            time.sleep(60)

def background_cache_refresh():
    """Refresh device cache in background every 300s to keep it warm."""
    global device_cache, device_cache_timestamp, alarm_config, running, friendly_name_cache
    
    while running:
        try:
            time.sleep(300)  # Wait 300 seconds (5 minutes) between refreshes (increased from 120s)
            
            # Skip if cache is still fresh (within 600s = 10 minutes)
            if device_cache_timestamp and (time.time() - device_cache_timestamp) < 600:
                continue
            
            # Skip if alarm_config not initialized
            if not alarm_config:
                continue
            
            # Call the cache refresh logic synchronously
            try:
                from alarm_playback.zeroconf_client import check_device_health
                from alarm_playback.discovery import discover_all_connect_devices
                
                devices_list = []
                device_names_seen = set()
                
                # Add devices from config - refresh names from getInfo (with caching)
                if alarm_config.targets:
                    current_time = time.time()
                    for device in alarm_config.targets:
                        # Check friendly name cache first
                        cache_key = (device.ip, device.port)
                        fresh_name = device.name  # Default to saved name
                        
                        # Use cached friendly name if available and fresh
                        if cache_key in friendly_name_cache:
                            cached_name, cached_time = friendly_name_cache[cache_key]
                            if (current_time - cached_time) < friendly_name_cache_ttl:
                                fresh_name = cached_name
                            else:
                                # Cache expired, remove it
                                del friendly_name_cache[cache_key]
                        
                        # Only call getInfo if not cached or cache expired
                        if cache_key not in friendly_name_cache:
                            try:
                                import sys
                                sys.path.insert(0, str(Path(__file__).parent))
                                from device_registry import DeviceRegistry
                                device_registry = DeviceRegistry(alarm_config)
                                from alarm_playback.discovery import DiscoveryResult
                                discovery_result = DiscoveryResult(
                                    instance_name=device.name,
                                    ip=device.ip,
                                    port=device.port,
                                    cpath=device.cpath,
                                    txt_records={}
                                )
                                fresh_name = device_registry._extract_friendly_name(discovery_result)
                                if not fresh_name:
                                    fresh_name = device.name
                                else:
                                    # Cache the friendly name
                                    friendly_name_cache[cache_key] = (fresh_name, current_time)
                                
                                # If we got a better name from getInfo, update the saved device profile
                                if fresh_name != device.name:
                                    try:
                                        # Update the device profile with the fresh name
                                        device.name = fresh_name
                                        device_data = {
                                            "devices": [device.model_dump() for device in alarm_config.targets],
                                            "last_updated": str(time.time())
                                        }
                                        with open(DEVICES_FILE, 'w') as f:
                                            json.dump(device_data, f, indent=2)
                                    except Exception:
                                        pass
                            except Exception:
                                fresh_name = device.name  # Fallback to saved name
                        
                        # Health check uses getInfo internally, but we can use a longer timeout since we're not in a hurry
                        health_info = check_device_health(device.ip, device.port, device.cpath, timeout_s=1.0)
                        devices_list.append({
                            "name": fresh_name,  # Use fresh name from getInfo
                            "ip": device.ip,
                            "port": device.port,
                            "cpath": device.cpath,
                            "is_online": health_info['responding'],
                            "last_seen": time.time() if health_info['responding'] else None,
                            "response_time_ms": health_info.get('response_time_ms'),
                            "error": health_info.get('error')
                        })
                        device_names_seen.add(fresh_name)
                
                # Add mDNS devices - use DeviceRegistry to get names from device properties (getInfo with caching)
                # Only do full discovery every 15 minutes to reduce network load
                current_time = time.time()
                last_discovery_time = getattr(background_cache_refresh, '_last_full_discovery', 0)
                discovery_interval = 900  # 15 minutes
                
                mdns_devices = []
                if (current_time - last_discovery_time) >= discovery_interval:
                    mdns_devices = discover_all_connect_devices(1.0)
                    background_cache_refresh._last_full_discovery = current_time
                
                for dev in mdns_devices:
                    # Check friendly name cache first
                    cache_key = (dev.ip, dev.port)
                    dev_name = None
                    
                    # Use cached friendly name if available and fresh
                    if cache_key in friendly_name_cache:
                        cached_name, cached_time = friendly_name_cache[cache_key]
                        if (current_time - cached_time) < friendly_name_cache_ttl:
                            dev_name = cached_name
                    
                    # Only call getInfo if not cached
                    if not dev_name:
                        try:
                            if alarm_config:
                                import sys
                                sys.path.insert(0, str(APP_DIR))
                                from device_registry import DeviceRegistry
                                device_registry = DeviceRegistry(alarm_config)
                                dev_name = device_registry._extract_friendly_name(dev)
                                if dev_name:
                                    # Cache the friendly name
                                    friendly_name_cache[cache_key] = (dev_name, current_time)
                                else:
                                    logger.info(f"_extract_friendly_name returned None for {dev.instance_name} ({dev.ip}:{dev.port})")
                        except Exception as e:
                            logger.warning(f"Error extracting friendly name for {dev.instance_name}: {e}")
                    
                    # Fallback to instance_name if extraction failed
                    if not dev_name:
                        dev_name = dev.instance_name or f"Device at {dev.ip}"
                    
                    if dev_name not in device_names_seen and dev.ip and dev.port:
                        health_info = check_device_health(dev.ip, dev.port, dev.cpath or "/", timeout_s=1.0)
                        devices_list.append({
                            "name": dev_name,
                            "ip": dev.ip,
                            "port": dev.port,
                            "cpath": dev.cpath,
                            "is_online": health_info['responding'],
                            "last_seen": time.time() if health_info['responding'] else None,
                            "response_time_ms": health_info.get('response_time_ms'),
                            "error": health_info.get('error')
                        })
                        device_names_seen.add(dev_name)
                
                # Add Spotify devices
                sp = get_spotify_client()
                if sp:
                    try:
                        spotify_devices = _retry_spotify_api(lambda: sp.devices(), max_retries=2)
                        for dev in spotify_devices.get('devices', []):
                            dev_name = dev.get('name', 'Unknown')
                            if dev_name not in device_names_seen:
                                devices_list.append({
                                    "name": dev_name,
                                    "ip": None,
                                    "port": None,
                                    "cpath": None,
                                    "is_online": dev.get('is_active', False),
                                    "last_seen": time.time() if dev.get('is_active', False) else None,
                                    "response_time_ms": None,
                                    "error": None if dev.get('is_active', False) else "Device inactive"
                                })
                                device_names_seen.add(dev_name)
                    except (EOFError, spotipy.SpotifyException) as e:
                        if isinstance(e, spotipy.SpotifyException) and e.http_status == 401:
                            logger.warning("401 Unauthorized getting Spotify devices - token may be expired")
                            _reset_spotify_client()
                    except Exception:
                        pass
                
                # Update cache
                device_cache = {
                    "total_devices": len(devices_list),
                    "online_devices": sum(1 for d in devices_list if d['is_online']),
                    "offline_devices": len(devices_list) - sum(1 for d in devices_list if d['is_online']),
                    "devices": devices_list
                }
                device_cache_timestamp = time.time()
                
            except Exception as e:
                logger.error(f"Error refreshing cache: {e}")
            
        except Exception as e:
            logger.error(f"Error in background cache refresh: {e}")
            time.sleep(120)

@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    """Serve favicon as .ico (browsers default to this)."""
    favicon_path = STATIC_DIR / "favicon.svg"
    if favicon_path.exists():
        return FileResponse(
            str(favicon_path.resolve()),
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "public, max-age=86400"
            }
        )
    raise HTTPException(status_code=404)

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Main page."""
    global spotify  # Declare global at function level
    
    # Check for callback parameters
    connected = request.query_params.get("connected")
    error = request.query_params.get("error")
    
    # Check Spotify connection status
    sp = get_spotify_client()
    is_connected = sp is not None
    
    if not is_connected:
        # Show connection screen
            return templates.TemplateResponse("index.html", {
                "request": request,
            "alarms": alarms,
                "playlists": [],
                "devices": [],
            "default_speaker": DEFAULT_SPEAKER,
            "default_volume": DEFAULT_VOLUME,
            "default_shuffle": DEFAULT_SHUFFLE,
            "spotify_connected": False,
            "spotify_auth_url": sp_oauth.get_authorize_url(),
            "auth_error": error,
            "auth_success": connected
        })
    
        # Get Spotify playlists (cached)
    playlists = []
    try:
        global playlist_cache, playlist_cache_timestamp
        if playlist_cache and playlist_cache_timestamp and (time.time() - playlist_cache_timestamp) < playlist_cache_ttl:
            playlists = playlist_cache
        else:
            if not sp:
                logger.warning("Cannot fetch playlists: Spotify client is not available. Token may be missing or invalid.")
            else:
                try:
                    # Use retry mechanism for API call
                    playlists_response = _retry_spotify_api(
                        lambda: sp.current_user_playlists(limit=50)
                    )
                playlists = playlists_response.get('items', [])
                playlist_cache = playlists
                playlist_cache_timestamp = time.time()
                except EOFError as e:
                    logger.error(f"EOFError getting playlists (interactive auth attempted during API call): {e}")
                    _reset_spotify_client()
                    playlists = []
                except spotipy.SpotifyException as e:
                    if e.http_status == 401:
                        logger.error(f"401 Unauthorized getting playlists - token expired or invalid")
                        _reset_spotify_client()
                    else:
                        logger.error(f"Spotify API error getting playlists (HTTP {e.http_status}): {e}")
                    playlists = []
                except Exception as e:
                    logger.error(f"Unexpected error getting playlists: {e}")
                    playlists = []
    except EOFError as e:
        logger.error(f"EOFError getting playlists (interactive auth attempted): {e}")
        # Reset the global spotify client to force re-authentication
        _reset_spotify_client()
        playlists = []
    except Exception as e:
        logger.error(f"Error getting playlists: {e}")
        # Log additional context for debugging
        if not sp:
            logger.error("  Spotify client is None - authentication may be required")
        elif not _validate_token_file():
            logger.error("  Token file is missing, empty, or invalid - re-authentication required")
    
    # Get all devices from cache (fast - no mDNS on every page load)
    all_devices = []
    try:
        global device_cache, device_cache_timestamp, device_cache_ttl
        
        # Use cached devices from test page (includes all mDNS + Spotify devices)
        if device_cache and device_cache_timestamp and (time.time() - device_cache_timestamp) < device_cache_ttl:
            # Use cached data (includes all devices from test page)
            all_devices = [{
                "name": d["name"],
                "ip": d["ip"],
                "is_online": d["is_online"]
            } for d in device_cache.get("devices", [])]
        else:
            # No cache - fetch all devices directly
            try:
                # Get mDNS devices
                from alarm_playback.discovery import discover_all_connect_devices
                loop = asyncio.get_event_loop()
                # Use await to get the result from executor
                mdn_result = await loop.run_in_executor(None, discover_all_connect_devices, 1.0)
                
                mdn_device_names = set()
                for device in mdn_result:
                    # Discovery共同lt doesn't have is_online, check via health check
                    from alarm_playback.zeroconf_client import check_device_health
                    health_info = check_device_health(device.ip, device.port, device.cpath or "/", timeout_s=1.0)
                    
                    # Use DeviceRegistry to get name from device properties (getInfo)
                    device_name = None
                    try:
                        if alarm_config:
                            import sys
                            sys.path.insert(0, str(APP_DIR))
                            from device_registry import DeviceRegistry
                            device_registry = DeviceRegistry(alarm_config)
                            device_name = device_registry._extract_friendly_name(device)
                    except Exception:
                        pass
                    
                    # Fallback to instance_name if extraction failed
                    if not device_name:
                        device_name = device.instance_name or device.name
                    
                    all_devices.append({
                        "name": device_name,
                        "ip": device.ip,
                        "is_online": health_info['responding']
                    })
                    mdn_device_names.add(device_name.upper())
                
                # Get Spotify devices (not already found via mDNS)
                if sp:
                    try:
                        devices_response = _retry_spotify_api(lambda: sp.devices(), max_retries=2)
                for dev in devices_response.get('devices', []):
                    dev_name = dev.get('name', 'Unknown')
                    if dev_name.upper() not in mdn_device_names:
                        all_devices.append({
                            "name": dev_name,
                            "ip": None,
                            "is_online": dev.get('is_active', False)
                        })
                    except (EOFError, spotipy.SpotifyException) as e:
                        if isinstance(e, spotipy.SpotifyException) and e.http_status == 401:
                            logger.warning("401 Unauthorized getting Spotify devices - token may be expired")
                            _reset_spotify_client()
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Error getting devices: {e}")
                all_devices = []
    except Exception as e:
        logger.error(f"Error getting devices: {e}")
        all_devices = []
        
    return templates.TemplateResponse("index.html", {
        "request": request,
        "alarms": alarms,
        "playlists": playlists,
        "devices": all_devices,
        "default_speaker": DEFAULT_SPEAKER,
        "default_volume": DEFAULT_VOLUME,
        "default_shuffle": DEFAULT_SHUFFLE,
        "spotify_connected": True,
        "spotify_auth_url": None
    })

@app.get("/api/playlists")
async def get_playlists():
    """Get all Spotify playlists."""
    global spotify  # Declare global at function level
    
    try:
        sp = get_spotify_client()
        if not sp:
            # Provide detailed error message based on token file state
            if not TOKEN_FILE.exists():
                error_detail = "Spotify token file not found. Please authenticate with Spotify."
            elif TOKEN_FILE.stat().st_size == 0:
                error_detail = "Spotify token file is empty. Please re-authenticate with Spotify."
            elif not _validate_token_file():
                error_detail = "Spotify token file is invalid or corrupted. Please re-authenticate with Spotify."
            else:
                error_detail = "Not authenticated with Spotify. Please authenticate to access playlists."
            logger.warning(f"Failed to get Spotify client for playlists: {error_detail}")
            raise HTTPException(status_code=401, detail=error_detail)
        
        try:
            # Use retry mechanism for API call
            playlists_response = _retry_spotify_api(
                lambda: sp.current_user_playlists(limit=50)
            )
        return {"playlists": playlists_response.get('items', [])}
        except EOFError as e:
            logger.error(f"EOFError getting playlists (interactive auth attempted during API call): {e}")
            _reset_spotify_client()
            raise HTTPException(status_code=401, detail="Spotify authentication required. Please re-authenticate.")
        except spotipy.SpotifyException as e:
            if e.http_status == 401:
                logger.error(f"401 Unauthorized getting playlists - token expired or invalid")
                _reset_spotify_client()
                raise HTTPException(status_code=401, detail="Spotify authentication expired. Please re-authenticate.")
            elif e.http_status == 429:
                logger.error(f"Rate limit exceeded getting playlists")
                raise HTTPException(status_code=429, detail="Spotify API rate limit exceeded. Please try again later.")
            else:
                logger.error(f"Spotify API error getting playlists (HTTP {e.http_status}): {e}")
                raise HTTPException(status_code=500, detail=f"Spotify API error: {str(e)}")
    except HTTPException:
        # Re-raise HTTP exceptions (like 401) as-is
        raise
    except EOFError as e:
        logger.error(f"EOFError getting playlists (interactive auth attempted): {e}")
        # Reset the global spotify client to force re-authentication
        _reset_spotify_client()
        raise HTTPException(status_code=401, detail="Spotify authentication required. Please re-authenticate.")
    except Exception as e:
        logger.error(f"Error getting playlists: {e}")
        # Log additional context for debugging
        if not _validate_token_file():
            logger.error("  Token file validation failed - this may be the root cause")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/spotify/devices")
async def get_spotify_devices():
    """Get all available Spotify Connect devices."""
    try:
        sp = get_spotify_client()
        if not sp:
            raise HTTPException(status_code=401, detail="Not authenticated with Spotify. Please connect your Spotify account.")
        
        # Use retry mechanism for API call
        devices = _retry_spotify_api(lambda: sp.devices())
        return {"devices": devices.get('devices', [])}
    except EOFError as e:
        logger.error(f"EOFError getting Spotify devices: {e}")
        _reset_spotify_client()
        raise HTTPException(status_code=401, detail="Spotify authentication required. Please re-authenticate.")
    except spotipy.SpotifyException as e:
        if e.http_status == 401:
            logger.error(f"401 Unauthorized getting Spotify devices - token expired or invalid")
            _reset_spotify_client()
            raise HTTPException(status_code=401, detail="Spotify authentication expired. Please re-authenticate.")
        elif e.http_status == 429:
            logger.error(f"Rate limit exceeded getting Spotify devices")
            raise HTTPException(status_code=429, detail="Spotify API rate limit exceeded. Please try again later.")
        else:
            logger.error(f"Spotify API error getting devices (HTTP {e.http_status}): {e}")
            raise HTTPException(status_code=500, detail=f"Spotify API error: {str(e)}")
    except Exception as e:
        logger.error(f"Unexpected error getting Spotify devices: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get Spotify devices: {str(e)}")

@app.get("/api/devices")
async def get_devices():
    """Get all discovered devices (online and offline)."""
    try:
        from alarm_playback.zeroconf_client import check_device_health
        import time
        
        global device_cache, device_cache_timestamp, device_cache_ttl
        
        if not alarm_config:
            return {"error": "Alarm config not initialized"}
        
        # Check cache first
        if device_cache and device_cache_timestamp and (time.time() - device_cache_timestamp) < device_cache_ttl:
            return device_cache
        
        # Get devices from config AND Spotify
        devices_list = []
        device_names_seen = set()
        
        # First, add devices from config - but refresh names from getInfo
        if alarm_config.targets:
            for device in alarm_config.targets:
                # Try to get fresh friendly name from getInfo (overrides saved name)
                fresh_name = device.name  # Default to saved name
                try:
                    import sys
                    sys.path.insert(0, str(APP_DIR))
                    from device_registry import DeviceRegistry
                    device_registry = DeviceRegistry(alarm_config)
                    # Create a discovery result-like object for getInfo call
                    from alarm_playback.discovery import DiscoveryResult
                    discovery_result = DiscoveryResult(
                        instance_name=device.name,
                        ip=device.ip,
                        port=device.port,
                        cpath=device.cpath,
                        txt_records={}
                    )
                    fresh_name = device_registry._extract_friendly_name(discovery_result)
                    if not fresh_name:
                        fresh_name = device.name
                    # If we got a better name from getInfo, update the saved device profile
                    elif fresh_name != device.name:
                        try:
                            # Update the device profile with the fresh name
                            device.name = fresh_name
                            alarm_config.save_device_profiles()
                        except Exception:
                            pass
                except Exception:
                    fresh_name = device.name  # Fallback to saved name
                
                # Check if device is online
                health_info = check_device_health(
                    device.ip, 
                    device.port, 
                    device.cpath, 
                    timeout_s=1.0
                )
                
                devices_list.append({
                    "name": fresh_name,  # Use fresh name from getInfo if available
                    "ip": device.ip,
                    "port": device.port,
                    "cpath": device.cpath,
                    "is_online": health_info['responding'],
                    "last_seen": time.time() if health_info['responding'] else None,
                    "response_time_ms": health_info.get('response_time_ms'),
                    "error": health_info.get('error')
                })
                device_names_seen.add(fresh_name)
        
        # Then, discover devices via mDNS (run in thread to avoid blocking async event loop)
        try:
            from alarm_playback.discovery import discover_all_connect_devices
            import asyncio
            
            # Run in thread pool executor to avoid blocking async event loop
            loop = asyncio.get_event_loop()
            mdns_devices = await loop.run_in_executor(None, discover_all_connect_devices, 2.0)  # Reduced from 5.0 to 2.0
            # Use DeviceRegistry to get names from device properties (getInfo)
            for dev in mdns_devices:
                # Use DeviceRegistry to extract friendly name (tries getInfo first)
                dev_name = None
                try:
                    if alarm_config:
                        import sys
                        sys.path.insert(0, str(APP_DIR))
                        from device_registry import DeviceRegistry
                        device_registry = DeviceRegistry(alarm_config)
                        dev_name = device_registry._extract_friendly_name(dev)
                except Exception as e:
                    logger.error(f"Error extracting friendly name for {dev.instance_name}: {e}")
                
                # Fallback to instance_name if extraction failed
                if not dev_name:
                    dev_name = dev.instance_name or f"Device at {dev.ip}"
                
                # Only add if not already in list
                if dev_name not in device_names_seen:
                    # Check if device is online (skip health check for cached results)
                    if dev.ip and dev.port:
                        health_info = check_device_health(
                            dev.ip, dev.port, dev.cpath or "/", timeout_s=1.0  # Increased timeout for slower devices
                        )
                        devices_list.append({
                            "name": dev_name,
                            "ip": dev.ip,
                            "port": dev.port,
                            "cpath": dev.cpath,
                            "is_online": health_info['responding'],
                            "last_seen": time.time() if health_info['responding'] else None,
                            "response_time_ms": health_info.get('response_time_ms'),
                            "error": health_info.get('error')
                        })
                        device_names_seen.add(dev_name)
        except Exception as e:
            logger.warning(f"Failed to get mDNS devices: {e}")
        
        # Finally, add Spotify devices as additional devices
        try:
            sp = get_spotify_client()
            if sp:
                try:
                    spotify_devices = _retry_spotify_api(lambda: sp.devices(), max_retries=2)
                for dev in spotify_devices.get('devices', []):
                    dev_name = dev.get('name', 'Unknown')
                    # Only add if not already in list from config or mDNS
                    if dev_name not in device_names_seen:
                        devices_list.append({
                            "name": dev_name,
                            "ip": None,
                            "port": None,
                            "cpath": None,
                            "is_online": dev.get('is_active', False),
                            "last_seen": time.time() if dev.get('is_active', False) else None,
                            "response_time_ms": None,
                            "error": None if dev.get('is_active', False) else "Device inactive"
                        })
                        device_names_seen.add(dev_name)
                except (EOFError, spotipy.SpotifyException) as e:
                    if isinstance(e, spotipy.SpotifyException) and e.http_status == 401:
                        logger.warning("401 Unauthorized getting Spotify devices - token may be expired")
                        _reset_spotify_client()
                except Exception:
                    pass
        except Exception:
            pass
        
        # Calculate counts
        total_devices = len(devices_list)
        online_devices = sum(1 for d in devices_list if d['is_online'])
        offline_devices = total_devices - online_devices
        
        result = {
            "total_devices": total_devices,
            "online_devices": online_devices,
            "offline_devices": offline_devices,
            "devices": devices_list
        }
        
        # Update cache (this will use fresh friendly names from getInfo)
        device_cache = result
        device_cache_timestamp = time.time()
        
        return result
        
    except Exception as e:
        logger.error(f"Error getting devices: {e}")
        import traceback
        traceback.print_exc()
        return {"total_devices": 0, "online_devices": 0, "offline_devices": 0, "devices": []}

@app.post("/api/devices/refresh")
async def refresh_devices():
    """Force refresh of device discovery."""
    try:
        import sys
        sys.path.insert(0, str(APP_DIR))
        from device_registry import DeviceRegistry
        
        if not alarm_config:
            return {"error": "Alarm config not initialized"}
        
        # Clear the device cache to force fresh discovery
        global device_cache, device_cache_timestamp
        device_cache = None
        device_cache_timestamp = None
        logger.info("Cleared device cache - forcing fresh discovery")
        
        registry = DeviceRegistry(alarm_config)
        registry.discover_devices(force_refresh=True)
        
        return {"status": "success", "message": "Devices refreshed and cache cleared"}
    except Exception as e:
        logger.error(f"Error refreshing devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/disconnect")
async def disconnect_spotify():
    """Disconnect from Spotify by deleting token."""
    try:
        # Delete token file
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        
        # Clear global spotify client and caches
        _reset_spotify_client()
        
        logger.info("Spotify disconnected")
        return {"status": "success", "message": "Disconnected from Spotify"}
    except Exception as e:
        logger.error(f"Error disconnecting Spotify: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect")

@app.post("/set_alarm")
async def set_alarm(
    playlist_uri: str = Form(...),
    device_name: str = Form(...),
    hour: int = Form(...),
    minute: int = Form(...),
    hour_period: str = Form(...),
    stop_hour: Optional[str] = Form(None),
    stop_minute: Optional[str] = Form(None),
    stop_hour_period: Optional[str] = Form(None),
    dow: List[str] = Form(...),
    volume: int = Form(...),
    shuffle: bool = Form(False)
):
    """Set a new alarm."""
    try:
        # Convert to 24-hour format
        if hour_period == "PM" and hour != 12:
            hour += 12
        elif hour_period == "AM" and hour == 12:
            hour = 0
        
        # Parse stop times - convert empty strings to None, then to int
        parsed_stop_hour = int(stop_hour) if stop_hour and stop_hour.strip() else None
        parsed_stop_minute = int(stop_minute) if stop_minute and stop_minute.strip() else None
        
        # Get playlist name from Spotify API
        playlist_name = get_playlist_name(playlist_uri)
        
        # Create alarm
        alarm = {
            "id": f"alarm_{int(time.time())}",
            "playlist_uri": playlist_uri,
            "playlist_name": playlist_name,
            "device_name": device_name,
            "hour": hour,
            "minute": minute,
            "dow": ",".join(dow),  # Store as comma-separated string
            "volume": volume,
            "shuffle": shuffle,
            "active": True,
            "recurring": True,
            "created_at": datetime.now().isoformat()
        }
        
        # Add stop time if provided
        if parsed_stop_hour is not None and parsed_stop_minute is not None and stop_hour_period:
            if stop_hour_period == "PM" and parsed_stop_hour != 12:
                parsed_stop_hour += 12
            elif stop_hour_period == "AM" and parsed_stop_hour == 12:
                parsed_stop_hour = 0
            
            alarm["stop_hour"] = parsed_stop_hour
            alarm["stop_minute"] = parsed_stop_minute
        
        alarms.append(alarm)
        
        # Don't clear device cache - devices don't change when alarms change
        
        # Save and reschedule synchronously to ensure it completes
        # (Background thread can fail silently, causing persistence issues)
        try:
            save_data()
            schedule_alarms()
            logger.info(f"Successfully saved and scheduled alarm {alarm['id']}")
        except Exception as e:
            logger.error(f"Error saving/scheduling alarm {alarm.get('id', 'unknown')}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
        
        logger.info(f"Created alarm: {alarm['id']}")
        return RedirectResponse(url="/", status_code=303)
        
    except Exception as e:
        logger.error(f"Error creating alarm: {e}")
        raise HTTPException(status_code=500, detail="Failed to create alarm")

@app.delete("/delete_alarm/{alarm_id}")
async def delete_alarm(alarm_id: str):
    """Delete an alarm."""
    global alarms
    
    try:
        alarms = [alarm for alarm in alarms if alarm['id'] != alarm_id]
        
        # Don't clear device cache - devices don't change when alarms change
        
        save_data()
        
        # Reschedule all alarms without the deleted alarm
        schedule_alarms()
        
        logger.info(f"Deleted alarm: {alarm_id}")
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Error deleting alarm: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete alarm")

@app.post("/play_alarm_now/{alarm_id}")
async def play_alarm_now(alarm_id: str):
    """Play an alarm immediately."""
    try:
        # Find the alarm
        alarm = None
        for a in alarms:
            if a['id'] == alarm_id:
                alarm = a
                break
        
        if not alarm:
            raise HTTPException(status_code=404, detail="Alarm not found")
        
        # Run alarm in separate thread
        alarm_thread = threading.Thread(target=run_alarm, args=(alarm,))
        alarm_thread.daemon = True
        alarm_thread.start()
        
        logger.info(f"Started immediate playback for alarm {alarm_id}")
        return {"status": "success", "message": "Playback started"}
        
    except Exception as e:
        logger.error(f"Error starting playback: {e}")
        raise HTTPException(status_code=500, detail="Failed to start playback")

@app.post("/stop_current_playback")
async def stop_current_playback():
    """Stop current Spotify playback."""
    try:
        sp = get_spotify_client()
        if not sp:
            raise HTTPException(status_code=401, detail="Not authenticated with Spotify")
        
        # Check if anything is playing
        try:
            current = _retry_spotify_api(lambda: sp.current_playback())
            if not current or not current.get('is_playing'):
                return {"status": "info", "message": "Nothing is playing"}
        except spotipy.SpotifyException as e:
            # If we can't check, just try to stop
            if e.http_status == 404 or "NO_ACTIVE_DEVICE" in str(e):
                return {"status": "info", "message": "No active device found"}
        except Exception:
            pass
        
        # Stop playback
        try:
            _retry_spotify_api(lambda: sp.pause_playback())
        logger.info("Stopped current playback")
        return {"status": "success", "message": "Playback stopped"}
    except spotipy.SpotifyException as e:
        # Handle Spotify API errors gracefully
        if e.http_status == 404 or "NO_ACTIVE_DEVICE" in str(e):
                return {"status": "info", "message": "No active device found"}
            elif e.http_status == 401:
                logger.error(f"401 Unauthorized stopping playback - token expired or invalid")
                _reset_spotify_client()
                raise HTTPException(status_code=401, detail="Spotify authentication expired. Please re-authenticate.")
            else:
                logger.error(f"Spotify API error stopping playback (HTTP {e.http_status}): {e}")
                raise HTTPException(status_code=500, detail=f"Failed to stop playback: {str(e)}")
    except EOFError as e:
        logger.error(f"EOFError stopping playback: {e}")
        _reset_spotify_client()
        raise HTTPException(status_code=401, detail="Spotify authentication required. Please re-authenticate.")
    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except Exception as e:
        logger.error(f"Unexpected error stopping playback: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop playback: {str(e)}")

@app.get("/callback")
async def callback(request: Request):
    """Spotify OAuth callback."""
    code = request.query_params.get("code")
    error = request.query_params.get("error")
    
    if error:
        logger.error(f"Spotify OAuth error: {error}")
        return RedirectResponse(url="/?error=auth_failed")
    
    if not code:
        logger.error("Missing authorization code in callback")
        return RedirectResponse(url="/?error=no_code")
    
    try:
        # Exchange code for token
        # get_access_token() handles OAuth exchange and automatically saves to cache_path
        # Spotipy manages the token file automatically via cache_path - no manual saving needed
        # Note: get_access_token() may return just a string in future versions,
        # but get_cached_token() always returns the full token dict
        sp_oauth.get_access_token(code)
        
        # Verify token was saved by spotipy (it saves automatically to cache_path)
        token_info = sp_oauth.get_cached_token()
        if not token_info:
            raise ValueError("Failed to get token after OAuth exchange")
        
        # Ensure parent directory exists (spotipy should have created it, but be safe)
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        
        # Set proper file permissions for security (rw-------)
        # Note: spotipy already saved the token to cache_path, we're just setting permissions
        if TOKEN_FILE.exists():
            try:
                os.chmod(TOKEN_FILE, 0o600)
                logger.info(f"Token saved by spotipy to {TOKEN_FILE} ({TOKEN_FILE.stat().st_size} bytes)")
            except Exception as e:
                logger.warning(f"Could not set token file permissions: {e}")
        
        logger.info("Spotify authentication successful")
        return RedirectResponse(url="/?connected=true")
            
    except Exception as e:
        logger.error(f"Error in Spotify callback: {e}")
        return RedirectResponse(url="/?error=auth_failed")

if __name__ == "__main__":
    import uvicorn
    import ssl
    
    # SSL certificate paths
    ssl_dir = BASE_DIR / "ssl"
    # Get IP address from SPOTIFY_REDIRECT_URI
    redirect_uri = os.getenv("SPOTIFY_REDIRECT_URI", "https://localhost:443/callback")
    ip_address = redirect_uri.replace("https://", "").replace("http://", "").split("/")[0].split(":")[0]
    
    cert_file = ssl_dir / f"{ip_address}.pem"
    key_file = ssl_dir / f"{ip_address}-key.pem"
    
    # Verify SSL certificates exist
    if not cert_file.exists() or not key_file.exists():
        logger.error(f"SSL certificates not found at {cert_file} and {key_file}")
        logger.error("Wakeify requires HTTPS. Please ensure SSL certificates are generated.")
        logger.error("Certificates are auto-generated on first container start.")
        raise FileNotFoundError(f"SSL certificates required but not found. Expected: {cert_file}, {key_file}")
    
    logger.info(f"Starting Wakeify with HTTPS on port 443")
    # Start with HTTPS only
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=443,
        ssl_keyfile=str(key_file),
        ssl_certfile=str(cert_file),
        log_level="info",
        access_log=False
    )