import os
import json
import logging
import threading
import time
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Import the comprehensive playback engine
from alarm_playback import AlarmPlaybackEngine
from alarm_playback.config import AlarmPlaybackConfig, DeviceProfile, PlaybackMetrics

# Import APScheduler for better scheduling
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Import structured logging utilities
from alarm_playback.logging_utils import setup_logging

# Configure structured logging based on environment variables
log_level = os.getenv("LOG_LEVEL", "INFO")
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
sp_oauth = SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope="user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private",
    cache_path=TOKEN_FILE,
    open_browser=False,
    show_dialog=True
)

# Global variables
alarms: List[Dict[str, Any]] = []
devices: List[Dict[str, Any]] = []
spotify = None
alarm_thread = None
scheduler = None
running = True
alarm_config = None

# Performance: Device cache
device_cache = None
device_cache_timestamp = None
device_cache_ttl = 300  # Cache for 5 minutes (increased from 60s)

# Performance: Playlist cache
playlist_cache = None
playlist_cache_timestamp = None
playlist_cache_ttl = 300  # Cache playlists for 5 minutes

def get_spotify_client():
    """Get authenticated Spotify client."""
    global spotify
    
    if spotify is None:
        try:
            token_info = sp_oauth.get_cached_token()
            if not token_info:
                return None
            
            spotify = spotipy.Spotify(auth_manager=sp_oauth)
        except Exception as e:
            logger.error(f"Error getting Spotify client: {e}")
            return None
    
    return spotify

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
        logger.info(f"Saved {len(alarms)} alarm(s) to {ALARMS_FILE}")
        with open(DEVICES_FILE, 'w') as f:
            json.dump(devices, f, indent=2)
        logger.debug(f"Saved {len(devices)} device(s) to {DEVICES_FILE}")
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
        logger.info("Using comprehensive playback engine with T-60s timeline")
        
        # Try to get existing device profile first, but don't create new ones via mDNS
        # Let the orchestrator handle Web API discovery first
        device_profile = None
        for profile in alarm_config.targets:
            if profile.name == target_device_name:
                device_profile = profile
                break
        
        # If no profile found, try to discover the device via mDNS and create profile with IP
        if not device_profile:
            logger.info(f"Device {target_device_name} not found in registry, attempting mDNS discovery")
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
                    except Exception as e:
                        logger.debug(f"Error extracting name for device: {e}")
                    
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
                        logger.info(f"✓ Discovered device via mDNS: friendly_name='{dev_name}', instance_name='{dev.instance_name}' (matched alarm target '{target_device_name}')")
                        logger.info(f"  Device profile created at {dev.ip}:{dev.port}, cpath={dev.cpath}")
                        break
                
                # If still no profile, create minimal one
                if not device_profile:
                    logger.info(f"Could not discover {target_device_name} via mDNS, creating minimal profile")
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
            logger.info(f"Added device {target_device_name} to orchestrator targets with volume {alarm_volume}%")
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
                logger.debug(f"Saved device profile for {target_device_name}")
            except Exception as e:
                logger.debug(f"Could not save device profile: {e}")
        
        # Update context URI in config
        alarm_config.context_uri = playlist_uri
        
        # Update shuffle setting
        alarm_shuffle = alarm.get("shuffle", False)
        alarm_config.shuffle = alarm_shuffle
        
        # Create playback engine
        engine = AlarmPlaybackEngine(alarm_config)
        
        # Execute full timeline
        logger.info(f"Starting T-60s timeline for {target_device_name}")
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
            logger.debug(f"Saved device profiles after alarm execution")
        except Exception as e:
            logger.debug(f"Could not save device profiles after alarm: {e}")
        
        # Log results
        logger.info(f"Alarm execution completed:")
        logger.info(f"  Branch: {metrics.branch}")
        logger.info(f"  Total duration: {metrics.total_duration_ms}ms")
        logger.info(f"  Discovery: {metrics.discovered_ms}ms")
        logger.info(f"  GetInfo: {metrics.getinfo_ms}ms")
        logger.info(f"  AddUser: {metrics.adduser_ms}ms")
        logger.info(f"  Cloud visible: {metrics.cloud_visible_ms}ms")
        logger.info(f"  Play: {metrics.play_ms}ms")
        
        if metrics.errors:
            logger.warning(f"Errors encountered: {len(metrics.errors)}")
            for error in metrics.errors:
                logger.warning(f"  - {error['error']} (phase: {error.get('phase', 'unknown')})")
        
            
    except Exception as e:
        logger.error(f"Error running alarm {alarm.get('id', 'unknown')}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")

