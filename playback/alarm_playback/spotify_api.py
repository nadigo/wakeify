"""
Spotify Web API wrapper using spotipy
"""

import logging
import json
import os
import time
import random
import threading
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from spotipy import Spotify, SpotifyOAuth, SpotifyException
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from alarm_playback.models import CloudDevice
from alarm_playback.config import SpotifyAuth

logger = logging.getLogger(__name__)


class TokenManager:
    """Manages Spotify access token with automatic refresh"""
    
    def __init__(self, auth_config: SpotifyAuth):
        """
        Initialize token manager.
        
        Args:
            auth_config: Spotify authentication configuration
        """
        self.auth_config = auth_config
        self._spotify = None
        self._oauth = None
        self._token_info: Optional[Dict[str, Any]] = None
        self._token_lock = threading.Lock()
        self._last_refresh_ts: float = 0.0
        self._refresh_margin_s = 120.0
    
    def _create_oauth_manager(self) -> SpotifyOAuth:
        """Create SpotifyOAuth manager"""
        if self._oauth is None:
            self._oauth = SpotifyOAuth(
                client_id=self.auth_config.client_id,
                client_secret=self.auth_config.client_secret,
                redirect_uri=self.auth_config.redirect_uri,
                scope="user-read-playback-state user-modify-playback-state",
                cache_path=self.auth_config.access_token_cache
            )
        return self._oauth
    
    def _token_file_path(self) -> Path:
        data_dir = Path(os.environ.get("DATA_DIR", "/data/wakeify/data"))
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir / "token.json"
    
    def _save_token_to_file(self, token_info: Dict[str, Any]) -> None:
        try:
            token_file = self._token_file_path()
            token_file.write_text(json.dumps(token_info, indent=2))
        except Exception as exc:
            logger.debug(f"Failed to persist Spotify token to file: {exc}")
    
    def _ensure_token_info(self, force_refresh: bool = False) -> Tuple[Dict[str, Any], bool]:
        """
        Ensure we have a non-expired token_info payload.
        
        Returns:
            tuple[token_info, refreshed_flag]
        """
        with self._token_lock:
            refreshed = False
            oauth = self._create_oauth_manager()
            
            if force_refresh or not self._token_info:
                token_info = oauth.get_cached_token()
                if not token_info:
                    token_info = self._load_token_from_file()
                
                if not token_info and self.auth_config.refresh_token:
                    try:
                        token_info = oauth.refresh_access_token(self.auth_config.refresh_token)
                        refreshed = True
                        logger.info("Successfully refreshed Spotify token from environment refresh token")
                    except Exception as exc:
                        logger.error(f"Failed to refresh Spotify token from environment: {exc}")
                        raise
                elif not token_info:
                    raise ValueError("No Spotify token available in cache, file, or environment refresh token")
                
                if refreshed:
                    self._save_token_to_file(token_info)
                self._token_info = token_info
            
            if not self._token_info:
                raise ValueError("Spotify token info unavailable after initialization")
            
            expires_at = self._token_info.get("expires_at")
            now = time.time()
            if not expires_at or expires_at - now <= self._refresh_margin_s:
                refresh_token = self._token_info.get("refresh_token") or self.auth_config.refresh_token
                if not refresh_token:
                    logger.error("Spotify token about to expire but no refresh_token is available")
                else:
                    try:
                        token_info = oauth.refresh_access_token(refresh_token)
                        refreshed = True
                        self._token_info = token_info
                        self._save_token_to_file(token_info)
                        logger.debug("Spotify token refreshed due to expiry window")
                    except Exception as exc:
                        logger.error(f"Failed to refresh Spotify token using refresh token: {exc}")
                        raise
            
            if refreshed:
                self._spotify = None  # Force rebuild of spotipy client with new token
                self._last_refresh_ts = time.time()
            
            return self._token_info, refreshed
    
    def _create_spotify_client(self) -> Spotify:
        """Create authenticated Spotify client"""
        if self._spotify is None:
            token_info, _ = self._ensure_token_info()
            self._spotify = Spotify(auth=token_info['access_token'])
        
        return self._spotify
    
    def _load_token_from_file(self) -> Optional[Dict[str, Any]]:
        """Load token from existing token.json file"""
        try:
            token_file = self._token_file_path()
            if token_file.exists():
                token_data = json.loads(token_file.read_text())
                # Check if token is still valid (not expired)
                if token_data.get('expires_at', 0) > time.time():
                    return token_data
                # If expired but has refresh_token, return it for refresh
                if 'refresh_token' in token_data:
                    return token_data
        except Exception as e:
            logger.error(f"Failed to load token from file: {e}")
        return None
    
    def get_access_token(self) -> str:
        """
        Get valid access token, refreshing if necessary.
        
        Returns:
            Valid access token string
        """
        try:
            token_info, _ = self._ensure_token_info()
            access_token = token_info.get('access_token')
            if not access_token:
                raise ValueError("Spotify token payload missing access_token")
            return access_token
        except Exception as e:
            logger.error(f"Failed to get access token: {e}")
            raise
    
    def refresh_token_if_needed(self, force: bool = False) -> bool:
        """
        Refresh token if it's expired or close to expiring.
        
        Returns:
            True if token was refreshed, False if still valid
        """
        try:
            _, refreshed = self._ensure_token_info(force_refresh=force)
            if refreshed:
                logger.info("Spotify access token refreshed")
            return refreshed
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return False


