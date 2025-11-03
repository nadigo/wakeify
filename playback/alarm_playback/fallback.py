_active_airplay_players = {}
"""
Fallback mechanisms for alarm playback when primary device fails
"""

import logging
import subprocess
import time
import os
import tempfile
import threading
import requests
from typing import List, Optional, Dict, Any
from pathlib import Path

from .config import AirPlayConfig, FallbackConfig
from .spotify_api import SpotifyApiWrapper
from .zeroconf_client import get_info

logger = logging.getLogger(__name__)


def _wake_device_via_ip(device_ip: str, device_port: int, device_cpath: str, device_name: str) -> bool:
    """
    Generic IP-based wake-up for all speakers.
    Tries multiple methods to wake device via its IP address.
    
    Methods tried in order:
    1. HTTP getInfo call to Spotify Connect endpoint
    2. mDNS queries for Spotify Connect services
    3. HTTP requests to common ports
    4. ICMP ping packets
    
    Args:
        device_ip: Device IP address
        device_port: Device port (default 80 if not specified)
        device_cpath: Device CPath (e.g., "/spotifyconnect/zeroconf")
        device_name: Device name for logging
        
    Returns:
        True if any method succeeds, False otherwise
    """
    logger.info(f"Attempting generic IP wake-up for {device_name} at {device_ip}:{device_port}")
    
    success = False
    method_used = None
    
    # Method 1: HTTP getInfo call to Spotify Connect endpoint
    try:
        if device_cpath:
            logger.debug(f"Method 1: Trying HTTP getInfo call to {device_ip}:{device_port}{device_cpath}")
            if get_info(device_ip, device_port, device_cpath, timeout_s=2.0):
                success = True
                method_used = "HTTP getInfo"
                logger.info(f"Generic IP wake-up succeeded for {device_name} via HTTP getInfo")
    except Exception as e:
        logger.debug(f"HTTP getInfo failed: {e}")
    
    if success:
        return True
    
    # Method 2: mDNS queries for Spotify Connect services
    try:
        logger.debug(f"Method 2: Sending mDNS queries for {device_name}")
        mdns_queries = [
            "_spotify-connect._tcp.local",
            "_airplay._tcp.local",
            "_raop._tcp.local"
        ]
        
        def send_mdns_query(service):
            try:
                cmd = ["avahi-browse", "-t", service, "--resolve", "--terminate"]
                subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            except Exception:
                pass
        
        threads = []
        for service in mdns_queries:
            thread = threading.Thread(target=send_mdns_query, args=(service,))
            thread.start()
            threads.append(thread)
        
        for thread in threads:
            thread.join(timeout=2)
        
        # Small delay to let mDNS take effect
        time.sleep(0.5)
        method_used = "mDNS queries"
        logger.debug(f"Sent mDNS queries for {device_name}")
    except Exception as e:
        logger.debug(f"mDNS queries failed: {e}")
    
    # Method 3: HTTP pings to common ports
    if not success:
        try:
            logger.debug(f"Method 3: Trying HTTP pings to common ports for {device_name}")
            common_ports = [80, 8080, 4070, 4071, 4072]
            
            for port in common_ports:
                try:
                    url = f"http://{device_ip}:{port}/"
                    response = requests.get(url, timeout=1.0)
                    if response.status_code in [200, 404, 400]:  # Any response means device is awake
                        success = True
                        method_used = f"HTTP ping port {port}"
                        logger.info(f"Generic IP wake-up succeeded for {device_name} via HTTP ping on port {port}")
                        break
                except requests.exceptions.RequestException:
                    continue
        except Exception as e:
            logger.debug(f"HTTP pings failed: {e}")
    
    # Method 4: ICMP ping packets
    if not success:
        try:
            logger.debug(f"Method 4: Sending ICMP ping to {device_ip} for {device_name}")
            result = subprocess.run(
                ["ping", "-c", "2", "-W", "1", device_ip],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                success = True
                method_used = "ICMP ping"
                logger.info(f"Generic IP wake-up succeeded for {device_name} via ICMP ping")
        except Exception as e:
            logger.debug(f"ICMP ping failed: {e}")
    
    if success:
        logger.info(f"Generic IP wake-up succeeded for {device_name} using method: {method_used}")
    else:
        logger.debug(f"All generic IP wake-up methods failed for {device_name}")
    
    return success


def play_on_spotifyd(api: SpotifyApiWrapper, device_name: str, context_uri: str) -> None:
    """
    Find and play on always-online spotifyd device.
    
    Args:
        api: Spotify API wrapper instance
        device_name: Name of the spotifyd device
        context_uri: URI to play
    """
    logger.info(f"Attempting fallback to spotifyd device: {device_name}")
    
    try:
        # Get available devices
        devices = api.get_devices()
        
        # Find the spotifyd device
        spotifyd_device = None
        for device in devices:
            if device.name == device_name:
                spotifyd_device = device
                break
        
        if not spotifyd_device:
            raise ValueError(f"spotifyd device '{device_name}' not found in available devices")
        
        logger.info(f"Found spotifyd device: {spotifyd_device.name} ({spotifyd_device.id})")
        
        # Transfer playback and start playing
        api.put_transfer(device_id=spotifyd_device.id, play=True)
        api.put_play(device_id=spotifyd_device.id, context_uri=context_uri)
        
        logger.info(f"Successfully started playback on spotifyd device {device_name}")
        
    except Exception as e:
        logger.error(f"Failed to play on spotifyd device {device_name}: {e}")
        raise


def airplay_start_via_raop(ips: List[str], pcm_source_cmd: List[str], 
                          volume_percent: int = 50) -> None:
    """
    Launch PCM source and pipe to raop_play for AirPlay fallback.
    
    Args:
        ips: List of AirPlay target IP addresses
        pcm_source_cmd: Command to generate PCM audio
        volume_percent: Volume level for AirPlay output
    """
    logger.info(f"Starting AirPlay fallback via raop_play to {len(ips)} targets")
    
    if not ips:
        raise ValueError("No AirPlay target IPs provided")
    
    processes = []
    
    try:
        # Start PCM source process
        logger.info(f"Starting PCM source: {' '.join(pcm_source_cmd)}")
        pcm_process = subprocess.Popen(
            pcm_source_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Start raop_play processes for each target
        for ip in ips:
            logger.info(f"Starting raop_play for target {ip}")
            
            # Build raop_play command
            raop_cmd = [
                "raop_play",
                "-i", ip,
                "-v", str(volume_percent),
                "-"
            ]
            
            raop_process = subprocess.Popen(
                raop_cmd,
                stdin=pcm_process.stdout,
                stderr=subprocess.PIPE,
                text=True
            )
            
            processes.append((raop_process, ip))
        
        # Close PCM process stdout to allow it to finish
        pcm_process.stdout.close()
        
        # Wait for processes to complete
        logger.info("Waiting for AirPlay processes to complete...")
        
        # Monitor processes
        start_time = time.time()
        timeout = 30  # 30 second timeout
        
        while time.time() - start_time < timeout:
            all_finished = True
            
            for process, ip in processes:
                if process.poll() is None:  # Still running
                    all_finished = False
                else:
                    # Process finished
                    if process.returncode != 0:
                        stderr = process.stderr.read()
                        logger.error(f"raop_play failed for {ip}: {stderr}")
                    else:
                        logger.info(f"raop_play completed successfully for {ip}")
            
            if all_finished:
                break
            
            time.sleep(0.5)
        
        if not all_finished:
            logger.warning("AirPlay processes did not complete within timeout")
            # Kill remaining processes
            for process, ip in processes:
                if process.poll() is None:
                    logger.info(f"Terminating raop_play process for {ip}")
                    process.terminate()
        
        logger.info("AirPlay fallback completed")
        
    except Exception as e:
        logger.error(f"AirPlay fallback failed: {e}")
        # Clean up processes
        for process, ip in processes:
            if process.poll() is None:
                process.terminate()
        raise


def airplay_fallback(cfg: FallbackConfig, target_ips: List[str] = None, spotify_api=None, target_device_name: str = None, playlist_uri: str = None, device_profile=None) -> None:
    """
    Comprehensive fallback sequence - ALWAYS uses user's selected playlist and NEVER switches devices.
    
    Sequence:
    1. Quick Check: Try Spotify Connect on target device
    2. Generic IP Wake-up: Wake device via IP (HTTP getInfo, mDNS, HTTP ping, ICMP ping)
    3. Generic mDNS Wake-up: Additional mDNS wake-up attempts
    4. mDNS Auth: mDNS auth user registration on Spotify
    5. Force Connection: Spotify play from device list (same device)
    6. AirPlay Fallback: Try AirPlay as final resort
    7. Final Failure: Log error if all methods fail
    
    Args:
        cfg: Fallback configuration
        target_ips: Optional list of target IPs (device IP)
        spotify_api: Spotify API instance for real playback
        target_device_name: Name of the target device to wake up and play on
        playlist_uri: The actual playlist URI from the alarm (e.g., spotify:playlist:3YQeSK4D1GrGlUv9NXuwWX)
    """
    logger.info(f"Starting comprehensive fallback sequence for {target_device_name} with playlist: {playlist_uri}")
    
    if not target_ips:
        logger.warning("No target IPs provided for fallback")
        return
    
    if not target_device_name:
        logger.warning("No target device name provided for fallback")
        return
    
    if not playlist_uri:
        logger.error("No playlist URI provided - cannot proceed with fallback")
        raise RuntimeError("No playlist URI provided for fallback")
    
    try:
        # Use provided Spotify API instance for real playback
        if spotify_api is None:
            logger.error("No Spotify API instance provided for fallback")
            raise RuntimeError("No Spotify API instance available")
        
        spotify = spotify_api
        
        # Validate target IP
        if not target_ips or len(target_ips) == 0:
            logger.error("No target IP provided for fallback")
            raise RuntimeError("No target IP provided for fallback")
        target_ip = target_ips[0]
        
        # STEP 1: Quick Check - Try Spotify Connect on target device
        logger.info(f"Step 1: Quick check - trying Spotify Connect on {target_device_name}")
        if device_profile:
            matching_names = device_profile.get_all_matching_names()
            logger.info(f"Fallback: Looking for '{target_device_name}' using stored names: {matching_names}")
        target_device = _wait_for_speaker_in_spotify_devices(spotify, target_device_name, max_attempts=2, delay_s=1, device_profile=device_profile)
        
        if target_device:
            logger.info(f"Step 1 SUCCESS: {target_device_name} found in Spotify devices")
            _play_on_spotify_device(spotify, target_device, playlist_uri, target_device_name)
            return
        
        # Discover device port and cpath for generic IP wake-up
        device_port = 80  # Default
        device_cpath = "/spotifyconnect/zeroconf"  # Default
        
        try:
            from alarm_playback.discovery import mdns_discover_connect
            discovery_result = mdns_discover_connect(target_device_name, timeout_s=1.5)
            if discovery_result.is_complete and discovery_result.port:
                device_port = discovery_result.port
            if discovery_result.is_complete and discovery_result.cpath:
                device_cpath = discovery_result.cpath
        except Exception as e:
            logger.debug(f"Could not discover device details, using defaults: {e}")
        
        # STEP 2: Generic IP Wake-up
        logger.info(f"Step 2: {target_device_name} not found, attempting generic IP wake-up")
        _wake_device_via_ip(target_ip, device_port, device_cpath, target_device_name)
        
        # Wait for wake-up to take effect
        target_device = _wait_for_speaker_in_spotify_devices(spotify, target_device_name, max_attempts=3, delay_s=2, device_profile=device_profile)
        
        if target_device:
            logger.info(f"Step 2 SUCCESS: {target_device_name} connected after generic IP wake-up")
            _play_on_spotify_device(spotify, target_device, playlist_uri, target_device_name)
            return
        
        # STEP 3: Generic mDNS Wake-up
        logger.info(f"Step 3: {target_device_name} still not found, attempting additional mDNS wake-up")
        _wake_up_speaker_via_mdns(target_ip, target_device_name)
        
        # Wait for wake-up to take effect
        target_device = _wait_for_speaker_in_spotify_devices(spotify, target_device_name, max_attempts=3, delay_s=2, device_profile=device_profile)
        
        if target_device:
            logger.info(f"Step 3 SUCCESS: {target_device_name} connected after mDNS wake-up")
            _play_on_spotify_device(spotify, target_device, playlist_uri, target_device_name)
            return
        
        # STEP 4: mDNS Auth
        logger.info(f"Step 4: {target_device_name} still not found, attempting mDNS auth user registration")
        _mdns_auth_user_registration(target_ip, target_device_name)
        
        # Refresh token after authentication to ensure we have a fresh token
        try:
            if hasattr(spotify, 'token_manager'):
                spotify.token_manager.refresh_token_if_needed()
                if hasattr(spotify, '_spotify'):
                    spotify._spotify = None  # Force recreation
                logger.debug("Refreshed token after mDNS auth registration")
        except Exception as e:
            logger.debug(f"Token refresh after mDNS auth failed (non-fatal): {e}")
        
        # Wait longer for auth registration to take effect (some devices need more time)
        logger.info(f"Waiting 5 seconds for {target_device_name} to register after mDNS auth...")
        time.sleep(5.0)
        
        # Wait for auth registration to take effect with more attempts
        target_device = _wait_for_speaker_in_spotify_devices(spotify, target_device_name, max_attempts=5, delay_s=2, device_profile=device_profile)
        
        if target_device:
            logger.info(f"Step 4 SUCCESS: {target_device_name} connected after mDNS auth registration")
            _play_on_spotify_device(spotify, target_device, playlist_uri, target_device_name)
            return
        
        # STEP 5: Force Connection
        logger.info(f"Step 5: {target_device_name} still not found, attempting to force connection via Spotify device list")
        target_device = _force_connect_to_target_device(spotify, target_device_name, target_ip, device_profile=device_profile)
        
        if target_device:
            logger.info(f"Step 5 SUCCESS: {target_device_name} connected via forced Spotify device list")
            _play_on_spotify_device(spotify, target_device, playlist_uri, target_device_name)
            return
        
        # STEP 6: AirPlay Fallback - Try AirPlay as final resort
        logger.info(f"Step 6: {target_device_name} still not available via Spotify Connect, attempting AirPlay fallback")
        try:
            _airplay_to_target_device(target_ip, playlist_uri, target_device_name, spotify_api=spotify)
            logger.info(f"Step 6 SUCCESS: {target_device_name} playing via AirPlay")
            return
        except Exception as e:
            logger.warning(f"Step 6 AirPlay fallback failed: {e}")
            # Continue to final failure
        
        # STEP 7: Final Failure - All methods failed
        logger.error(f"════════════════════════════════════════════════════════════════")
        logger.error(f"All playback methods failed for {target_device_name}")
        logger.error(f"")
        logger.error(f"Device could not be woken or authenticated")
        logger.error(f"")
        logger.error(f"TO FIX THIS:")
        logger.error(f"   1. Ensure device is powered on")
        logger.error(f"   2. Open Spotify app on your phone/computer")
        logger.error(f"   3. Look for '{target_device_name}' in available devices")
        logger.error(f"   4. Select it and play a song to authenticate")
        logger.error(f"   5. Then alarms will work!")
        logger.error(f"")
        logger.error(f"════════════════════════════════════════════════════════════════")
        raise RuntimeError(f"Cannot play on {target_device_name} - device not available")
            
    except Exception as e:
        # Final failure - Log error with helpful message
        if "Cannot play on" not in str(e):  # Avoid duplicate error messages
            logger.error(f"═══════════════════════════════════════════════════════════════")
            logger.error(f"All playback methods failed for {target_device_name}")
            logger.error(f"")
            logger.error(f"Device could not be woken or authenticated")
            logger.error(f"")
            logger.error(f"TO FIX THIS:")
            logger.error(f"   1. Ensure device is powered on")
            logger.error(f"   2. Open Spotify app on your phone/computer")
            logger.error(f"   3. Look for '{target_device_name}' in available devices")
            logger.error(f"   4. Select it and play a song to authenticate")
            logger.error(f"   5. Then alarms will work!")
            logger.error(f"")
            logger.error(f"═══════════════════════════════════════════════════════════════")
        raise RuntimeError(f"Cannot play on {target_device_name} - device not available")


def _wake_up_speaker_via_mdns(target_ip: str, device_name: str) -> None:
    """
    Wake up target speaker by sending mDNS queries (like Spotify app does).
    Optimized for faster execution.
    """
    import subprocess
    import time
    import threading
    
    logger.debug(f"Sending mDNS wake-up queries to {target_ip} for {device_name}")
    
    try:
        # Send mDNS queries for Spotify Connect services in parallel for speed
        mdns_queries = [
            "_spotify-connect._tcp.local",
            "_airplay._tcp.local", 
            "_raop._tcp.local"
        ]
        
        def send_mdns_query(service):
            try:
                cmd = ["avahi-browse", "-t", service, "--resolve", "--terminate"]
                subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except Exception:
                pass
        
        # Send mDNS queries in parallel
        threads = []
        for service in mdns_queries:
            thread = threading.Thread(target=send_mdns_query, args=(service,))
            thread.start()
            threads.append(thread)
        
        # Also ping the device to wake it up
        def ping_device():
            try:
                subprocess.run(["ping", "-c", "2", target_ip], capture_output=True, text=True, timeout=5)
            except Exception:
                pass
        
        ping_thread = threading.Thread(target=ping_device)
        ping_thread.start()
        threads.append(ping_thread)
        
        # Wait for all threads to complete (max 5 seconds)
        for thread in threads:
            thread.join(timeout=5)
        
        logger.debug(f"mDNS wake-up queries completed for {device_name}")
        
    except Exception as e:
        logger.error(f"Failed to send mDNS wake-up queries for {device_name}: {e}")
        raise


def _wait_for_speaker_in_spotify_devices(spotify, device_name: str, max_attempts: int = 10, delay_s: float = 2.0, device_profile=None):
    """
    Wait for target speaker to appear in Spotify's device list after wake-up.
    Uses exact matching against stored name mapping (friendly name, instance name, Spotify device names).
    No pattern matching - only exact matches from stored mapping.
    """
    if not device_name or not device_name.strip():
        logger.warning("Empty device name provided")
        return None
    
    logger.debug(f"Waiting for {device_name} to appear in Spotify devices (max {max_attempts} attempts)")
    
    # Get all stored names that should match this device
    if device_profile and hasattr(device_profile, 'get_all_matching_names'):
        matching_names = device_profile.get_all_matching_names()
    else:
        matching_names = [device_name]
    
    # Create case-insensitive lookup set
    matching_names_set = {name.lower().strip() for name in matching_names if name}
    
    for attempt in range(max_attempts):
        try:
            devices = spotify.get_devices()
            if attempt == 0:
                # Always log first attempt with device names
                device_names = [d.name for d in devices] if devices else []
                logger.info(f"Fallback attempt {attempt + 1}: Spotify devices: {device_names}")
                logger.info(f"Fallback: Trying to match '{device_name}' against stored names: {matching_names}")
            elif attempt == max_attempts - 1:
                logger.debug(f"Attempt {attempt + 1}: Found {len(devices)} Spotify devices")
            
            # Look for target device using exact matching against stored names
            for device in devices:
                if not device.name:
                    continue
                device_name_normalized = device.name.lower().strip()
                if device_name_normalized in matching_names_set:
                    logger.info(f"Exact match found from stored mapping: {device.name} matches {device_name} (ID: {device.id}, tried names: {matching_names})")
                    # Store this Spotify device name for future exact matching
                    if device_profile and hasattr(device_profile, 'spotify_device_names'):
                        if device.name not in device_profile.spotify_device_names:
                            device_profile.spotify_device_names.append(device.name)
                            logger.info(f"✓ LEARNED: Stored Spotify device name '{device.name}' for device '{device_name}'")
                    return device
            
            if attempt < max_attempts - 1:
                time.sleep(delay_s)
            
        except Exception as e:
            logger.debug(f"Error checking Spotify devices (attempt {attempt + 1}): {e}")
            if attempt < max_attempts - 1:
                time.sleep(delay_s)
    
    logger.warning(f"{device_name} did not appear in Spotify devices after {max_attempts} attempts (tried names: {matching_names})")
    return None


def _play_on_spotify_device(spotify, device, playlist_uri: str, device_name: str) -> None:
    """
    Play user's selected playlist on a Spotify device.
    """
    try:
        # Transfer playback to target device
        spotify.put_transfer(device_id=device.id, play=False)
        logger.info(f"Transferred playback to {device_name}")
        
        # Play user's selected playlist
        spotify.put_play(device_id=device.id, context_uri=playlist_uri)
        logger.info(f"Started playback on {device_name} with playlist: {playlist_uri}")
        
        # Let it play for the full alarm duration (don't stop it manually)
        # The alarm system will handle stopping when the alarm duration expires
        logger.info(f"Playback started on {device_name} - will continue until alarm duration expires")
        
    except Exception as e:
        logger.error(f"Failed to play on {device_name}: {e}")
        raise


def _mdns_auth_user_registration(target_ip: str, device_name: str) -> None:
    """
    Step 3: mDNS auth user registration on Spotify (attempt proper addUser).
    """
    import subprocess
    import time
    
    logger.debug(f"Attempting mDNS auth user registration for {device_name} at {target_ip}")
    
    try:
        # First, discover the device to get port and cpath
        from alarm_playback.discovery import mdns_discover_connect
        result = mdns_discover_connect(device_name, timeout_s=2.0)
        
        if result.is_complete and result.ip:
            logger.debug(f"Discovered {device_name} at {result.ip}:{result.port}, cpath: {result.cpath}")
            
            # Try real addUser authentication with access_token mode
            try:
                from alarm_playback.zeroconf_client import add_user
                
                # Get access token from environment or Spotify API
                import os
                access_token = None
                try:
                    # Try to get access token from environment variables via SpotifyAuth
                    from alarm_playback.spotify_api import SpotifyApiWrapper, TokenManager
                    from alarm_playback.config import SpotifyAuth
                    auth_config = SpotifyAuth.from_env()
                    spotify_api = SpotifyApiWrapper(TokenManager(auth_config))
                    access_token = spotify_api.token_manager.get_access_token()
                except Exception:
                    pass
                
                if access_token:
                    creds = {
                        "accessToken": access_token
                    }
                    success = add_user(result.ip, result.port, result.cpath, "access_token", creds, timeout_s=2.5)
                    if success:
                        logger.info(f"Successfully authenticated {device_name} via addUser")
                        return
                    else:
                        logger.debug(f"addUser authentication failed for {device_name}")
                else:
                    logger.debug(f"Could not get access token for {device_name} authentication")
            except Exception as e:
                logger.debug(f"addUser failed for {device_name}: {e}")
        
        # Fallback: Send additional mDNS queries for authentication services
        auth_queries = [
            "_spotify-connect._tcp.local",
            "_spotify-user._tcp.local",
            "_spotify-auth._tcp.local"
        ]
        
        for service in auth_queries:
            try:
                cmd = ["avahi-browse", "-t", service, "--resolve", "--terminate"]
                subprocess.run(cmd, capture_output=True, text=True, timeout=3)
                time.sleep(0.5)
            except Exception:
                pass
        
        # Send HTTP requests to common Spotify Connect ports
        for port in [4070, 4071, 4072, 8080, 8081]:
            try:
                cmd = ["curl", "-s", "--connect-timeout", "2", f"http://{target_ip}:{port}/"]
                subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except Exception:
                pass
        
        logger.debug(f"mDNS auth user registration completed for {device_name}")
        
    except Exception as e:
        logger.error(f"Failed mDNS auth user registration for {device_name}: {e}")
        raise


def _force_connect_to_target_device(spotify, device_name: str, target_ip: str, device_profile=None):
    """
    Force connection to target device via Spotify device list.
    Uses exact matching against stored name mapping (friendly name, instance name, Spotify device names).
    No pattern matching - only exact matches from stored mapping.
    """
    if not device_name or not device_name.strip():
        logger.warning("Empty device name provided for force connect")
        return None
    
    logger.info(f"Attempting to force connect to {device_name} via Spotify device list")
    
    # Get all stored names that should match this device
    if device_profile and hasattr(device_profile, 'get_all_matching_names'):
        matching_names = device_profile.get_all_matching_names()
    else:
        matching_names = [device_name]
    
    # Create case-insensitive lookup set
    matching_names_set = {name.lower().strip() for name in matching_names if name}
    
    try:
        # Get all available devices
        devices = spotify.get_devices()
        logger.info(f"Available Spotify devices: {[d.name for d in devices]}")
        
        # Look for exact match using stored names only
        for device in devices:
            if not device.name:
                continue
            device_name_normalized = device.name.lower().strip()
            if device_name_normalized in matching_names_set:
                logger.info(f"Found exact match from stored mapping: {device.name} matches {device_name} (ID: {device.id}, tried names: {matching_names})")
                # Store this Spotify device name for future exact matching
                if device_profile and hasattr(device_profile, 'spotify_device_names'):
                    if device.name not in device_profile.spotify_device_names:
                        device_profile.spotify_device_names.append(device.name)
                        logger.info(f"✓ LEARNED: Stored Spotify device name '{device.name}' for device '{device_name}'")
                return device
        
        # If no match found, try to create a virtual connection
        logger.warning(f"No exact match found for {device_name} (tried names: {matching_names}) in Spotify devices")
        return None
        
    except Exception as e:
        logger.error(f"Failed to force connect to {device_name}: {e}")
        return None


def _airplay_to_target_device(target_ip: str, playlist_uri: str, device_name: str, spotify_api=None) -> None:
    """
    AirPlay to target device as FINAL RESORT - SPOTIFY PLAYLIST + AIRPLAY STREAMING.
    
    CRITICAL CONSTRAINTS (DO NOT VIOLATE):
    - Plays ONLY on target_device (target_ip parameter - never switches to other devices)
    - Plays ONLY user's selected playlist (playlist_uri parameter - NO test tones)
    - No fallback to other devices
    - If this fails, alarm fails completely (no further fallbacks)
    
    Implementation:
    1. Starts spotifyd with pipe backend (outputs PCM audio)
    2. Starts ffmpeg to stream PCM to AirPlay device via RTP
    3. Waits for spotifyd to appear in Spotify API
    4. Starts playlist playback on spotifyd (user's selected playlist)
    5. Audio flows: spotifyd → pipe → ffmpeg → AirPlay → target_device
    
    Args:
        target_ip: IP address of target device (from device profile - never switches)
        playlist_uri: User's selected playlist URI (e.g., spotify:playlist:...)
        device_name: Name of target device (for logging and spotifyd device name)
        spotify_api: Optional Spotify API instance (if not provided, will create one)
    """
    logger.info(f"Attempting AirPlay with Spotify playlist to {device_name} at {target_ip}")
    
    try:
        import subprocess
        import tempfile
        import os
        
        # Try actual Spotify playlist playback via AirPlay
        try:
            from alarm_playback.airplay_spotify import play_spotify_via_airplay
            from alarm_playback.spotify_api import SpotifyApiWrapper, TokenManager
            from alarm_playback.config import SpotifyAuth
            
            # Get access token - use provided API if available, otherwise create new one
            if spotify_api:
                token = spotify_api.token_manager.get_access_token()
            else:
                import os
                auth_config = SpotifyAuth.from_env()
                spotify_api = SpotifyApiWrapper(TokenManager(auth_config))
                token = spotify_api.token_manager.get_access_token()
            
            # Get volume from user settings (default to 20 for testing)
            import os
            volume = int(os.getenv("DEFAULT_VOLUME", "20"))
            
            # Use unique device name for spotifyd to avoid conflicts with real device
            # Format: "AirPlay-{device_name}" to make it easily identifiable
            spotifyd_device_name = f"AirPlay-{device_name}"
            
            # Try to play playlist via AirPlay
            success, player = play_spotify_via_airplay(
                target_ip=target_ip,
                playlist_uri=playlist_uri,
                access_token=token,
                device_name=spotifyd_device_name,
                volume=volume
            )
            
            if success:
                logger.info(f"Successfully started Spotify playback via AirPlay to {device_name}")
                # Store player instance for stopping later if needed
                global _active_airplay_players
                _active_airplay_players[device_name] = player
            else:
                logger.error("AirPlay Spotify playlist playback FAILED")
                raise RuntimeError("Failed to play user's chosen playlist on chosen speaker")
                
        except Exception as e:
            logger.error(f"Spotify playlist playback via AirPlay failed: {e}")
            logger.error("No fallback available - playlist playback only, no test tones")
            raise RuntimeError(f"Failed to play playlist on {device_name}: {e}")
        
    except Exception as e:
        logger.error(f"AirPlay failed for {device_name}: {e}")
        raise


def stop_airplay_playback(device_name: str) -> bool:
    """
    Stop AirPlay playback for a specific device.
    
    Args:
        device_name: Name of the device to stop
        
    Returns:
        True if stopped successfully, False if no active playback
    """
    global _active_airplay_players
                
    
    if device_name in _active_airplay_players:
        logger.info(f"Stopping AirPlay playback for {device_name}")
        try:
            player = _active_airplay_players[device_name]
            player.stop()
            del _active_airplay_players[device_name]
            logger.info(f"Successfully stopped AirPlay playback for {device_name}")
            return True
        except Exception as e:
            logger.error(f"Error stopping AirPlay playback for {device_name}: {e}")
            return False
    else:
        logger.info(f"No active AirPlay playback found for {device_name}")
        return False


def stop_all_airplay_playback() -> None:
    """Stop all active AirPlay playback"""
    global _active_airplay_players
                
    
    logger.info(f"Stopping all active AirPlay playback ({len(_active_airplay_players)} players)")
    
    for device_name, player in list(_active_airplay_players.items()):
        try:
            logger.info(f"Stopping AirPlay playback for {device_name}")
            player.stop()
        except Exception as e:
            logger.error(f"Error stopping AirPlay playback for {device_name}: {e}")
    
    _active_airplay_players.clear()
    logger.info("All AirPlay playback stopped")




def _try_raop_play(wav_file: str, target_ip: str, device_name: str) -> bool:
    """Try raop_play binary for AirPlay streaming."""
    try:
        import subprocess
        
        # Try to find raop_play binary
        raop_play_cmd = ["raop_play", "-a", target_ip, "-f", wav_file]
        logger.info(f"Trying raop_play: {' '.join(raop_play_cmd)}")
        
        result = subprocess.run(raop_play_cmd, capture_output=True, text=True, timeout=35)
        
        if result.returncode == 0:
            logger.info(f"raop_play succeeded for {device_name}")
            return True
        else:
            logger.warning(f"raop_play failed: {result.stderr}")
            return False
            
    except FileNotFoundError:
        logger.warning("raop_play binary not found")
        return False
    except Exception as e:
        logger.warning(f"raop_play error: {e}")
        return False


def _try_shairport_sync(wav_file: str, target_ip: str, device_name: str) -> bool:
    """Try shairport-sync for AirPlay streaming."""
    try:
        import subprocess
        
        # Convert WAV to raw PCM for shairport-sync
        pcm_file = wav_file.replace('.wav', '.pcm')
        
        # Convert WAV to raw PCM
        ffmpeg_cmd = [
            "ffmpeg", "-i", wav_file, "-f", "s16le", "-ar", "44100", "-ac", "2", pcm_file
        ]
        
        logger.info(f"Converting to PCM: {' '.join(ffmpeg_cmd)}")
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            logger.warning(f"ffmpeg conversion failed: {result.stderr}")
            return False
        
        # Try shairport-sync (correct syntax)
        shairport_cmd = [
            "shairport-sync", "-a", device_name, "-o", "stdout"
        ]
        
        logger.info(f"Trying shairport-sync: {' '.join(shairport_cmd)}")
        
        # Pipe PCM data to shairport-sync
        with open(pcm_file, 'rb') as pcm_data:
            result = subprocess.run(
                shairport_cmd, 
                stdin=pcm_data, 
                capture_output=True, 
                text=True, 
                timeout=35
            )
        
        # Clean up PCM file
        if os.path.exists(pcm_file):
            os.unlink(pcm_file)
        
        if result.returncode == 0:
            logger.info(f"shairport-sync succeeded for {device_name}")
            return True
        else:
            logger.warning(f"shairport-sync failed: {result.stderr}")
            return False
            
    except FileNotFoundError:
        logger.warning("shairport-sync binary not found")
        return False
    except Exception as e:
        logger.warning(f"shairport-sync error: {e}")
        return False


def _try_ffmpeg_rtp(wav_file: str, target_ip: str, device_name: str) -> bool:
    """Try ffmpeg RTP streaming to AirPlay 2 compatible devices."""
    try:
        import subprocess
        
        # AirPlay 2 uses RTSP over port 5004
        # Try different AirPlay/AirPlay 2 compatible formats
        airplay_configs = [
            # AirPlay 2 standard configuration
            {
                "format": "alsa",
                "args": ["-ar", "44100", "-ac", "2", "-f", "s16le"],
                "output": f"rtp://{target_ip}:5004",
                "name": "AirPlay 2 ALSA"
            },
            # Alternative AAC format for AirPlay
            {
                "format": "aac",
                "args": ["-ar", "44100", "-ac", "2", "-f", "mp4"],
                "output": f"rtp://{target_ip}:5004",
                "name": "AirPlay AAC"
            },
            # Original MP3 fallback
            {
                "format": "mp3",
                "args": ["-ar", "44100", "-ac", "2", "-f", "mp3"],
                "output": f"rtp://{target_ip}:5004",
                "name": "AirPlay MP3"
            }
        ]
        
        for config in airplay_configs:
            ffmpeg_cmd = [
                "ffmpeg", "-re", "-i", wav_file,
                *config["args"],
                config["output"]
            ]
            
            logger.info(f"Trying {config['name']} to {target_ip}:5004")
            
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=35)
            
            if result.returncode == 0:
                logger.info(f"{config['name']} succeeded for {device_name}")
                return True
            else:
                logger.debug(f"{config['name']} failed: {result.stderr[:200]}")
        
        logger.warning(f"All AirPlay 2 methods failed for {device_name}")
        return False
        
    except Exception as e:
        logger.warning(f"ffmpeg AirPlay 2 error: {e}")
        return False


def create_spotifyd_config(target_ip: str) -> str:
    """
    Create spotifyd configuration for Spotify Connect fallback.
    """
    return f"""[global]
# Use environment variables for Spotify credentials
username = "${{SPOTIFY_USERNAME}}"
password_cmd = "echo $SPOTIFY_PASSWORD"
device_name = "Alarm Fallback - {target_ip}"
device_type = "speaker"
bitrate = 320
cache_path = "/tmp/spotifyd_cache"
volume_controller = "alsa"
volume_normalisation = true
normalisation_pregain = -10

[audio]
backend = "alsa"
device = "default"
mixer = "PCM"
control = "Master"

[proxy]
enabled = false

[discovery]
enabled = true
name = "Alarm Fallback - {target_ip}"

[spotify]
# Use device-specific settings
device_name = "Alarm Fallback - {target_ip}"
"""


def check_spotifyd_available() -> bool:
    """
    Check if spotifyd is available on the system.
    """
    try:
        result = subprocess.run(["spotifyd", "--version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_ffmpeg_available() -> bool:
    """
    Check if ffmpeg is available on the system.
    """
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def check_raop_play_available() -> bool:
    """
    Check if raop_play is available on the system.
    
    Returns:
        True if raop_play is available, False otherwise
    """
    try:
        result = subprocess.run(["which", "raop_play"], 
                              capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def check_sox_available() -> bool:
    """
    Check if sox is available on the system for audio generation.
    
    Returns:
        True if sox is available, False otherwise
    """
    try:
        result = subprocess.run(["which", "sox"], 
                              capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def validate_airplay_setup(cfg: AirPlayConfig) -> Dict[str, bool]:
    """
    Validate AirPlay fallback setup.
    
    Args:
        cfg: AirPlay configuration
        
    Returns:
        Dictionary with validation results
    """
    results = {
        "raop_play_available": check_raop_play_available(),
        "sox_available": check_sox_available(),
        "target_ips_configured": len(cfg.raop_target_ips) > 0
    }
    
    if not results["raop_play_available"]:
        logger.warning("raop_play not found - AirPlay fallback will not work")
    
    if not results["sox_available"]:
        logger.warning("sox not found - test tone generation will not work")
    
    if not results["target_ips_configured"]:
        logger.warning("No AirPlay target IPs configured")
    
    return results


def create_audio_pipe(cfg: AirPlayConfig, audio_source_cmd: List[str]) -> subprocess.Popen:
    """
    Create a named pipe for audio streaming to multiple AirPlay targets.
    
    Args:
        cfg: AirPlay configuration
        audio_source_cmd: Command to generate audio
        
    Returns:
        Subprocess object for the audio source
    """
    # Create a temporary named pipe
    pipe_path = tempfile.mktemp(suffix='.pipe')
    os.mkfifo(pipe_path)
    
    try:
        # Start audio source process
        audio_process = subprocess.Popen(
            audio_source_cmd + [pipe_path],
            stderr=subprocess.PIPE,
            text=True
        )
        
        # Start raop_play processes reading from the pipe
        raop_processes = []
        for ip in cfg.raop_target_ips:
            raop_cmd = ["raop_play", "-i", ip, "-f", pipe_path]
            raop_process = subprocess.Popen(
                raop_cmd,
                stderr=subprocess.PIPE,
                text=True
            )
            raop_processes.append(raop_process)
        
        logger.info(f"Created audio pipe with {len(raop_processes)} AirPlay targets")
        return audio_process
        
    except Exception as e:
        # Clean up pipe
        if os.path.exists(pipe_path):
            os.unlink(pipe_path)
        raise