def alarm_monitor():
    """Monitor and execute alarms."""
    global running
    triggered_alarms = {}  # Track when alarms were triggered (alarm_id -> date string)
    active_playback = {}  # Track which alarms are currently playing (alarm_id -> thread)
    current_date = ""  # Track current date to reset triggered_alarms daily
    
    while running:
        try:
            current_time = datetime.now()
            today = current_time.date().isoformat()
            current_time_only = current_time.time()
            
            # Reset triggered alarms if we've moved to a new day
            if today != current_date:
                logger.info(f"New day: {today}. Resetting triggered alarms.")
                triggered_alarms = {}
                active_playback = {}
                current_date = today
            
            for alarm in alarms[:]:  # Copy list to avoid modification during iteration
                if not alarm.get('active', True):
                    continue
                
                alarm_id = alarm['id']
                
                # Check stop time if alarm has stop time and is playing
                stop_hour = alarm.get('stop_hour')
                stop_minute = alarm.get('stop_minute')
                if stop_hour is not None and stop_minute is not None and alarm_id in active_playback:
                    stop_time = datetime.strptime(f"{stop_hour:02d}:{stop_minute:02d}", '%H:%M').time()
                    time_diff_stop = abs((datetime.combine(current_time.date(), stop_time) - 
                                        datetime.combine(current_time.date(), current_time_only)).total_seconds())
                    
                    if time_diff_stop <= 10:  # Within 10 seconds of stop time
                        logger.info(f"Stop time reached for alarm {alarm_id} at {stop_hour:02d}:{stop_minute:02d}, stopping playback")
                        # Stop playback
                        sp = get_spotify_client()
                        if sp:
                            try:
                                sp.pause_playback()
                                logger.info(f"Successfully paused playback for alarm {alarm_id}")
                            except Exception as e:
                                logger.error(f"Failed to pause playback: {e}")
                        # Remove from active playback
                        active_playback.pop(alarm_id, None)
                        logger.info(f"Removed alarm {alarm_id} from active playback tracking")
                        continue
                
                # Check if alarm should trigger
                alarm_hour = alarm.get('hour', 0)
                alarm_minute = alarm.get('minute', 0)
                alarm_time = datetime.strptime(f"{alarm_hour:02d}:{alarm_minute:02d}", '%H:%M').time()
                
                # Check if it's the right day of week
                current_dow = current_time.strftime('%a').lower()
                dow_string = alarm.get('dow', '')
                if current_dow not in dow_string.lower():
                    continue
                
                # Debug: log time check
                logger.info(f"Checking alarm {alarm_id}: current={current_time.strftime('%H:%M:%S')}, target={alarm_hour:02d}:{alarm_minute:02d}")
                
                # Check if alarm should trigger (within 10 seconds to be more precise)
                time_diff = abs((datetime.combine(current_time.date(), alarm_time) - 
                               datetime.combine(current_time.date(), current_time_only)).total_seconds())
                
                if time_diff <= 10:  # Within 10 seconds for precision
                    # Check if this alarm was already triggered today
                    if alarm_id in triggered_alarms and triggered_alarms[alarm_id] == today:
                        continue  # Already triggered today, skip
                    
                    logger.info(f"Alarm {alarm_id} triggered!")
                    
                    # Mark alarm as triggered for today
                    triggered_alarms[alarm_id] = today
                    
                    # Run alarm in separate thread
                    alarm_thread = threading.Thread(target=run_alarm, args=(alarm,))
                    alarm_thread.daemon = True
                    alarm_thread.start()
                    active_playback[alarm_id] = alarm_thread
                    
                    # Remove alarm if it's a one-time alarm
                    if not alarm.get('recurring', True):
                        alarms.remove(alarm)
                        save_data()
            
            time.sleep(10)  # Check every 10 seconds
            
        except Exception as e:
            logger.error(f"Error in alarm monitor: {e}")
            time.sleep(30)

