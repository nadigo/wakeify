"""
Orchestrator for alarm playback timeline and state machine
"""

import logging
import time
from typing import Dict, Optional, List

from .config import AlarmPlaybackConfig, DeviceProfile
from .models import State, PhaseMetrics, CloudDevice, CircuitBreakerState
from .discovery import mdns_discover_connect
from .zeroconf_client import get_info, add_user
from .spotify_api import SpotifyApiWrapper, TokenManager
from .playback import stage_device, start_play, verify_device_ready
# Adapters module removed - not needed for current implementation
from .logging_utils import (
    get_logger, log_phase_start, log_phase_end, log_device_state_change,
    log_error, log_metrics
)

logger = get_logger(__name__)


class AlarmPlaybackEngine:
    """Main orchestrator for alarm playback with failover capabilities"""
    
    def __init__(self, cfg: AlarmPlaybackConfig):
        """
        Initialize alarm playback engine.
        
        Args:
            cfg: Alarm playback configuration
        """
        self.cfg = cfg
        self.token_manager = TokenManager(cfg.spotify)
        self.api = SpotifyApiWrapper(self.token_manager)
        
        # Circuit breakers for device failure tracking
        self.circuit_breakers: Dict[str, CircuitBreakerState] = {}
        
        # Device registry
        self._registry: Dict[str, DeviceProfile] = {}
        for device in cfg.targets:
            self._registry[device.name] = device
            self.circuit_breakers[device.name] = CircuitBreakerState(device.name)
        
        logger.info(f"Initialized alarm playback engine with {len(self._registry)} target devices")
    
    def _get_target_profile(self, target_name: str) -> DeviceProfile:
        """Get target device profile by name"""
        if target_name not in self._registry:
            raise ValueError(f"Target device '{target_name}' not found in registry")
        return self._registry[target_name]
    
    def _should_bypass_primary(self, target_name: str) -> bool:
        """Check if primary path should be bypassed due to circuit breaker"""
        if target_name not in self.circuit_breakers:
            # Create circuit breaker for unknown device
            self.circuit_breakers[target_name] = CircuitBreakerState(target_name)
        return self.circuit_breakers[target_name].should_bypass_primary()
    
    def _record_failure(self, target_name: str):
        """Record a failure for circuit breaker"""
        if target_name not in self.circuit_breakers:
            # Create circuit breaker for unknown device
            self.circuit_breakers[target_name] = CircuitBreakerState(target_name)
        self.circuit_breakers[target_name].record_failure()
        logger.warning(f"Recorded failure for device {target_name}")
    
    def _record_success(self, target_name: str):
        """Record a success for circuit breaker"""
        if target_name not in self.circuit_breakers:
            # Create circuit breaker for unknown device
            self.circuit_breakers[target_name] = CircuitBreakerState(target_name)
        self.circuit_breakers[target_name].record_success()
        logger.info(f"Recorded success for device {target_name}")
    
    def _needs_adduser(self, target: DeviceProfile) -> bool:
        """Always attempt addUser for all devices (generic approach)"""
        return True
    
    # _get_adduser_creds method removed - not needed for current implementation
    
    def _pick_device(self, devices: List[CloudDevice], target_name: str) -> Optional[CloudDevice]:
        """Pick the best matching device from available devices using stored name mapping (exact matching only)
        
        Uses the stored device profile to get all known names (friendly name, instance name, 
        Spotify device names) and matches exactly against Spotify's device list.
        No pattern matching - only exact matches from stored mapping.
        """
        if not devices or not target_name:
            return None
        
        # Get the device profile to access stored name mappings
        try:
            target_profile = self._get_target_profile(target_name)
            # Get all names that should match this device (from stored mapping)
            matching_names = target_profile.get_all_matching_names()
        except ValueError:
            # No profile found, use just the target name
            matching_names = [target_name]
        
        # Create case-insensitive lookup set
        matching_names_set = {name.lower().strip() for name in matching_names if name}
        
        # Try exact match (case-insensitive) against stored names only
        for device in devices:
            if not device.name:
                continue
            device_name_normalized = device.name.lower().strip()
            if device_name_normalized in matching_names_set:
                logger.debug(f"Exact match found from stored mapping: {device.name} matches one of {matching_names}")
                return device
        
        device_names = [d.name for d in devices] if devices else []
        logger.warning(f"No exact match found for {target_name} (tried names: {matching_names}) in Spotify devices: {device_names}")
        
        # If addUser succeeded but device still doesn't appear, provide helpful message
        if device_names and len(device_names) > 0:
            logger.warning(f"Device '{target_name}' was authenticated via addUser but doesn't appear in Spotify.")
            logger.warning(f"This usually means the device needs manual authentication first:")
            logger.warning(f"  1. Open Spotify app on your phone/computer")
            logger.warning(f"  2. Look for '{target_name}' in available devices")
            logger.warning(f"  3. Select it and play a song to authenticate")
            logger.warning(f"  4. Then alarms will work!")
        
        return None
    
    def _failover(self, metrics: PhaseMetrics, target_name: str, reason: str) -> PhaseMetrics:
        """Record failure when the primary Spotify Connect flow cannot recover."""
        metrics.branch = f"failed:{reason}"
        
        # Provide helpful error messages based on failure reason
        if reason == "not_in_devices_by_deadline":
            error_msg = (
                f"Device '{target_name}' did not appear in Spotify API within the deadline. "
                f"This usually means the device needs manual authentication first. "
                f"SOLUTION: Open Spotify app on your phone/computer, select '{target_name}' as the playback device, "
                f"and play any song to authenticate it. Then retry the alarm."
            )
        elif reason == "no_mdns":
            error_msg = (
                f"Device '{target_name}' could not be discovered on the network. "
                f"Check that the device is powered on and connected to the same network."
            )
        elif reason == "circuit_breaker_open":
            error_msg = (
                f"Device '{target_name}' has failed multiple times. "
                f"Circuit breaker is open. The device may need manual authentication or troubleshooting."
            )
        else:
            error_msg = (
                f"Alarm playback failed for '{target_name}' (reason: {reason}). "
                f"Fallback pipeline has been removed; the primary Spotify Connect flow must succeed."
            )
        
        metrics.add_error(error_msg, reason)
        logger.error(
            "Alarm failed for %s: primary branch failed (%s) and no secondary path is available.",
            target_name,
            reason,
        )
        logger.error("Error details: %s", error_msg)
        
        raise RuntimeError(
            f"Alarm playback failed (reason={reason}) and no fallback is available. {error_msg}"
        )
    
    def play_alarm(self, target_name: str) -> PhaseMetrics:
        """
        Run the wake-and-play timeline for the target device.
        
        Args:
            target_name: Name of the target device
            
        Returns:
            PhaseMetrics with timings and chosen branch
            
        Raises:
            Exception: Only for hard misconfiguration; otherwise tries fallback
        """
        logger.info(f"Starting alarm playback for device: {target_name}")
        
        metrics = PhaseMetrics()
        state = State.UNKNOWN
        
        total_start_time = time.time()
        
        try:
            # Phase 1: Check if device is already available via Web API (fastest path)
            log_phase_start(logger, "webapi_check", target_name)
            webapi_start = time.time()
            
            try:
                devices = self.api.get_devices(force_refresh=True)
                cloud_device = self._pick_device(devices, target_name)
                
                if cloud_device:
                    logger.info(f"Device {target_name} already available via Web API - skipping local discovery and addUser")
                    
                    # Store this Spotify device name for future exact matching
                    try:
                        target = self._get_target_profile(target_name)
                        if cloud_device.name not in target.spotify_device_names:
                            target.spotify_device_names.append(cloud_device.name)
                            logger.info(f"Learned and stored Spotify device name '{cloud_device.name}' for device '{target_name}'")
                    except ValueError:
                        pass  # Profile not found, skip storing
                    
                    metrics.discovered_ms = int((time.time() - webapi_start) * 1000)
                    log_phase_end(logger, "webapi_check", target_name, metrics.discovered_ms, True)
                    state = State.CLOUD_VISIBLE
                    
                    # Skip debounce - device is already active via Web API, no need to wait
                    
                    # Skip directly to staging and playback (use default volume if no profile)
                    try:
                        target = self._get_target_profile(target_name)
                        volume = target.volume_preset
                    except ValueError:
                        # Device not in registry, use default volume
                        volume = 30  # Default volume
                    
                    log_phase_start(logger, "stage", target_name)
                    stage_device(self.api, cloud_device.id, volume)
                    state = State.STAGED
                    log_phase_end(logger, "stage", target_name, None, True)
                    
                    # Start playback
                    log_phase_start(logger, "play", target_name)
                    play_start = time.time()
                    
                    start_play(self.api, cloud_device.id, self.cfg.context_uri,
                              retry_404_delay_s=self.cfg.timings.retry_404_delay_s,
                              shuffle=self.cfg.shuffle)
                    
                    metrics.play_ms = int((time.time() - play_start) * 1000)
                    log_phase_end(logger, "play", target_name, metrics.play_ms, True)
                    
                    # Warn if play took longer than expected
                    if metrics.play_ms > 1000:
                        logger.warning(f"Play phase took {metrics.play_ms}ms (expected <1000ms) - network may be slow")
                    
                    # Confirm playback started (like other paths do)
                    logger.info(f"Playback started, confirming within {self.cfg.timings.failover_fire_after_s}s...")
                    confirmation_deadline = time.time() + self.cfg.timings.failover_fire_after_s
                    playback_confirmed = False
                    
                    while time.time() < confirmation_deadline:
                        try:
                            if verify_device_ready(self.api, cloud_device.id, timeout_s=self.cfg.timings.verify_device_ready_timeout_s):
                                logger.info(f"Playback confirmed for {target_name}")
                                playback_confirmed = True
                                break
                            time.sleep(self.cfg.timings.confirmation_sleep_s)
                        except Exception as e:
                            logger.warning(f"Confirmation check failed: {e}")
                            time.sleep(self.cfg.timings.confirmation_sleep_s)
                    
                    if not playback_confirmed:
                        logger.warning(f"Playback not confirmed by T+{self.cfg.timings.failover_fire_after_s}s for {target_name} - but continuing as device may still be starting")
                    
                    state = State.PLAYING
                    
                    # Success via Web API
                    metrics.branch = "webapi_direct"
                    metrics.total_duration_ms = int((time.time() - total_start_time) * 1000)
                    logger.info(f"Alarm playback completed successfully via Web API for {target_name}")
                    return metrics
                else:
                    logger.info(f"Device {target_name} not available via Web API, proceeding with local discovery")
                    
            except Exception as e:
                logger.warning(f"Web API check failed for {target_name}: {e}, proceeding with local discovery")
            
            log_phase_end(logger, "webapi_check", target_name, int((time.time() - webapi_start) * 1000), False)
            
            # Get target profile for local discovery (only if Web API failed)
            try:
                target = self._get_target_profile(target_name)
            except ValueError:
                # Device not in registry, create a minimal profile for fallback
                logger.info(f"Device {target_name} not in registry, creating minimal profile for fallback")
                from alarm_playback.config import DeviceProfile
                target = DeviceProfile(
                    name=target_name,
                    volume_preset=30  # Default volume
                )
            
            # Check circuit breaker
            if self._should_bypass_primary(target_name):
                logger.warning(f"Bypassing primary path for {target_name} due to circuit breaker")
                return self._failover(metrics, target_name, "circuit_breaker_open")
            
            # Phase 2: Generic IP Wake-up (before mDNS discovery)
            # Try to wake device via IP if we have device IP information
            if target.ip:
                log_phase_start(logger, "ip_wakeup", target_name)
                ip_wakeup_start = time.time()
                
                try:
                    from .fallback import _wake_device_via_ip
                    device_port = target.port or 80
                    device_cpath = target.cpath or "/spotifyconnect/zeroconf"
                    wake_success = _wake_device_via_ip(target.ip, device_port, device_cpath, target_name)
                    
                    ip_wakeup_duration = int((time.time() - ip_wakeup_start) * 1000)
                    log_phase_end(logger, "ip_wakeup", target_name, ip_wakeup_duration, wake_success)
                    
                    if wake_success:
                        logger.info(f"Generic IP wake-up succeeded for {target_name}, checking if device appears in Spotify")
                        # Quick check if device now appears
                        devices = self.api.get_devices(force_refresh=True)
                        cloud_device = self._pick_device(devices, target_name)
                        if cloud_device:
                            logger.info(f"Device {target_name} appeared in Spotify after IP wake-up, skipping to staging")
                            # Store this Spotify device name for future exact matching
                            if cloud_device.name not in target.spotify_device_names:
                                target.spotify_device_names.append(cloud_device.name)
                                logger.info(f"✓ LEARNED: Stored Spotify device name '{cloud_device.name}' for device '{target_name}'")
                            state = State.CLOUD_VISIBLE
                            # Skip to staging and playback (Phase 7 and 8)
                            # Debounce after device is seen
                            time.sleep(self.cfg.timings.debounce_after_seen_s)
                            
                            # Stage device (transfer + volume)
                            log_phase_start(logger, "stage", target_name)
                            stage_device(self.api, cloud_device.id, target.volume_preset)
                            state = State.STAGED
                            log_phase_end(logger, "stage", target_name, None, True)
                            
                            # Start playback
                            log_phase_start(logger, "play", target_name)
                            play_start = time.time()
                            
                            start_play(self.api, cloud_device.id, self.cfg.context_uri,
                                      retry_404_delay_s=self.cfg.timings.retry_404_delay_s,
                                      shuffle=self.cfg.shuffle)
                            
                            metrics.play_ms = int((time.time() - play_start) * 1000)
                            log_phase_end(logger, "play", target_name, metrics.play_ms, True)
                            
                            # T+2s Failover trigger: confirm playback within 2 seconds
                            logger.info(f"Playback started, confirming within {self.cfg.timings.failover_fire_after_s}s...")
                            confirmation_deadline = time.time() + self.cfg.timings.failover_fire_after_s
                            
                            while time.time() < confirmation_deadline:
                                try:
                                    if verify_device_ready(self.api, cloud_device.id, timeout_s=0.5):
                                        logger.info(f"Playback confirmed for {target_name}")
                                        break
                                    time.sleep(0.2)
                                except Exception as e:
                                    logger.warning(f"Confirmation check failed: {e}")
                                    time.sleep(0.2)
                            else:
                                # Not confirmed by T+2s
                                logger.error(f"Playback not confirmed by T+2s for {target_name}")
                                return self._failover(metrics, target_name, "play_not_confirmed_t2")
                            
                            state = State.PLAYING
                            metrics.branch = "primary_ip_wakeup"
                            metrics.cloud_visible_ms = int((time.time() - total_start_time) * 1000)
                            
                            # Record success for circuit breaker
                            self._record_success(target_name)
                            
                            logger.info(f"Successfully completed alarm playback for {target_name} via IP wake-up")
                            return metrics
                except Exception as e:
                    logger.debug(f"Generic IP wake-up failed for {target_name}: {e}")
                    log_phase_end(logger, "ip_wakeup", target_name, int((time.time() - ip_wakeup_start) * 1000), False)
            
            # Phase 3: T-60s Pre-warm - mDNS discovery (fallback)
            log_phase_start(logger, "discovery", target_name)
            discovery_start = time.time()
            
            discovery_result = mdns_discover_connect(target.name, timeout_s=self.cfg.timings.mdns_discovery_timeout_s)
            metrics.discovered_ms = int((time.time() - discovery_start) * 1000)
            
            # Update device profile with instance_name if discovered
            if discovery_result.is_complete and discovery_result.instance_name:
                if not target.instance_name:
                    target.instance_name = discovery_result.instance_name
                    logger.debug(f"Stored instance_name '{discovery_result.instance_name}' for device profile '{target.name}'")
            
            # If mDNS discovery failed, try to use already discovered device profile
            if not discovery_result.is_complete:
                logger.info(f"mDNS discovery failed for {target_name}, checking device registry")
                try:
                    discovered_device = self._get_target_profile(target_name)
                    if discovered_device and discovered_device.ip:
                        # Create a discovery result from the device profile
                        from alarm_playback.discovery import DiscoveryResult
                        discovery_result = DiscoveryResult(
                            ip=discovered_device.ip,
                            port=discovered_device.port or 80,
                            cpath=discovered_device.cpath or "/spotifyconnect/zeroconf",
                            instance_name=target_name
                        )
                        logger.info(f"Using discovered device profile as Spotify Connect device: {target_name} at {discovery_result.ip}:{discovery_result.port} (cpath: {discovery_result.cpath})")
                    else:
                        log_device_state_change(logger, target_name, "UNKNOWN", "DEEP_SLEEP_SUSPECTED")
                        state = State.DEEP_SLEEP_SUSPECTED
                        return self._failover(metrics, target_name, "no_mdns")
                except ValueError:
                    # Device not in registry, cannot proceed
                    log_device_state_change(logger, target_name, "UNKNOWN", "DEEP_SLEEP_SUSPECTED")
                    state = State.DEEP_SLEEP_SUSPECTED
                    return self._failover(metrics, target_name, "no_mdns")
            
            log_phase_end(logger, "discovery", target_name, metrics.discovered_ms, 
                         discovery_result.is_complete)
            
            state = State.DISCOVERED
            
            # Phase 4: T-30s Activate - getInfo check
            log_phase_start(logger, "getinfo", target_name)
            getinfo_start = time.time()
            
            local_ok = get_info(discovery_result.ip, discovery_result.port, 
                               discovery_result.cpath, timeout_s=self.cfg.timings.getinfo_timeout_s)
            metrics.getinfo_ms = int((time.time() - getinfo_start) * 1000)
            
            log_phase_end(logger, "getinfo", target_name, metrics.getinfo_ms, local_ok)
            
            # Even if getInfo fails, try addUser to activate device for account
            if not local_ok:
                logger.warning(f"getInfo failed for {target_name}, but attempting addUser to activate device")
            
            # Phase 5: addUser authentication (always attempt for all devices)
            log_phase_start(logger, "adduser", target_name)
            adduser_start = time.time()
            
            # Unified credentials for all devices - use token_manager for fresh token
            # Refresh token before addUser to ensure it's valid
            try:
                self.api.token_manager.refresh_token_if_needed()
                self.api._spotify = None  # Force recreation with fresh token
            except Exception as e:
                logger.warning(f"Token refresh before addUser failed (non-fatal): {e}")
            
            access_token = self.api.token_manager.get_access_token()
            
            # Verify token is not empty
            if not access_token or len(access_token.strip()) == 0:
                logger.error(f"Invalid or empty access token for {target_name}")
                raise ValueError("Access token is invalid or empty")
            
            logger.debug(f"Using access token for addUser (length: {len(access_token)})")
            
            # Try access_token mode first (more reliable for most devices)
            creds = {
                "userName": "alarm_user",
                "accessToken": access_token,
                "tokenType": "accesstoken"
            }
            
            auth_ok = add_user(
                discovery_result.ip, 
                discovery_result.port,
                discovery_result.cpath, 
                "access_token",  # Force access_token mode for all devices
                creds, 
                timeout_s=self.cfg.timings.adduser_timeout_s
            )
            
            # If access_token failed, try blob_clientKey mode as fallback
            if not auth_ok:
                logger.info(f"access_token mode failed for {target_name}, trying blob_clientKey mode...")
                from alarm_playback.adapters.adduser_spotifywebapipython import create_credential_provider
                provider = create_credential_provider(self.api._get_client(), "generic")
                try:
                    blob_creds = provider.get_blob_clientkey_creds()
                    auth_ok = add_user(
                        discovery_result.ip, 
                        discovery_result.port,
                        discovery_result.cpath, 
                        "blob_clientKey",
                        blob_creds, 
                        timeout_s=self.cfg.timings.adduser_timeout_s
                    )
                except Exception as e:
                    logger.debug(f"blob_clientKey mode also failed: {e}")
            
            metrics.adduser_ms = int((time.time() - adduser_start) * 1000)
            
            log_phase_end(logger, "adduser", target_name, metrics.adduser_ms, auth_ok)
            
            if not auth_ok:
                # addUser failed - log warning but continue
                logger.warning(f"addUser failed for {target_name}, but continuing - device may still appear in Spotify")
                # Don't failover - try to proceed anyway
            else:
                logger.info(f"addUser succeeded for {target_name}, device should now be activated")
                state = State.LOGGED_IN
                # Refresh token to ensure we have a fresh token for device polling
                try:
                    self.api.token_manager.refresh_token_if_needed()
                    # Force recreation of Spotify client with fresh token
                    self.api._spotify = None
                    logger.debug(f"Refreshed token after addUser for {target_name}")
                except Exception as e:
                    logger.warning(f"Token refresh after addUser failed (non-fatal): {e}")
                
                # Try to get updated device info after addUser - device name might have changed
                try:
                    from alarm_playback.zeroconf_client import get_device_info
                    updated_info = get_device_info(discovery_result.ip, discovery_result.port, discovery_result.cpath, timeout_s=self.cfg.timings.device_info_timeout_s)
                    if updated_info:
                        # Extract potential device names from getInfo response
                        name_fields = ['remoteName', 'displayName', 'name', 'deviceName']
                        for field in name_fields:
                            if field in updated_info and updated_info[field]:
                                name_value = str(updated_info[field]).strip()
                                if name_value and name_value not in target.spotify_device_names:
                                    target.spotify_device_names.append(name_value)
                                    logger.info(f"Learned device name '{name_value}' from getInfo after addUser")
                except Exception as e:
                    logger.debug(f"Failed to get device info after addUser (non-fatal): {e}")
                
                # Give device more time to register with Spotify after successful addUser
                # Some devices take several seconds to appear after authentication
                wait_time = self.cfg.timings.adduser_wait_after_s
                logger.info(f"Waiting {wait_time}s for {target_name} to register with Spotify after addUser")
                time.sleep(wait_time)
                
                # Quick check if device appeared immediately after wait
                try:
                    devices = self.api.get_devices(force_refresh=True)
                    cloud_device = self._pick_device(devices, target.name)
                    if cloud_device:
                        logger.info(f"Device {target_name} appeared immediately after addUser wait period")
                        # Store this Spotify device name for future exact matching
                        if cloud_device.name not in target.spotify_device_names:
                            target.spotify_device_names.append(cloud_device.name)
                            logger.info(f"✓ LEARNED: Stored Spotify device name '{cloud_device.name}' for device '{target_name}'")
                        # Continue to staging and playback
                        metrics.cloud_visible_ms = int((time.time() - total_start_time) * 1000)
                        state = State.CLOUD_VISIBLE
                        # Skip directly to staging
                        time.sleep(self.cfg.timings.debounce_after_seen_s)
                        
                        log_phase_start(logger, "stage", target_name)
                        stage_device(self.api, cloud_device.id, target.volume_preset)
                        state = State.STAGED
                        log_phase_end(logger, "stage", target_name, None, True)
                        
                        log_phase_start(logger, "play", target_name)
                        play_start = time.time()
                        start_play(self.api, cloud_device.id, self.cfg.context_uri,
                                  retry_404_delay_s=self.cfg.timings.retry_404_delay_s,
                                  shuffle=self.cfg.shuffle)
                        metrics.play_ms = int((time.time() - play_start) * 1000)
                        log_phase_end(logger, "play", target_name, metrics.play_ms, True)
                        
                        # Confirm playback
                        confirmation_deadline = time.time() + self.cfg.timings.failover_fire_after_s
                        while time.time() < confirmation_deadline:
                            try:
                                if verify_device_ready(self.api, cloud_device.id, timeout_s=self.cfg.timings.verify_device_ready_timeout_s):
                                    logger.info(f"Playback confirmed for {target_name}")
                                    break
                                time.sleep(self.cfg.timings.confirmation_sleep_s)
                            except Exception as e:
                                logger.warning(f"Confirmation check failed: {e}")
                                time.sleep(self.cfg.timings.confirmation_sleep_s)
                        else:
                            logger.error(f"Playback not confirmed by T+2s for {target_name}")
                            return self._failover(metrics, target_name, "play_not_confirmed_t2")
                        
                        state = State.PLAYING
                        metrics.branch = "primary_adduser_immediate"
                        self._record_success(target_name)
                        logger.info(f"Successfully completed alarm playback for {target_name} via addUser immediate")
                        return metrics
                    else:
                        device_names = [d.name for d in devices] if devices else []
                        logger.info(f"Device not yet visible after initial wait (available: {device_names}), continuing to poll...")
                except Exception as e:
                    logger.debug(f"Quick check after addUser failed (non-fatal): {e}")
            
            # If we get here, either getInfo succeeded or addUser succeeded
            if local_ok:
                state = State.LOCAL_AWAKE
            elif auth_ok:
                state = State.LOGGED_IN
            else:
                state = State.DISCOVERED
            
            # Phase 6: Poll /devices until deadline
            log_phase_start(logger, "cloud_poll", target_name)
            cloud_poll_start = time.time()
            
            # If addUser succeeded, extend the deadline since we know device was just authenticated
            # Some devices take longer to appear in Spotify's API after authentication
            base_deadline = self.cfg.timings.total_poll_deadline_s
            if auth_ok:
                extension = self.cfg.timings.poll_deadline_extension_s
                extended_deadline = base_deadline + extension
                logger.info(f"addUser succeeded - extending polling deadline from {base_deadline}s to {extended_deadline}s to allow device to register")
            else:
                extended_deadline = base_deadline
            
            deadline = time.time() + extended_deadline
            fast_until = time.time() + self.cfg.timings.poll_fast_period_s
            cloud_device = None
            
            first_attempt = True
            attempt_count = 0
            while time.time() < deadline:
                try:
                    # Refresh token periodically during polling to ensure it's valid
                    if attempt_count > 0 and attempt_count % 5 == 0:
                        try:
                            self.api.token_manager.refresh_token_if_needed()
                            self.api._spotify = None  # Force recreation
                        except Exception as e:
                            logger.debug(f"Token refresh during polling failed (non-fatal): {e}")
                    
                    devices = self.api.get_devices(force_refresh=True)
                    attempt_count += 1
                    
                    # Log available devices on first attempt and periodically for debugging
                    if first_attempt or (attempt_count % 5 == 0):
                        device_names = [d.name for d in devices] if devices else []
                        matching_names = target.get_all_matching_names()
                        if first_attempt:
                            logger.info(f"Available Spotify devices: {device_names}")
                            logger.info(f"Looking for device '{target.name}' using stored names: {matching_names}")
                            logger.info(f"Device profile instance_name: {target.instance_name}, spotify_device_names: {target.spotify_device_names}")
                            first_attempt = False
                        else:
                            logger.debug(f"Poll attempt {attempt_count}: Available Spotify devices: {device_names}")
                    
                    # Log if we're getting empty device lists repeatedly
                    if not devices or len(devices) == 0:
                        if attempt_count == 1:
                            # On first attempt, verify token and provide detailed diagnostics
                            try:
                                client = self.api._get_client()
                                user_info = client.current_user()
                                user_id = user_info.get('id', 'unknown')
                                logger.warning(f"Spotify API returned empty device list - Token validated for user: {user_id}")
                                logger.warning(f"  This account has no active devices visible via Spotify API")
                                logger.warning(f"  Device may need manual authentication first via Spotify app")
                                logger.warning(f"  Solution: Open Spotify app, select '{target_name}', play a song to authenticate")
                            except Exception as e:
                                logger.error(f"Token validation failed: {e}")
                                logger.error(f"  This may indicate the token is invalid or expired")
                        
                        if attempt_count % 5 == 0:  # Log every 5th empty result to reduce spam
                            remaining_time = deadline - time.time()
                            logger.debug(f"Spotify API still returning empty device list (attempt {attempt_count}, {remaining_time:.1f}s remaining)")
                            # Try to refresh token if we're getting empty results
                            if attempt_count % 10 == 0:  # Only refresh token every 10th attempt
                                try:
                                    self.api.token_manager.refresh_token_if_needed()
                                    self.api._spotify = None
                                    logger.debug("Refreshed token due to empty device list")
                                except Exception as e:
                                    logger.debug(f"Token refresh failed: {e}")
                    else:
                        # Log available devices periodically even when not matching
                        if attempt_count % 5 == 0 and attempt_count > 0:
                            device_names = [d.name for d in devices]
                            remaining_time = deadline - time.time()
                            logger.debug(f"Poll attempt {attempt_count}: {len(devices)} devices available ({device_names}), {remaining_time:.1f}s remaining")
                    
                    cloud_device = self._pick_device(devices, target.name)
                    
                    if cloud_device:
                        logger.info(f"Found device {cloud_device.name} matching {target.name} in Spotify devices")
                        # Store this Spotify device name in the device profile for future exact matching
                        if cloud_device.name not in target.spotify_device_names:
                            target.spotify_device_names.append(cloud_device.name)
                            logger.info(f"✓ LEARNED: Stored Spotify device name '{cloud_device.name}' for device profile '{target.name}'")
                            logger.info(f"  This name will now be used for exact matching on future alarm runs")
                            logger.info(f"  Device profile will be saved automatically after alarm completes")
                        break
                    
                    # Fast polling for first period, then slower
                    sleep_time = self.cfg.timings.poll_sleep_fast_s if time.time() < fast_until else self.cfg.timings.poll_sleep_slow_s
                    time.sleep(sleep_time)
                    
                except Exception as e:
                    log_error(logger, target_name, e, {"phase": "cloud_poll"})
                    time.sleep(self.cfg.timings.poll_sleep_slow_s)
            
            metrics.cloud_visible_ms = int((time.time() - total_start_time) * 1000)
            
            if not cloud_device:
                # Log final diagnostic information
                try:
                    final_devices = self.api.get_devices(force_refresh=True)
                    final_device_names = [d.name for d in final_devices] if final_devices else []
                    logger.error(f"Device {target_name} did not appear in Spotify devices after {extended_deadline}s")
                    logger.error(f"Final device list: {final_device_names}")
                    logger.error(f"Matching names tried: {target.get_all_matching_names()}")
                    if auth_ok:
                        logger.error(f"Note: addUser succeeded but device still didn't appear - device may need manual authentication first")
                        logger.error(f"Try: Open Spotify app, select '{target_name}', play a song to authenticate")
                except Exception as e:
                    logger.error(f"Failed to get final device list for diagnostics: {e}")
                
                return self._failover(metrics, target_name, "not_in_devices_by_deadline")
            
            cloud_poll_duration = int((time.time() - cloud_poll_start) * 1000)
            log_phase_end(logger, "cloud_poll", target_name, cloud_poll_duration, True)
            
            state = State.CLOUD_VISIBLE
            
            # Debounce after device is seen
            time.sleep(self.cfg.timings.debounce_after_seen_s)
            
            # Phase 7: Stage device (transfer + volume)
            log_phase_start(logger, "stage", target_name)
            stage_device(self.api, cloud_device.id, target.volume_preset)
            state = State.STAGED
            log_phase_end(logger, "stage", target_name, None, True)
            
            # Phase 8: T-0 Fire - start playback
            log_phase_start(logger, "play", target_name)
            play_start = time.time()
            
            start_play(self.api, cloud_device.id, self.cfg.context_uri,
                      retry_404_delay_s=self.cfg.timings.retry_404_delay_s,
                      shuffle=self.cfg.shuffle)
            
            metrics.play_ms = int((time.time() - play_start) * 1000)
            log_phase_end(logger, "play", target_name, metrics.play_ms, True)
            
            # T+2s Failover trigger: confirm playback within 2 seconds
            logger.info(f"Playback started, confirming within {self.cfg.timings.failover_fire_after_s}s...")
            confirmation_deadline = time.time() + self.cfg.timings.failover_fire_after_s
            
            while time.time() < confirmation_deadline:
                try:
                    if verify_device_ready(self.api, cloud_device.id, timeout_s=self.cfg.timings.verify_device_ready_timeout_s):
                        logger.info(f"Playback confirmed for {target_name}")
                        break
                    time.sleep(self.cfg.timings.confirmation_sleep_s)
                except Exception as e:
                    logger.warning(f"Confirmation check failed: {e}")
                    time.sleep(self.cfg.timings.confirmation_sleep_s)
            else:
                # Not confirmed by T+2s
                logger.error(f"Playback not confirmed by T+2s for {target_name}")
                return self._failover(metrics, target_name, "play_not_confirmed_t2")
            
            state = State.PLAYING
            metrics.branch = "primary"
            
            # Record success for circuit breaker
            self._record_success(target_name)
            
            logger.info(f"Successfully completed alarm playback for {target_name}")
            
        except Exception as e:
            log_error(logger, target_name, e)
            self._record_failure(target_name)
            
            # Check if exception is already from _failover() to avoid double-wrapping
            if "Alarm playback failed and no fallback succeeded" in str(e):
                # Already in failover, just re-raise
                raise
            
            # Try failover
            return self._failover(metrics, target_name, str(e))
        
        finally:
            metrics.total_duration_ms = int((time.time() - total_start_time) * 1000)
            log_metrics(logger, target_name, metrics.to_dict())
        
        return metrics
    
    def get_device_status(self, target_name: str) -> Dict[str, any]:
        """Get current status of a target device"""
        if target_name not in self._registry:
            raise ValueError(f"Target device '{target_name}' not found")
        
        target = self._registry[target_name]
        cb_state = self.circuit_breakers[target_name]
        
        status = {
            "name": target_name,
            "profile": target.dict(),
            "circuit_breaker": {
                "failure_count": cb_state.failure_count,
                "last_failure_time": cb_state.last_failure_time,
                "is_open": cb_state.is_open
            },
            "available": True  # Could be enhanced with actual availability check
        }
        
        return status
    
    def reset_circuit_breaker(self, target_name: str) -> None:
        """Reset circuit breaker for a device"""
        if target_name in self.circuit_breakers:
            self.circuit_breakers[target_name].record_success()
            logger.info(f"Reset circuit breaker for device {target_name}")
        else:
            raise ValueError(f"Target device '{target_name}' not found")

