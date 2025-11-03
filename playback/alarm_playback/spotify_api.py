"""
Spotify Web API wrapper using spotipy
"""

import logging
import json
import os
import time
import random
from typing import List, Optional, Dict, Any
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
    
    def _create_spotify_client(self) -> Spotify:
        """Create authenticated Spotify client"""
        if self._spotify is None:
            oauth = self._create_oauth_manager()
            
            # Try to get cached token first
            token_info = oauth.get_cached_token()
            
            if not token_info:
                # If no cached token, try to load from existing token.json file
                token_info = self._load_token_from_file()
                
                if not token_info:
                    # If no token file, try to refresh using the provided refresh token
                    if self.auth_config.refresh_token:
                        try:
                            token_info = oauth.refresh_access_token(self.auth_config.refresh_token)
                            logger.info("Successfully refreshed access token from env")
                        except Exception as e:
                            logger.error(f"Failed to refresh access token from env: {e}")
                            raise
                    else:
                        raise ValueError("No refresh token available in environment or token file")
                else:
                    logger.info("Loaded token from existing token.json file")
            
            self._spotify = Spotify(auth=token_info['access_token'])
        
        return self._spotify
    
    def _load_token_from_file(self) -> Optional[Dict[str, Any]]:
        """Load token from existing token.json file"""
        try:
            token_file = os.path.join(os.environ.get("DATA_DIR", "/data/wakeify/data"), "token.json")
            if os.path.exists(token_file):
                with open(token_file, 'r') as f:
                    token_data = json.load(f)
                    # Check if token is still valid (not expired)
                    if 'expires_at' in token_data and token_data['expires_at'] > time.time():
                        return token_data
                    # If expired but has refresh_token, return it for refresh
                    elif 'refresh_token' in token_data:
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
            spotify = self._create_spotify_client()
            # Force token refresh if needed
            if spotify._auth:
                return spotify._auth
            else:
                raise ValueError("No valid access token available")
        except Exception as e:
            logger.error(f"Failed to get access token: {e}")
            raise
    
    def refresh_token_if_needed(self) -> bool:
        """
        Refresh token if it's expired or close to expiring.
        
        Returns:
            True if token was refreshed, False if still valid
        """
        try:
            oauth = self._create_oauth_manager()
            token_info = oauth.get_cached_token()
            
            if not token_info:
                return False
            
            # Check if token needs refresh (expires in less than 5 minutes)
            import time
            if token_info.get('expires_at', 0) - time.time() < 300:
                oauth.refresh_access_token(token_info['refresh_token'])
                self._spotify = None  # Force recreation with new token
                logger.info("Token refreshed successfully")
                return True
            
            return False
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return False


class SpotifyApiWrapper:
    """Wrapper around spotipy for alarm playback operations"""
    
    def __init__(self, token_manager: TokenManager):
        """
        Initialize API wrapper.
        
        Args:
            token_manager: Token manager instance
        """
        self.token_manager = token_manager
        self._spotify = None
    
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
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((SpotifyException,))
    )
    def get_devices(self) -> List[CloudDevice]:
        """
        Get list of available Spotify devices.
        
        Returns:
            List of CloudDevice objects
        """
        try:
            client = self._get_client()
            
            # First, verify token is valid by checking current user
            try:
                user_info = client.current_user()
                user_id = user_info.get('id', 'unknown')
                user_display_name = user_info.get('display_name', 'unknown')
                logger.debug(f"Token validated: user_id={user_id}, display_name={user_display_name}")
            except Exception as e:
                logger.warning(f"Failed to verify token validity with current_user(): {e}")
                # Continue anyway - might still work for devices
            
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
            return cloud_devices
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
                self._spotify = None  # Force recreation with new token
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
            client = self._get_client()
            client.transfer_playback(device_id=device_id, force_play=play)
            logger.debug(f"Transferred playback to device {device_id} (play={play})")
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
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
            client = self._get_client()
            client.volume(volume_percent=percent, device_id=device_id)
            logger.debug(f"Set volume to {percent}% for device {device_id}")
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
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
                        playlist_id = context_uri.split(':')[-1]
                        playlist_info = client.playlist(playlist_id, fields='tracks.total')
                        total_tracks = playlist_info.get('tracks', {}).get('total', 0)
                        
                        if total_tracks > 1:
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
                                playlist_id = context_uri.split(':')[-1]
                                playlist_info = client.playlist(playlist_id, fields='tracks.total')
                                total_tracks = playlist_info.get('tracks', {}).get('total', 0)
                                
                                if total_tracks > 1:
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
            client = self._get_client()
            playback_info = client.current_playback()
            return playback_info
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
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
            client = self._get_client()
            client.pause_playback(device_id=device_id)
            logger.info(f"Paused playback on device {device_id}")
            
        except SpotifyException as e:
            if e.http_status == 401:
                logger.warning("Access token expired, refreshing...")
                self.token_manager.refresh_token_if_needed()
                self._spotify = None
                raise
            else:
                logger.error(f"Spotify API error pausing playback: {e}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error pausing playback: {e}")
            raise