def prewarm_device(alarm: Dict[str, Any]) -> None:
    """Prewarm device 60 seconds before alarm time to wake it up."""
    try:
        alarm_id = alarm.get('id')
        target_device_name = alarm.get("device_name", DEFAULT_SPEAKER)
        logger.info(f"Prewarming device {target_device_name} for alarm {alarm_id}")
        
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
                        logger.info(f"Discovered device {dev.instance_name} for prewarm")
                        break
            except Exception as e:
                logger.debug(f"Could not discover device during prewarm: {e}")
        
        # Wake up device if we have IP
        if device_profile and device_profile.ip:
            try:
                from alarm_playback.fallback import _wake_device_via_ip
                logger.info(f"Waking device {target_device_name} via IP {device_profile.ip}")
                _wake_device_via_ip(
                    device_profile.ip,
                    device_profile.port or 80,
                    device_profile.cpath or "/spotifyconnect/zeroconf",
                    target_device_name
                )
                logger.info(f"Successfully prewarmed device {target_device_name}")
            except Exception as e:
                logger.warning(f"Prewarm failed for {target_device_name}: {e}")
        else:
            logger.debug(f"No IP available for prewarm of {target_device_name}")
            
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
            logger.info(f"Scheduled prewarm for alarm {alarm_id} at {prewarm_hour:02d}:{prewarm_minute:02d} on {dow}")
            
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
            logger.info(f"Scheduled alarm {alarm_id} for {hour:02d}:{minute:02d} on {dow}")
            
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
                logger.info(f"Scheduled stop for alarm {alarm_id} at {stop_hour:02d}:{stop_minute:02d}")
        except Exception as e:
            logger.error(f"Failed to schedule alarm {alarm_id}: {e}")

def stop_alarm_playback(alarm_id: str):
    """Stop playback for a specific alarm."""
    logger.info(f"Stop time reached for alarm {alarm_id}, stopping playback")
    
    # Stop any active AirPlay playback
    try:
        from alarm_playback.fallback import stop_all_airplay_playback
        stop_all_airplay_playback()
        logger.info("Stopped all AirPlay playback")
    except Exception as e:
        logger.error(f"Error stopping AirPlay playback: {e}")
    
    # Stop regular Spotify playback
    sp = get_spotify_client()
    if sp:
        try:
            sp.pause_playback()
            logger.info(f"Successfully paused Spotify playback for alarm {alarm_id}")
        except Exception as e:
            logger.error(f"Failed to pause Spotify playback: {e}")
    
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
                    devices = sp.devices()
                    spotify_device_names = {d['name'].upper() for d in devices.get('devices', [])}
                    
                    # Check each device in targets
                    for device in alarm_config.targets:
                        if not device.ip:
                            continue
                            
                        device_found = any(device.name.upper() in d['name'].upper() or d['name'].upper() in device.name.upper() 
                                          for d in devices.get('devices', []))
                        
                        if not device_found:
                            logger.debug(f"{device.name} not in Spotify devices - attempting background registration")
                            
                            try:
                                from alarm_playback.fallback import _mdns_auth_user_registration
                                _mdns_auth_user_registration(device.ip, device.name)
                            except Exception as e:
                                logger.debug(f"Background registration attempt failed for {device.name}: {e}")
                            
                except Exception as e:
                    logger.debug(f"Error checking devices in background task: {e}")
            
            time.sleep(60)  # Check every minute
            
        except Exception as e:
            logger.error(f"Error in background device registration: {e}")
            time.sleep(60)

