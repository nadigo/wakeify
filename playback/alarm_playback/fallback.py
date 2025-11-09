"""
Helpers for waking Spotify Connect devices and re-running Zeroconf auth flows.
"""

import logging
import subprocess
import threading
import time
from typing import Optional

import requests

from .zeroconf_client import get_info

logger = logging.getLogger(__name__)

__all__ = ("_wake_device_via_ip", "_mdns_auth_user_registration")


def _wake_device_via_ip(device_ip: str, device_port: int, device_cpath: Optional[str], device_name: str) -> bool:
    """Attempt to wake a Spotify Connect device using lightweight network probes."""
    logger.info("Attempting IP wake-up for %s at %s:%s", device_name, device_ip, device_port or 80)

    # Step 1: call getInfo on the advertised Zeroconf endpoint
    try:
        cpath = device_cpath or "/spotifyconnect/zeroconf"
        if get_info(device_ip, device_port or 80, cpath, timeout_s=2.0):
            logger.info("Device %s responded to getInfo", device_name)
            return True
    except Exception as exc:  # pragma: no cover - diagnostic only
        logger.debug("getInfo probe failed for %s: %s", device_name, exc)

    # Step 2: send a quick mDNS browse for Spotify Connect services
    def _mdns_query() -> None:
        try:
            subprocess.run(
                ["avahi-browse", "-t", "_spotify-connect._tcp.local", "--resolve", "--terminate"],
                capture_output=True,
                text=True,
                timeout=2,
            )
        except Exception:  # pragma: no cover - best effort only
            pass

    thread = threading.Thread(target=_mdns_query, daemon=True)
    thread.start()
    thread.join(timeout=2)
    time.sleep(0.3)

    # Step 3: HTTP probe across common Spotify ports
    for port in {device_port or 80, 80, 8080, 4070, 4071, 4072}:
        try:
            response = requests.get(f"http://{device_ip}:{port}/", timeout=1.0)
            if response.status_code in (200, 204, 400, 404):
                logger.info("HTTP probe succeeded for %s on port %s", device_name, port)
                return True
        except requests.RequestException:
            continue

    # Step 4: final ICMP ping
    try:
        result = subprocess.run(
            ["ping", "-c", "2", "-W", "1", device_ip],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            logger.info("ICMP ping succeeded for %s", device_name)
            return True
    except Exception as exc:  # pragma: no cover - diagnostic only
        logger.debug("ICMP ping failed for %s: %s", device_name, exc)

    logger.debug("No wake-up probes succeeded for %s", device_name)
    return False


def _mdns_auth_user_registration(target_ip: str, device_name: str) -> None:
    """Trigger Zeroconf auth flows that mirror the Spotify app's behaviour."""
    from alarm_playback.discovery import mdns_discover_connect
    from alarm_playback.spotify_api import SpotifyApiWrapper, TokenManager
    from alarm_playback.config import SpotifyAuth
    from alarm_playback.zeroconf_client import add_user

    logger.debug("Running mDNS auth registration for %s (%s)", device_name, target_ip)

    try:
        discovery = mdns_discover_connect(device_name, timeout_s=2.0)
    except Exception as exc:  # pragma: no cover - diagnostic only
        logger.debug("Discovery failed for %s: %s", device_name, exc)
        discovery = None

    if discovery and discovery.is_complete:
        try:
            token_manager = TokenManager(SpotifyAuth.from_env())
            spotify_api = SpotifyApiWrapper(token_manager)
            access_token = spotify_api.token_manager.get_access_token()
            creds = {"accessToken": access_token}
            if add_user(discovery.ip, discovery.port, discovery.cpath, "access_token", creds, timeout_s=2.5):
                logger.info("addUser completed for %s", device_name)
                return
        except Exception as exc:  # pragma: no cover - diagnostic only
            logger.debug("addUser via access token failed for %s: %s", device_name, exc)

    # Fire mDNS queries for Spotify auth services as a best-effort fallback.
    for service in ("_spotify-connect._tcp.local", "_spotify-user._tcp.local", "_spotify-auth._tcp.local"):
        try:
            subprocess.run(
                ["avahi-browse", "-t", service, "--resolve", "--terminate"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            time.sleep(0.5)
        except Exception:  # pragma: no cover
            pass

    # Hit common Spotify Connect ports to wake embedded web servers.
    for port in (4070, 4071, 4072, 8080, 8081):
        try:
            subprocess.run(
                ["curl", "-s", "--connect-timeout", "2", f"http://{target_ip}:{port}/"],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except Exception:  # pragma: no cover
            pass

    logger.debug("Completed mDNS auth stimulation for %s", device_name)