class SpotifyApiWrapper:
    """Wrapper around spotipy for alarm playback operations"""
    
    DEVICE_CACHE_TTL_S = 0.75
    TOKEN_VALIDATION_TTL_S = 300.0
    PLAYLIST_CACHE_TTL_S = 300.0
    
    def __init__(self, token_manager: TokenManager):
        """
        Initialize API wrapper.
        
        Args:
            token_manager: Token manager instance
        """
        self.token_manager = token_manager
        self._spotify = None
        self._device_cache: Optional[Tuple[List[CloudDevice], float]] = None
        self._last_validation_ts: float = 0.0
        # Cache playlist track counts to avoid repeated metadata fetches when shuffle is enabled
        self._playlist_track_cache: Dict[str, Tuple[int, float]] = {}
    
    def _get_client(self) -> Spotify:
        """Get authenticated Spotify client"""
        if self._spotify is None:
            access_token = self.token_manager.get_access_token()
            self._spotify = Spotify(auth=access_token)
            # Ensure the client has an auth manager for compatibility
            if not hasattr(self._spotify, '_auth_manager') or self._spotify._auth_manager is None:
                # Create a simple auth manager with the access token
                from spotipy.oauth2 import SpotifyClientCredentials
                self._spotify._auth_manager = SpotifyClientCredentials(
                    client_id=self.token_manager.auth_config.client_id,
                    client_secret=self.token_manager.auth_config.client_secret
                )
                # Override get_access_token to return our token
                self._spotify._auth_manager.get_access_token = lambda: access_token
        return self._spotify
    
    def invalidate_device_cache(self) -> None:
        """Clear cached Spotify device list."""
        self._device_cache = None
    
    def _get_cached_playlist_tracks(self, playlist_id: str) -> Optional[int]:
        """Return cached playlist track count if still fresh."""
        cached = self._playlist_track_cache.get(playlist_id)
        if not cached:
            return None
        count, cached_ts = cached
        if time.time() - cached_ts <= self.PLAYLIST_CACHE_TTL_S:
            return count
        # Cache expired
        self._playlist_track_cache.pop(playlist_id, None)
        return None

    def _set_cached_playlist_tracks(self, playlist_id: str, track_count: int) -> None:
        """Store playlist track count in cache."""
        if track_count < 0:
            return
        self._playlist_track_cache[playlist_id] = (track_count, time.time())

    def _extract_playlist_id(self, context_uri: str) -> Optional[str]:
        """Extract playlist ID from a Spotify context URI."""
        if not context_uri:
            return None
        if context_uri.startswith("spotify:playlist:"):
            return context_uri.split(":")[-1]
        if context_uri.startswith("https://open.spotify.com/playlist/"):
            return context_uri.rstrip("/").split("/")[-1].split("?")[0]
        return None

    def _get_playlist_track_count(self, client: Spotify, context_uri: str) -> Optional[int]:
        """Retrieve total number of tracks for the playlist backing the given context."""
        playlist_id = self._extract_playlist_id(context_uri)
        if not playlist_id:
            return None

        cached_total = self._get_cached_playlist_tracks(playlist_id)
        if cached_total is not None:
            return cached_total

        try:
            playlist_info = client.playlist(playlist_id, fields='tracks.total')
        except Exception as exc:
            logger.debug(f"Failed to fetch playlist metadata for {playlist_id}: {exc}")
            return None

        total_tracks = playlist_info.get('tracks', {}).get('total', 0) if playlist_info else 0
        if isinstance(total_tracks, int) and total_tracks >= 0:
            self._set_cached_playlist_tracks(playlist_id, total_tracks)
            return total_tracks
        return None

    def _validate_token_if_needed(self, client: Spotify) -> None:
        """Call a lightweight validation endpoint only when stale."""
        now = time.time()
        if now - self._last_validation_ts < self.TOKEN_VALIDATION_TTL_S:
            return
        
        try:
            user_info = client.current_user()
            user_id = user_info.get('id', 'unknown') if user_info else 'unknown'
            user_display_name = user_info.get('display_name', 'unknown') if user_info else 'unknown'
            logger.debug(f"Token validated: user_id={user_id}, display_name={user_display_name}")
        except Exception as exc:
            logger.warning(f"Failed to validate Spotify token via current_user(): {exc}")
        finally:
            self._last_validation_ts = now
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def get_devices(self, force_refresh: bool = False) -> List[CloudDevice]:
        """
        Get list of available Spotify devices.
        
        Returns:
            List of CloudDevice objects
        """
        try:
            now = time.time()
            if not force_refresh and self._device_cache:
                cached_devices, cached_ts = self._device_cache
                if now - cached_ts <= self.DEVICE_CACHE_TTL_S:
                    logger.debug("Returning cached Spotify device list")
                    return cached_devices
            
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            self._validate_token_if_needed(client)
            devices_response = client.devices()
            devices = devices_response.get('devices', [])
            
            # Log device information (only warn once per session for empty lists)
            if not devices:
                # Only log warning at debug level to reduce noise - the orchestrator will log it once
                logger.debug("Spotify API returned empty device list")
            else:
                device_names = [d.get('name', 'unknown') for d in devices]
                logger.debug(f"Spotify API returned {len(devices)} devices: {device_names}")
            
            cloud_devices = []
            for device_dict in devices:
                cloud_device = CloudDevice.from_spotify_dict(device_dict)
                cloud_devices.append(cloud_device)
            
            logger.debug(f"Retrieved {len(cloud_devices)} devices from Spotify API")
            self._device_cache = (cloud_devices, time.time())
            return cloud_devices
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed(force=True)
                self._spotify = None  # Force recreation with new token
                self.invalidate_device_cache()
                raise  # Let retry mechanism handle it
            else:
                logger.error(f"Spotify API error getting devices: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error getting devices: {e}")
            raise
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def get_playlists(self) -> List[Dict[str, Any]]:
        """
        Get user's playlists.
        
        Returns:
            List of playlist dictionaries
        """
        try:
            client = self._get_client()
            playlists = client.current_user_playlists(limit=50)
            return playlists['items'] if playlists else []
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
                self._spotify = None  # Force recreation with new token
                raise  # Let retry mechanism handle it
            else:
                logger.error(f"Spotify API error getting playlists: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error getting playlists: {e}")
            raise
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def put_transfer(self, device_id: str, play: bool = False) -> None:
        """
        Transfer playback to a specific device.
        
        Args:
            device_id: Target device ID
            play: Whether to start playing immediately
        """
        try:
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            client.transfer_playback(device_id=device_id, force_play=play)
            logger.debug(f"Transferred playback to device {device_id} (play={play})")
            self.invalidate_device_cache()
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed(force=True)
                self._spotify = None
                raise
            else:
                logger.error(f"Spotify API error transferring playback: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error transferring playback: {e}")
            raise
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def put_volume(self, device_id: str, percent: int) -> None:
        """
        Set volume for a specific device.
        
        Args:
            device_id: Target device ID
            percent: Volume percentage (0-100)
        """
        try:
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            client.volume(volume_percent=percent, device_id=device_id)
            logger.debug(f"Set volume to {percent}% for device {device_id}")
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed(force=True)
                self._spotify = None
                raise
            else:
                logger.error(f"Spotify API error setting volume: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error setting volume: {e}")
            raise
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def put_play(self, device_id: str, context_uri: Optional[str] = None, retry_404_delay_s: float = 0.7, shuffle: bool = False) -> None:
        """
        Start playback on a specific device.
        
        Args:
            device_id: Target device ID
            context_uri: URI to play (playlist, album, artist)
            retry_404_delay_s: Delay before retrying on 404 error
            shuffle: Whether to enable shuffle mode
        """
        try:
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            
            # Set shuffle state BEFORE starting playback to ensure it applies to first track
            if shuffle:
                try:
                    client.shuffle(True, device_id=device_id)
                    logger.debug(f"Shuffle enabled on device {device_id}")
                except Exception as e:
                    logger.warning(f"Failed to set shuffle on device {device_id}: {e}")
            
            # Start playback with random offset for shuffle to ensure first song varies
            if context_uri:
                if shuffle and 'playlist' in context_uri:
                    # Get playlist track count for random offset
                    try:
                        total_tracks = self._get_playlist_track_count(client, context_uri)
                        
                        if total_tracks and total_tracks > 1:
                            # Pick a random starting position
                            random_offset = random.randint(0, total_tracks - 1)
                            logger.debug(f"Starting shuffled playlist at random position {random_offset} of {total_tracks}")
                            client.start_playback(device_id=device_id, context_uri=context_uri, offset={"position": random_offset})
                        else:
                            client.start_playback(device_id=device_id, context_uri=context_uri)
                            logger.info(f"Started playback on device {device_id} with context {context_uri}")
                    except Exception as e:
                        # Fallback: if we can't get track count, just start normally
                        logger.warning(f"Could not get playlist info for random offset: {e}")
                        client.start_playback(device_id=device_id, context_uri=context_uri)
                        logger.info(f"Started playback on device {device_id} with context {context_uri}")
                else:
                    client.start_playback(device_id=device_id, context_uri=context_uri)
                    logger.info(f"Started playback on device {device_id} with context {context_uri}")
            else:
                client.start_playback(device_id=device_id)
                logger.info(f"Started playback on device {device_id}")
                
            self.invalidate_device_cache()
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
                self._spotify = None
                raise
            elif e.http_status == 404:
                # Device not found - retry once after delay
                logger.warning(f"Device {device_id} not found (404), retrying after {retry_404_delay_s}s...")
                import time
                time.sleep(retry_404_delay_s)
                
                # Retry once
                try:
                    # Set shuffle before retrying playback
                    if shuffle:
                        try:
                            client.shuffle(True, device_id=device_id)
                            logger.info(f"Shuffle enabled on device {device_id} (retry)")
                        except Exception:
                            pass  # Ignore shuffle errors during retry
                    
                    # Retry with same logic as main path
                    if context_uri:
                        if shuffle and 'playlist' in context_uri:
                            try:
                                total_tracks = self._get_playlist_track_count(client, context_uri)
                                
                                if total_tracks and total_tracks > 1:
                                    random_offset = random.randint(0, total_tracks - 1)
                                    logger.info(f"Starting shuffled playlist at random position {random_offset} of {total_tracks} (retry)")
                                    client.start_playback(device_id=device_id, context_uri=context_uri, offset={"position": random_offset})
                                else:
                                    client.start_playback(device_id=device_id, context_uri=context_uri)
                            except Exception:
                                # Fallback on retry
                                client.start_playback(device_id=device_id, context_uri=context_uri)
                        else:
                            client.start_playback(device_id=device_id, context_uri=context_uri)
                    else:
                        client.start_playback(device_id=device_id)
                    logger.info(f"Retry successful - started playback on device {device_id}")
                    self.invalidate_device_cache()
                except SpotifyException as retry_e:
                    logger.error(f"Retry failed for device {device_id}: {retry_e}")
                    raise retry_e
            else:
                logger.error(f"Spotify API error starting playback: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error starting playback: {e}")
            raise
    
    def get_current_playback(self) -> Optional[Dict[str, Any]]:
        """
        Get current playback state.
        
        Returns:
            Current playback info or None if no active playback
        """
        try:
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            playback_info = client.current_playback()
            return playback_info
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed(force=True)
                self._spotify = None
                raise
            else:
                logger.error(f"Spotify API error getting current playback: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error getting current playback: {e}")
            raise
    
    def pause_playback(self, device_id: str) -> None:
        """
        Pause playback on a specific device.
        
        Args:
            device_id: Target device ID
        """
        try:
            self.token_manager.refresh_token_if_needed()
            client = self._get_client()
            client.pause_playback(device_id=device_id)
            logger.info(f"Paused playback on device {device_id}")
            self.invalidate_device_cache()
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed(force=True)
                self._spotify = None
                raise
            else:
                logger.error(f"Spotify API error pausing playback: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error pausing playback: {e}")
            raise

