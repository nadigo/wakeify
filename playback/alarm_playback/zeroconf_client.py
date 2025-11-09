"""
HTTP client for Spotify Connect Zeroconf endpoints
"""

import logging
import threading
from typing import Dict, Any, Optional

import requests
from requests.adapters import HTTPAdapter
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_SESSION: Optional[requests.Session] = None
_SESSION_LOCK = threading.Lock()


def _http_session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                session = requests.Session()
                retry_cfg = Retry(
                    total=2,
                    backoff_factor=0.2,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=("GET", "POST"),
                    raise_on_status=False,
                )
                adapter = HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=retry_cfg)
                session.mount("http://", adapter)
                session.mount("https://", adapter)
                session.headers.update({"User-Agent": "Wakeify-Zeroconf/1.0"})
                _SESSION = session
    return _SESSION


def _normalize_cpath(cpath: Optional[str]) -> str:
    normalized = (cpath or "/spotifyconnect/zeroconf").strip()
    if not normalized:
        normalized = "/spotifyconnect/zeroconf"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized.rstrip("/")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
)
def get_info(ip: str, port: int, cpath: str, timeout_s: float = 1.5) -> bool:
    """
    HTTP GET to getInfo endpoint to check if device is awake.
    
    Args:
        ip: Device IP address
        port: Device port
        cpath: Device CPath (e.g., '/spotify')
        timeout_s: Request timeout in seconds
        
    Returns:
        True if device responds successfully, False otherwise
    """
    session = _http_session()
    cpath_normalized = _normalize_cpath(cpath)
    url = f"http://{ip}:{port}{cpath_normalized}/"
    params = {"action": "getInfo"}
    
    logger.debug(f"GET {url} with params {params}")
    
    try:
        response = session.get(url, params=params, timeout=timeout_s)
        logger.debug(f"getInfo response: {response.status_code}")
        
        if response.status_code == 200:
            logger.info(f"Device {ip}:{port} is awake and responding")
            return True
        else:
            logger.warning(f"Device {ip}:{port} returned status {response.status_code}")
            return False
            
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Connection failed to {ip}:{port}: {e}")
        return False
    except requests.exceptions.Timeout as e:
        logger.warning(f"Timeout connecting to {ip}:{port}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error calling getInfo on {ip}:{port}: {e}")
        return False


@retry(
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout))
)
def add_user(ip: str, port: int, cpath: str, mode: str, creds: Dict[str, Any], timeout_s: float = 2.5) -> bool:
    """
    POST to addUser endpoint to authenticate with device.
    
    Args:
        ip: Device IP address
        port: Device port
        cpath: Device CPath (e.g., '/spotify')
        mode: Authentication mode ("blob_clientKey" or "access_token")
        creds: Credentials dictionary with required fields for the mode
        timeout_s: Request timeout in seconds
        
    Returns:
        True if authentication succeeds, False otherwise
    """
    session = _http_session()
    cpath_normalized = _normalize_cpath(cpath)
    url = f"http://{ip}:{port}{cpath_normalized}/"
    params = {"action": "addUser"}
    
    # Prepare payload based on mode
    if mode == "blob_clientKey":
        payload = {
            "userName": creds["userName"],
            "blob": creds["blob"],
            "clientKey": creds["clientKey"],
            "tokenType": creds["tokenType"]
        }
    elif mode == "access_token":
        payload = {
            "tokenType": "accesstoken",
            "accessToken": creds["accessToken"]
        }
    else:
        logger.error(f"Invalid addUser mode: {mode}")
        return False
    
    logger.debug(f"POST {url} with params {params} and payload {payload}")
    
    try:
        # Try JSON first (modern devices)
        response = session.post(url, params=params, json=payload, timeout=timeout_s)
        logger.debug(f"addUser JSON response: {response.status_code}")
        
        if response.status_code == 200:
            logger.info(f"Successfully authenticated with device {ip}:{port} using JSON")
            return True
        
        # If 415 (Unsupported Media Type), try form-encoded data (some devices don't accept JSON format)
        if response.status_code == 415:
            logger.debug(f"Device returned 415 for JSON, trying form-encoded data")
            try:
                response = session.post(url, params=params, data=payload, timeout=timeout_s)
                logger.debug(f"addUser form-encoded response: {response.status_code}")
                
                if response.status_code == 200:
                    logger.info(f"Successfully authenticated with device {ip}:{port} using form-encoded data")
                    return True
            except Exception as e:
                logger.debug(f"Form-encoded addUser also failed: {e}")
        
        # If still failed, log the error
        logger.warning(f"addUser failed on {ip}:{port}: status {response.status_code}, response: {response.text}")
        return False
            
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Connection failed to {ip}:{port}: {e}")
        return False
    except requests.exceptions.Timeout as e:
        logger.warning(f"Timeout connecting to {ip}:{port}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error calling addUser on {ip}:{port}: {e}")
        return False