def background_cache_refresh():
    """Refresh device cache in background every 120s to keep it warm."""
    global device_cache, device_cache_timestamp, alarm_config, running
    
    while running:
        try:
            time.sleep(120)  # Wait 120 seconds between refreshes (increased from 50s)
            
            # Skip if cache is still fresh (within 280s)
            if device_cache_timestamp and (time.time() - device_cache_timestamp) < 280:
                logger.debug("Cache still fresh, skipping refresh")
                continue
            
            # Skip if alarm_config not initialized
            if not alarm_config:
                logger.debug("Alarm config not initialized, skipping cache refresh")
                continue
            
            # Call the cache refresh logic synchronously
            try:
                from alarm_playback.zeroconf_client import check_device_health
                from alarm_playback.discovery import discover_all_connect_devices
                
                devices_list = []
                device_names_seen = set()
                
                # Add devices from config - refresh names from getInfo
                if alarm_config.targets:
                    for device in alarm_config.targets:
                        # Try to get fresh friendly name from getInfo (overrides saved name)
                        fresh_name = device.name  # Default to saved name
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
                            # If we got a better name from getInfo, update the saved device profile
                            elif fresh_name != device.name:
                                try:
                                    # Update the device profile with the fresh name
                                    device.name = fresh_name
                                    device_data = {
                                        "devices": [device.model_dump() for device in alarm_config.targets],
                                        "last_updated": str(time.time())
                                    }
                                    with open(DEVICES_FILE, 'w') as f:
                                        json.dump(device_data, f, indent=2)
                                except Exception as e:
                                    logger.debug(f"Could not save updated name for {device.name}: {e}")
                        except Exception as e:
                            logger.debug(f"Could not refresh name for {device.name} from getInfo: {e}")
                            fresh_name = device.name  # Fallback to saved name
                        
                        health_info = check_device_health(device.ip, device.port, device.cpath, timeout_s=0.1)
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
                
                # Add mDNS devices - use DeviceRegistry to get names from device properties (getInfo)
                mdns_devices = discover_all_connect_devices(1.0)
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
                            if not dev_name:
                                logger.info(f"_extract_friendly_name returned None for {dev.instance_name} ({dev.ip}:{dev.port})")
                    except Exception as e:
                        logger.warning(f"Error extracting friendly name for {dev.instance_name}: {e}")
                        import traceback
                        logger.debug(f"Traceback: {traceback.format_exc()}")
                    
                    # Fallback to instance_name if extraction failed
                    if not dev_name:
                        dev_name = dev.instance_name or f"Device at {dev.ip}"
                    
                    if dev_name not in device_names_seen and dev.ip and dev.port:
                        health_info = check_device_health(dev.ip, dev.port, dev.cpath or "/", timeout_s=0.1)
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
                        spotify_devices = sp.devices()
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
                    except Exception as e:
                        logger.debug(f"Failed to get Spotify devices: {e}")
                
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
            playlists_response = sp.current_user_playlists(limit=50)
            playlists = playlists_response.get('items', [])
            playlist_cache = playlists
            playlist_cache_timestamp = time.time()
    except Exception as e:
        logger.error(f"Error getting playlists: {e}")
    
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
                    health_info = check_device_health(device.ip, device.port, device.cpath or "/", timeout_s=0.1)
                    
                    # Use DeviceRegistry to get name from device properties (getInfo)
                    device_name = None
                    try:
                        if alarm_config:
                            import sys
                            sys.path.insert(0, str(APP_DIR))
                            from device_registry import DeviceRegistry
                            device_registry = DeviceRegistry(alarm_config)
                            device_name = device_registry._extract_friendly_name(device)
                    except Exception as e:
                        logger.debug(f"Error extracting friendly name: {e}")
                    
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
                devices_response = sp.devices()
                for dev in devices_response.get('devices', []):
                    dev_name = dev.get('name', 'Unknown')
                    if dev_name.upper() not in mdn_device_names:
                        all_devices.append({
                            "name": dev_name,
                            "ip": None,
                            "is_online": dev.get('is_active', False)
                        })
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
    try:
        sp = get_spotify_client()
        if not sp:
            raise HTTPException(status_code=401, detail="Not authenticated with Spotify")
        
        playlists_response = sp.current_user_playlists(limit=50)
        return {"playlists": playlists_response.get('items', [])}
    except Exception as e:
        logger.error(f"Error getting playlists: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/spotify/devices")
