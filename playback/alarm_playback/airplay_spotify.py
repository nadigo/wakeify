"""
Play Spotify playlists via AirPlay to any AirPlay 2 compatible device
"""
import subprocess
import logging
import time
import signal
import os
from typing import Optional, Tuple
import threading

logger = logging.getLogger(__name__)


class AirPlaySpotify:
    """
    Play Spotify playlists via AirPlay by:
    1. Starting spotifyd to play music
    2. Using soundcard capture or pipe to get audio
    3. Streaming to AirPlay device via ffmpeg
    """
    
    def __init__(self, target_ip: str, target_port: int = 5004):
        self.target_ip = target_ip
        self.target_port = target_port
        self.spotifyd_process: Optional[subprocess.Popen] = None
        self.ffmpeg_process: Optional[subprocess.Popen] = None
        self._stop_event = threading.Event()
        self._stderr_lines = []  # Store stderr output for debugging
        self._stderr_thread: Optional[threading.Thread] = None
        
    def play_playlist(
        self,
        playlist_uri: str,
        access_token: str,
        device_name: str = "AirPlay Alarm",
        volume: int = 20
    ) -> bool:
        """
        Play Spotify playlist via AirPlay
        
        Args:
            playlist_uri: Spotify playlist URI (e.g., spotify:playlist:...)
            access_token: Spotify access token
            device_name: Name for the spotifyd device
            
        Returns:
            True if playback started successfully
        """
        try:
            # Step 1: Start spotifyd with pipe backend
            logger.info(f"Starting spotifyd for playlist playback: {playlist_uri}")
            if not self._start_spotifyd(playlist_uri, access_token, device_name, volume):
                logger.error("Failed to start spotifyd")
                return False
            
            # Wait for spotifyd to appear as a Spotify Connect device
            logger.info("Waiting for spotifyd to appear as Spotify Connect device...")
            time.sleep(5)
            
            # Step 2: Start AirPlay streaming (captures and streams audio)
            logger.info("Starting AirPlay streaming pipeline...")
            if not self._start_airplay_stream():
                logger.error("Failed to start AirPlay stream")
                self.stop()
                return False
            
            # Now we need to actually play the playlist using the Spotify API
            # We need to wait for spotifyd to appear as a device, then control it via API
            logger.info("Waiting for spotifyd device to appear in Spotify API...")
            
            # Wait for spotifyd to appear and start playback
            from alarm_playback.spotify_api import SpotifyApiWrapper, TokenManager
            from alarm_playback.config import SpotifyAuth
            
            auth_config = SpotifyAuth.from_env()
            spotify_api = SpotifyApiWrapper(TokenManager(auth_config))
            
            # Wait for spotifyd to appear (increased to 30 attempts = 60 seconds)
            # spotifyd can take time to authenticate and register with Spotify
            spotifyd_device = None
            for attempt in range(30):
                devices = spotify_api.get_devices()
                # Look for device matching the name we set for spotifyd
                for device in devices:
                    if device.name == device_name:
                        spotifyd_device = device
                        break
                if spotifyd_device:
                    break
                if attempt < 5 or attempt % 5 == 0:  # Log every 5th attempt after first 5
                    logger.info(f"spotifyd device '{device_name}' not found yet, attempt {attempt + 1}/30...")
                time.sleep(2)
            
            if not spotifyd_device:
                logger.error(f"spotifyd device '{device_name}' did not appear in Spotify API after 60 seconds")
                logger.error("This could mean:")
                logger.error("  1. spotifyd failed to authenticate (check credentials)")
                logger.error("  2. spotifyd is still connecting (needs more time)")
                logger.error("  3. Device name conflict (spotifyd might appear with different name)")
                
                # Log spotifyd stderr output for debugging authentication issues
                if hasattr(self, '_stderr_lines') and self._stderr_lines:
                    logger.error(f"spotifyd stderr output (last 30 lines):")
                    for line in self._stderr_lines[-30:]:
                        logger.error(f"  {line}")
                else:
                    logger.warning("No spotifyd stderr output captured - stderr monitoring may have failed")
                    
                # Also try to read stderr directly if process is still accessible
                if self.spotifyd_process and hasattr(self.spotifyd_process, 'stderr'):
                    try:
                        # Wait a moment and try to read any remaining output
                        import select
                        import sys
                        if self.spotifyd_process.stderr and hasattr(self.spotifyd_process.stderr, 'readable'):
                            # Try non-blocking read if possible
                            logger.debug("Attempting to read remaining stderr from spotifyd process")
                    except Exception as e:
                        logger.debug(f"Could not read stderr directly: {e}")
                
                # Check if process is still running
                if self.spotifyd_process:
                    if self.spotifyd_process.poll() is None:
                        logger.warning("spotifyd process is still running but device not appearing")
                    else:
                        logger.error("spotifyd process has exited - check authentication errors above")
                
                # Log all available devices for debugging
                try:
                    devices = spotify_api.get_devices()
                    logger.error(f"Available Spotify devices: {[d.name for d in devices]}")
                except:
                    pass
                return False
            
            logger.info(f"Found spotifyd device: {spotifyd_device.name} ({spotifyd_device.id})")
            
            # Start playback on spotifyd
            logger.info(f"Starting playlist playback on spotifyd device: {playlist_uri}")
            spotify_api.put_transfer(device_id=spotifyd_device.id, play=True)
            spotify_api.put_play(device_id=spotifyd_device.id, context_uri=playlist_uri)
            
            logger.info("Successfully started playlist playback on spotifyd")
            return True
            
        except Exception as e:
            logger.error(f"Error starting AirPlay Spotify: {e}")
            self.stop()
            return False
    
    def _start_spotifyd(
        self,
        playlist_uri: str,
        access_token: str,
        device_name: str,
        volume: int = 20
    ) -> bool:
        """Start spotifyd configured to output audio to a pipe that ffmpeg can read"""
        try:
            # Create spotifyd config with pipe backend that outputs raw PCM
            # Get credentials from environment
            import os
            username = os.getenv("SPOTIFY_USERNAME", "")
            password = os.getenv("SPOTIFY_PASSWORD", "")
            
            if not username or not password:
                logger.error("SPOTIFY_USERNAME and SPOTIFY_PASSWORD must be set in environment")
                logger.error("To use AirPlay fallback, set these environment variables:")
                logger.error("  export SPOTIFY_USERNAME='your_spotify_username'")
                logger.error("  export SPOTIFY_PASSWORD='your_spotify_password'")
                logger.error("Note: AirPlay fallback is optional - Spotify Connect is preferred")
                return False
            
            # Create the pipe file if it doesn't exist
            os.makedirs("/tmp", exist_ok=True)
            
            # Create named pipe for audio output
            pipe_path = "/tmp/spotifyd_output.pcm"
            try:
                os.mkfifo(pipe_path)
                logger.info(f"Created named pipe: {pipe_path}")
            except FileExistsError:
                logger.info(f"Named pipe already exists: {pipe_path}")
            except Exception as e:
                logger.error(f"Failed to create named pipe: {e}")
                return False
            
            # Create a wrapper script to provide password via environment variable
            # This is more reliable than password_cmd with echo
            wrapper_script = "/tmp/spotifyd_password_wrapper.sh"
            with open(wrapper_script, "w") as f:
                f.write("#!/bin/sh\n")
                f.write(f'echo "$SPOTIFY_PASSWORD"\n')
            os.chmod(wrapper_script, 0o755)
            
            # Escape password for shell command (handle special characters)
            import shlex
            password_escaped = shlex.quote(password)
            
            # Create spotifyd config file
            # Use wrapper script for password_cmd - more reliable than inline echo
            config_content = f"""[global]
username = "{username}"
password_cmd = "{wrapper_script}"
"""
            
            config_path = "/tmp/spotifyd_airplay.conf"
            with open(config_path, "w") as f:
                f.write(config_content)
            logger.debug(f"Created spotifyd config at {config_path}")
            logger.debug(f"Created password wrapper script at {wrapper_script}")
            
            # Start spotifyd with pipe backend
            cmd = [
                "spotifyd",
                "--config-path", config_path,
                "--no-daemon",
                "--backend", "pipe",
                "--device", "/tmp/spotifyd_output.pcm",
                "--device-name", device_name,
                "--bitrate", "320",
                "--initial-volume", str(volume)
            ]
            logger.info(f"Starting spotifyd with pipe backend: {cmd}")
            
            # Set up environment for password
            # spotifyd will call the wrapper script which reads SPOTIFY_PASSWORD from env
            env = os.environ.copy()
            env["SPOTIFY_PASSWORD"] = password
            
            self.spotifyd_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                bufsize=1  # Line buffered
            )
            
            # Start background thread to capture stderr for debugging
            self._stderr_lines = []
            def capture_stderr():
                """Capture stderr output continuously for debugging"""
                try:
                    if self.spotifyd_process and self.spotifyd_process.stderr:
                        for line in iter(self.spotifyd_process.stderr.readline, ''):
                            if not line:
                                break
                            line = line.strip()
                            if line:
                                self._stderr_lines.append(line)
                                # Log important errors/warnings immediately
                                if any(keyword in line.lower() for keyword in ['error', 'failed', 'auth', 'login', 'credential']):
                                    logger.error(f"spotifyd stderr: {line}")
                                elif 'warn' in line.lower():
                                    logger.warning(f"spotifyd stderr: {line}")
                                else:
                                    logger.debug(f"spotifyd stderr: {line}")
                except Exception as e:
                    logger.error(f"Error capturing spotifyd stderr: {e}")
            
            self._stderr_thread = threading.Thread(target=capture_stderr, daemon=True)
            self._stderr_thread.start()
            
            # Wait a bit to check if process starts successfully
            time.sleep(3)
            
            if self.spotifyd_process.poll() is not None:
                # Process already died
                stdout, stderr = self.spotifyd_process.communicate()
                logger.error(f"spotifyd failed to start")
                logger.error(f"spotifyd stdout: {stdout[:500] if stdout else 'None'}")
                logger.error(f"spotifyd stderr: {stderr[:500] if stderr else 'None'}")
                if self._stderr_lines:
                    logger.error(f"Captured stderr lines: {self._stderr_lines}")
                return False
            
            logger.info("spotifyd started - device should appear in Spotify shortly")
            # Log initial stderr output if any
            if self._stderr_lines:
                logger.debug(f"Initial spotifyd output: {self._stderr_lines[:5]}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start spotifyd: {e}")
            return False
    
    def _start_airplay_stream(self) -> bool:
        """Start streaming captured audio to AirPlay device via FFmpeg"""
        try:
            # FFmpeg command to read from the PCM pipe and stream to AirPlay
            cmd = [
                "ffmpeg",
                "-f", "s16le",     # 16-bit little-endian PCM format
                "-ar", "44100",    # Sample rate
                "-ac", "2",        # Stereo
                "-i", "/tmp/spotifyd_output.pcm",  # Read from spotifyd pipe
                "-f", "mp4",       # MP4 container for AirPlay
                "-c:a", "aac",     # AAC codec
                "-ar", "44100",
                "-ac", "2",
                "-b:a", "256k",    # Bitrate
                "-bufsize", "512k",
                f"rtp://{self.target_ip}:{self.target_port}"
            ]
            
            logger.info(f"Starting AirPlay stream to {self.target_ip}:{self.target_port}")
            
            self.ffmpeg_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Wait a moment to see if it starts successfully
            time.sleep(2)
            
            if self.ffmpeg_process.poll() is not None:
                # Process already died
                stdout, stderr = self.ffmpeg_process.communicate()
                logger.error(f"ffmpeg failed: {stderr}")
                return False
            
            logger.info("AirPlay streaming started successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to start AirPlay stream: {e}")
            return False
    
    def stop(self):
        """Stop all processes"""
        try:
            # Stop stderr monitoring thread
            if hasattr(self, '_stderr_thread') and self._stderr_thread and self._stderr_thread.is_alive():
                # Thread will stop when process ends, but we can log final stderr if any
                if hasattr(self, '_stderr_lines') and self._stderr_lines:
                    logger.debug(f"Final spotifyd stderr lines: {self._stderr_lines[-10:]}")
            
            if self.spotifyd_process:
                logger.info("Stopping spotifyd")
                self.spotifyd_process.terminate()
                try:
                    self.spotifyd_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.spotifyd_process.kill()
                # Capture final output
                if hasattr(self.spotifyd_process, 'stderr') and self.spotifyd_process.stderr:
                    try:
                        # stderr is already being read by thread, just log what we captured
                        if hasattr(self, '_stderr_lines') and self._stderr_lines:
                            logger.debug(f"Final spotifyd stderr: {self._stderr_lines[-5:]}")
                    except:
                        pass
            
            if self.ffmpeg_process:
                logger.info("Stopping ffmpeg")
                self.ffmpeg_process.terminate()
                try:
                    self.ffmpeg_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.ffmpeg_process.kill()
                    
        except Exception as e:
            logger.error(f"Error stopping processes: {e}")


def play_spotify_via_airplay(
    target_ip: str,
    playlist_uri: str,
    access_token: str,
    device_name: str = "Alarm AirPlay",
    volume: int = 20
) -> Tuple[bool, Optional[AirPlaySpotify]]:
    """
    Play Spotify playlist via AirPlay
    
    Returns:
        Tuple of (success, AirPlaySpotify instance)
    """
    player = AirPlaySpotify(target_ip)
    
    success = player.play_playlist(playlist_uri, access_token, device_name, volume)
    
    if success:
        return (True, player)
    else:
        return (False, None)