def check_device_health(ip: str, port: int, cpath: str, timeout_s: float = 1.0) -> Dict[str, Any]:
    """
    Perform a health check on a Spotify Connect device.
    
    Args:
        ip: Device IP address
        port: Device port
        cpath: Device CPath
        timeout_s: Request timeout in seconds
        
    Returns:
        Dictionary with health check results
    """
    health_info = {
        "reachable": False,
        "responding": False,
        "response_time_ms": None,
        "error": None
    }
    
    import time
    start_time = time.time()
    session = _http_session()
    cpath_normalized = _normalize_cpath(cpath)
    
    try:
        # Try to reach the device
        url = f"http://{ip}:{port}{cpath_normalized}/"
        params = {"action": "getInfo"}
        
        response = session.get(url, params=params, timeout=timeout_s)
        response_time = (time.time() - start_time) * 1000
        
        health_info["reachable"] = True
        health_info["response_time_ms"] = response_time
        
        if response.status_code == 200:
            health_info["responding"] = True
            logger.debug(f"Device {ip}:{port} health check passed ({response_time:.1f}ms)")
        else:
            health_info["error"] = f"HTTP {response.status_code}"
            logger.debug(f"Device {ip}:{port} health check failed: {response.status_code}")
            
    except requests.exceptions.ConnectionError as e:
        health_info["error"] = f"Connection error: {e}"
        logger.debug(f"Device {ip}:{port} health check failed: connection error")
    except requests.exceptions.Timeout as e:
        health_info["error"] = f"Timeout: {e}"
        logger.debug(f"Device {ip}:{port} health check failed: timeout")
    except Exception as e:
        health_info["error"] = f"Unexpected error: {e}"
        logger.error(f"Device {ip}:{port} health check failed: {e}")
    
    return health_info


def get_device_info(ip: str, port: int, cpath: str, timeout_s: float = 2.0) -> Optional[Dict[str, Any]]:
    """
    Get detailed device information from getInfo endpoint.
    
    Args:
        ip: Device IP address
        port: Device port
        cpath: Device CPath
        timeout_s: Request timeout in seconds
        
    Returns:
        Device info dictionary if successful, None otherwise
    """
    session = _http_session()
    cpath_normalized = _normalize_cpath(cpath)
    url = f"http://{ip}:{port}{cpath_normalized}/"
    params = {"action": "getInfo"}
    
    logger.debug(f"Getting device info from {url}")
    
    try:
        response = session.get(url, params=params, timeout=timeout_s)
        
        if response.status_code == 200:
            device_info = response.json()
            logger.info(f"Retrieved device info from {ip}:{port}: {device_info}")
            return device_info
        else:
            logger.warning(f"Failed to get device info from {ip}:{port}: status {response.status_code}")
            return None
            
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Connection failed to {ip}:{port}: {e}")
        return None
    except requests.exceptions.Timeout as e:
        logger.warning(f"Timeout connecting to {ip}:{port}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error getting device info from {ip}:{port}: {e}")
        return None