async def get_spotify_devices():
    """Get all available Spotify Connect devices."""
    try:
        sp = get_spotify_client()
        if not sp:
            raise HTTPException(status_code=401, detail="Not authenticated with Spotify")
        
        devices = sp.devices()
        return {"devices": devices.get('devices', [])}
    except Exception as e:
        logger.error(f"Error getting Spotify devices: {e}")
        raise HTTPException(status_code=500, detail=str(e))

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
            logger.debug("Returning cached device list")
            return device_cache
        
        # Get devices from config AND Spotify
        devices_list = []
        device_names_seen = set()
        
        # First, add devices from config - but refresh names from getInfo
        if alarm_config.targets:
            logger.debug(f"Using {len(alarm_config.targets)} devices from config")
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
                        except Exception as e:
                            logger.debug(f"Could not save updated name for {device.name}: {e}")
                except Exception as e:
                    logger.debug(f"Could not refresh name for {device.name} from getInfo: {e}")
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
                        if not dev_name:
                            logger.debug(f"getInfo failed for {dev.instance_name} ({dev.ip}), using instance name")
                except Exception as e:
                    logger.error(f"Error extracting friendly name for {dev.instance_name}: {e}")
                
                # Fallback to instance_name if extraction failed
                if not dev_name:
                    dev_name = dev.instance_name or f"Device at {dev.ip}"
                    logger.debug(f"Using fallback name '{dev_name}' for {dev.instance_name}")
                
                # Only add if not already in list
                if dev_name not in device_names_seen:
                    # Check if device is online (skip health check for cached results)
                    if dev.ip and dev.port:
                        health_info = check_device_health(
                            dev.ip, dev.port, dev.cpath or "/", timeout_s=0.1  # Optimized for speed
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
                spotify_devices = sp.devices()
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
        except Exception as e:
            logger.debug(f"Failed to get Spotify devices: {e}")
        
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
        global spotify
        
        # Delete token file
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        
        # Clear global spotify client
        spotify = None
        
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
            current = sp.current_playback()
            if not current or not current.get('is_playing'):
                return {"status": "info", "message": "Nothing is playing"}
        except Exception as e:
            # If we can't check, just try to stop
            logger.debug(f"Could not check playback status: {e}")
        
        # Stop playback
        sp.pause_playback()
        logger.info("Stopped current playback")
        return {"status": "success", "message": "Playback stopped"}
    except spotipy.SpotifyException as e:
        # Handle Spotify API errors gracefully
        if e.http_status == 404 or "NO_ACTIVE_DEVICE" in str(e):
            return {"status": "info", "message": "Nothing is playing"}
        logger.error(f"Error stopping playback: {e}")
        raise HTTPException(status_code=500, detail="Failed to stop playback")
    except Exception as e:
        logger.error(f"Error stopping playback: {e}")
        raise HTTPException(status_code=500, detail="Failed to stop playback")

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
        token_info = sp_oauth.get_access_token(code)
        
        # Save token to file
        with open(TOKEN_FILE, 'w') as f:
            json.dump(token_info, f)
        
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